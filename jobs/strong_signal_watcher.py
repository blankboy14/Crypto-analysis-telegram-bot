"""
jobs/strong_signal_watcher.py

Phase 2.2 - "Find 24/7 Strong Signal". Scheduled by
bot/handlers/strong_signal.py as a per-chat repeating job
(context.job_queue.run_repeating(tick, interval=scan_interval_seconds,
chat_id=..., data={"market": ...})).

WHY THIS ISN'T "JUST CALL scan_market_above_confidence() EVERY TICK":
engine/signal_scanner.py is explicit that a full scan (every pair, 6
timeframes, order flow, news) is a genuinely multi-minute operation.
Two things follow from that:

1. It must never run directly inside this async tick() - that would
   block the whole bot's event loop for minutes, freezing every other
   chat/command/button in the meantime. It's run in a background
   thread via loop.run_in_executor() instead.
2. If several chats have this mode ON for the same market, they
   shouldn't each trigger their own independent multi-minute scan on
   their own schedule - that's the same expensive work repeated N
   times for no benefit. A small shared cache (keyed by market
   "spot"/"future"/"both") plus a per-market lock means only ONE scan
   for a given market is ever in flight at a time; whichever chats'
   ticks land while it's stale all await the SAME result instead of
   starting duplicate scans.

Per-chat state is limited to the push cooldown (so a chat doesn't get
the same pair+verdict pushed again within cooldown_seconds while it
keeps qualifying scan after scan) - like volume_spike_watcher.py, this
is in-memory and resets on a bot restart, which is an acceptable cost
for a "don't spam" mechanism.
"""
import asyncio
import logging
import time

from engine.signal_scanner import MARKET_SCOPE_MAP, scan_market_above_confidence
from engine.bitget_api import get_token_list
from engine.order_flow import get_order_flow
from bot import state_store
from bot.formatters import format_strong_signal, format_pump_reversal_alert
from bot.scan_executor import SCAN_EXECUTOR

log = logging.getLogger("crypto-telegram-bot")

MODE = "strong_signal"

# --- shared, module-level state (see docstring above for why) ---

# market ("spot"/"future"/"both") -> {"result": <scan_market_above_confidence() output>, "ts": <unix ts>}
_scan_cache: dict = {}

# market -> asyncio.Lock, created lazily on first use (can't create
# real asyncio.Lock objects at import time outside a running loop in
# every Python/PTB version, so this is built on first need instead).
_scan_locks: dict = {}

# (chat_id, raw_symbol, verdict) -> unix ts of the last push for that
# exact pair+verdict, for this chat's cooldown.
_last_push: dict = {}

# market ("spot"/"future"/"both") -> {"events": [<reversal event dict>, ...], "ts": <unix ts>}
# Same one-shared-check-per-market idea as _scan_cache above, but much
# cheaper (token list + occasional order-flow calls only, no full
# multi-timeframe scan) so it can refresh far more often.
_pump_check_cache: dict = {}
_pump_check_locks: dict = {}

# (chat_id, raw_symbol) -> unix ts of the last pump-reversal push for
# this chat's cooldown (separate from _last_push above since these
# aren't scan_market_above_confidence results and don't have a
# "verdict" from the normal engine - they're always SELL by design).
_last_pump_push: dict = {}


def _get_pump_lock(market: str) -> asyncio.Lock:
    lock = _pump_check_locks.get(market)
    if lock is None:
        lock = asyncio.Lock()
        _pump_check_locks[market] = lock
    return lock


def _run_pump_reversal_check(scopes: list, cfg: dict) -> list:
    """
    For every pair in `scopes`: records today's price, checks whether
    its trailing cumulative move now crosses the "overextended"
    threshold (and flags it if so), then checks every currently
    -flagged pair for a reversal (price down `reversal_drop_pct` off
    its peak since being flagged) confirmed by sell-dominant order
    flow. Returns the reversal events found this check - one per pair,
    each only returned once (resolve_overextended() is called
    immediately so the same reversal isn't re-detected next check).
    """
    window_days = cfg.get("pump_window_days", 5)
    pump_threshold = cfg.get("pump_threshold_pct", 80.0)
    reversal_drop_pct = cfg.get("reversal_drop_pct", 15.0)
    reversal_sell_flow_pct = cfg.get("reversal_sell_flow_pct", 55.0)

    events = []
    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Pump reversal check: token list fetch failed for {scope}: {exc}")
            continue

        price_by_symbol = {}
        for token in tokens:
            raw_symbol = token["rawSymbol"]
            price = token.get("lastPrice")
            if not price:
                continue
            price_by_symbol[raw_symbol] = (price, token["symbol"])

            state_store.record_daily_price(scope, raw_symbol, price)
            cumulative_pct = state_store.get_cumulative_pct(scope, raw_symbol, window_days)
            if cumulative_pct is not None and cumulative_pct >= pump_threshold:
                state_store.flag_overextended(scope, raw_symbol, token["symbol"], cumulative_pct, price)

        for ov in state_store.get_overextended(scope):
            raw_symbol = ov["rawSymbol"]
            current = price_by_symbol.get(raw_symbol)
            if current is None:
                continue
            current_price, symbol = current
            peak = ov["peakPrice"]
            if peak <= 0:
                continue
            drop_pct = (peak - current_price) / peak * 100
            if drop_pct < reversal_drop_pct:
                continue

            sell_pct = None
            try:
                flow = get_order_flow(raw_symbol, scope, bucket_seconds=60)
                if flow:
                    live = flow.get("live") or {}
                    recent = live.get("last60s") or live
                    buy_pct = recent.get("buyPct")
                    if buy_pct is not None:
                        sell_pct = 100 - buy_pct
            except Exception as exc:
                log.error(f"Pump reversal check: order flow fetch failed for {raw_symbol} ({scope}): {exc}")

            # Order flow, when available, must actually confirm sell
            # pressure before firing - a flaky/unavailable fetch
            # doesn't block the alert (the price drop is the primary
            # evidence), but a flow reading that's still buy-dominant
            # means this isn't confirmed yet.
            if sell_pct is not None and sell_pct < reversal_sell_flow_pct:
                continue

            events.append({
                "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol,
                "cumulativePct": ov["cumulativePct"], "peakPrice": peak,
                "currentPrice": current_price, "dropPct": drop_pct, "sellPct": sell_pct,
            })
            state_store.resolve_overextended(scope, raw_symbol)

    return events


async def _get_or_run_pump_check(market: str, scopes: list, cfg: dict) -> list:
    """Same shared-cache-per-market pattern as _get_or_run_scan(), sized to this check's own (much shorter) refresh interval."""
    max_age = cfg.get("pump_check_interval_seconds", 60)
    cached = _pump_check_cache.get(market)
    now = time.time()
    if cached and (now - cached["ts"]) < max_age:
        return cached["events"]

    lock = _get_pump_lock(market)
    async with lock:
        cached = _pump_check_cache.get(market)
        now = time.time()
        if cached and (now - cached["ts"]) < max_age:
            return cached["events"]

        loop = asyncio.get_running_loop()
        events = await loop.run_in_executor(SCAN_EXECUTOR, _run_pump_reversal_check, scopes, cfg)
        _pump_check_cache[market] = {"events": events, "ts": time.time()}
        return events


def _get_lock(market: str) -> asyncio.Lock:
    lock = _scan_locks.get(market)
    if lock is None:
        lock = asyncio.Lock()
        _scan_locks[market] = lock
    return lock


async def _get_or_run_scan(market: str, min_confidence: float, enabled_indicators, worker_count: int, max_age_seconds: float) -> dict:
    """
    Returns a fresh-enough scan_market_above_confidence() result for
    `market`, reusing the cached one if it's still within
    `max_age_seconds`, otherwise running exactly one new scan (never
    more than one concurrently per market - see module docstring).
    """
    cached = _scan_cache.get(market)
    now = time.time()
    if cached and (now - cached["ts"]) < max_age_seconds:
        return cached["result"]

    lock = _get_lock(market)
    async with lock:
        # Re-check inside the lock - another chat's tick may have just
        # refreshed this while we were waiting for the lock.
        cached = _scan_cache.get(market)
        now = time.time()
        if cached and (now - cached["ts"]) < max_age_seconds:
            return cached["result"]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            SCAN_EXECUTOR,
            scan_market_above_confidence,
            market, min_confidence, enabled_indicators, None, worker_count,
        )
        _scan_cache[market] = {"result": result, "ts": time.time()}
        return result


async def tick(context) -> None:
    """The job_queue.run_repeating callback - one push-check for one chat."""
    job = context.job
    chat_id = job.chat_id
    market = (job.data or {}).get("market")

    # Self-heal: mirrors volume_spike_watcher.tick()'s guard - if this
    # chat's mode got turned off but the job somehow survived, stop it
    # instead of doing pointless work every interval.
    if not state_store.is_mode_on(chat_id, MODE):
        job.schedule_removal()
        return

    if market not in MARKET_SCOPE_MAP:
        log.error(f"Strong signal watch: unknown market {market!r} for chat {chat_id}, stopping job")
        job.schedule_removal()
        return

    settings = context.bot_data.get("settings", {})
    cfg = settings.get("strong_signal_watch", {})
    min_confidence = cfg.get("min_confidence_to_push", 80)
    worker_count = cfg.get("worker_count", 8)
    cooldown_seconds = cfg.get("cooldown_seconds", 3600)
    scan_interval = cfg.get("scan_interval_seconds", 900)

    enabled_indicators = state_store.get_enabled_indicators()

    try:
        scan = await _get_or_run_scan(market, min_confidence, enabled_indicators, worker_count, scan_interval)
    except Exception as exc:
        log.error(f"Strong signal watch: scan failed for chat {chat_id} (market={market}): {exc}")
        state_store.log_scan(chat_id, "watcher", market, "failed", error=str(exc))
        return

    state_store.log_scan(chat_id, "watcher", market, "success", scanned_count=scan.get("scanned"))

    now = time.time()
    for result in scan.get("strong", []):
        cooldown_key = (chat_id, result.get("rawSymbol"), result.get("verdict"))
        last = _last_push.get(cooldown_key, 0)
        if now - last < cooldown_seconds:
            continue

        try:
            text = format_strong_signal(result)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_push[cooldown_key] = now
            state_store.log_signal(
                chat_id, "watcher", result.get("exchange", ""), result.get("symbol", "?"),
                result.get("verdict", "?"), result.get("multiTimeframe", {}).get("combinedConfidence", 0),
            )
        except Exception as exc:
            log.error(f"Strong signal watch: failed to send push to chat {chat_id}: {exc}")

    # --- pump/reversal SELL bias (see module docstring + _run_pump_reversal_check) ---
    scopes = MARKET_SCOPE_MAP[market]
    try:
        pump_events = await _get_or_run_pump_check(market, scopes, cfg)
    except Exception as exc:
        log.error(f"Pump reversal watch: check failed for chat {chat_id} (market={market}): {exc}")
        pump_events = []

    for event in pump_events:
        cooldown_key = (chat_id, event["rawSymbol"])
        last = _last_pump_push.get(cooldown_key, 0)
        if now - last < cooldown_seconds:
            continue

        try:
            text = format_pump_reversal_alert(
                pair=event["symbol"], market=market, cumulative_pct=event["cumulativePct"],
                peak_price=event["peakPrice"], current_price=event["currentPrice"],
                drop_pct=event["dropPct"], sell_pct=event["sellPct"],
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_pump_push[cooldown_key] = now
            state_store.log_signal(chat_id, "watcher", event["scope"], event["symbol"], "SELL", event["dropPct"])
        except Exception as exc:
            log.error(f"Pump reversal watch: failed to send push to chat {chat_id}: {exc}")