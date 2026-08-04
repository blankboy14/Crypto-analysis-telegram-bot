"""
jobs/rsi_extreme_watcher.py

"RSI Extreme" add-on to "Find 24/7 Strong Signal" - rides the SAME
ON/OFF toggle, scheduled alongside the other add-ons (see
bot/handlers/strong_signal.py's start_watching()). No separate button.

Normal RSI reads 30-70. This watches specifically for RSI pushing into
genuinely extreme territory - because a pair sitting there can turn
any time - in BOTH directions, with repeating checkpoints as it stays
extreme (same "checkpoint stepping" idea as jobs/meme_move_watcher.py,
just driven by the RSI reading itself instead of cumulative price %):

  Overbought: first alert once RSI >= high_first (85), then again
    every further +high_step (95/105/115/...).
  Oversold: first alert once RSI <= low_first (16), then again every
    further -low_step (11/6/1/...).

The moment a pair crosses either threshold it's ALSO added to
state_store's rsi_alert_state table, which jobs/high_alert_watcher.py
folds into its full-indicator-engine scan pool alongside the existing
pump-overextended pool - an overbought pair gets scanned looking for a
confirmed SELL setup, an oversold one for a confirmed BUY setup.

RETEST (new): a pair only ever gets a 70/30 alert if it was ACTUALLY
extreme first (i.e. it already has a rsi_alert_state row from crossing
85/16 above). Once such a pair pulls back and crosses back through
retest_high (70, for a high/overbought pair) or retest_low (30, for a
low/oversold pair), that's a genuine retest of the old line and fires
a ONE-TIME "RSI RETEST" alert - then the row is cleared, so it's
removed from the High Alert scan pool and the next extreme move on
that pair/timeframe starts a fresh cycle. A pair that just wanders
across 70/30 on its own (never having hit 85/16) has no rsi_alert_state
row and is silently ignored here - this is what keeps the 70/30 side
from spamming the way a plain 70/30 watch would.

RSI here is read from one or more timeframes
(rsi_extreme_watch.timeframes in settings.yaml, ["4h"] by default -
each tracked and alerted completely independently, so a pair can be
flagged on 4h without necessarily being flagged on another configured
timeframe and vice versa) fetched once per pair per timeframe per
check cycle - deliberately NOT the full 6-timeframe blend the main
engine uses, to
keep this cheap enough to run across the whole market on a normal
interval (this is a screening pass, not the final confirmation - that
full confirmation happens in jobs/high_alert_watcher.py once a pair
is actually in the pool).
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from engine.bitget_api import get_token_list
from engine.exchange_adapter import EXCHANGE_ADAPTERS
from engine.indicators.rsi import compute_rsi
from engine.signal_scanner import MARKET_SCOPE_MAP
from bot import state_store
from bot.formatters import format_rsi_extreme_alert, format_rsi_retest_alert
from bot.scan_executor import SCAN_EXECUTOR

log = logging.getLogger("crypto-telegram-bot")

MODE = "strong_signal"  # rides the same toggle - see module docstring

_check_cache: dict = {}
_check_locks: dict = {}
_last_push: dict = {}   # (chat_id, rawSymbol, level) -> unix ts


def _get_check_lock(market: str) -> asyncio.Lock:
    lock = _check_locks.get(market)
    if lock is None:
        lock = asyncio.Lock()
        _check_locks[market] = lock
    return lock


def _read_rsi(scope: str, raw_symbol: str, timeframe: str, limit: int, period: int):
    """One pair's latest RSI reading on `timeframe`, or None if the fetch/compute couldn't complete - never raises, so one bad pair can't take down the whole check."""
    try:
        candles = EXCHANGE_ADAPTERS[scope]["fetch"](raw_symbol, timeframe, limit)
    except Exception as exc:
        log.error(f"RSI extreme watch: candle fetch failed for {raw_symbol} ({scope}): {exc}")
        return None
    if not candles or len(candles) < period + 1:
        return None
    return compute_rsi([c["close"] for c in candles], period)


def _run_rsi_extreme_check(scopes: list, cfg: dict) -> list:
    """
    For every (pair, timeframe) in `scopes` x rsi_extreme_watch.timeframes:
    reads its RSI, updates/creates its rsi_alert_state row for THAT
    timeframe, and decides whether a checkpoint event fires this pass.
    Returns the list of checkpoint events (each fired only once - state
    is updated immediately, same pattern as meme_move_watcher.py's
    checkpoint check).

    Checking more than one timeframe means more than one candle fetch
    per pair per cycle - if that ever causes rate-limit trouble, trim
    rsi_extreme_watch.timeframes down to just ["4h"] in settings.yaml
    (no code change needed - this loop already just iterates whatever
    list is configured there).
    """
    timeframes = cfg.get("timeframes") or [cfg.get("timeframe", "4h")]  # back-compat with the old single-timeframe key
    candle_limit = cfg.get("candle_limit", 100)
    period = cfg.get("period", 14)
    high_first = cfg.get("high_first", 85.0)
    high_step = cfg.get("high_step", 10.0)
    low_first = cfg.get("low_first", 16.0)
    low_step = cfg.get("low_step", 5.0)
    retest_high = cfg.get("retest_high", 70.0)
    retest_low = cfg.get("retest_low", 30.0)
    worker_count = cfg.get("worker_count", 8)

    events = []

    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"RSI extreme watch: token list fetch failed for {scope}: {exc}")
            continue

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {}
            for token in tokens:
                for timeframe in timeframes:
                    future = pool.submit(_read_rsi, scope, token["rawSymbol"], timeframe, candle_limit, period)
                    futures[future] = (token, timeframe)

            for future in as_completed(futures):
                token, timeframe = futures[future]
                rsi_value = future.result()
                if rsi_value is None:
                    continue

                raw_symbol, symbol = token["rawSymbol"], token["symbol"]
                existing = state_store.get_rsi_alert_state(scope, raw_symbol, timeframe)

                if rsi_value >= high_first:
                    steps_past = int((rsi_value - high_first) / high_step) if high_step > 0 else 0
                    target_level = high_first + steps_past * high_step

                    fresh_direction = existing is None or existing["direction"] != "high"
                    last_announced = None if fresh_direction else existing["lastAnnouncedLevel"]

                    if last_announced is None or target_level > last_announced:
                        events.append({
                            "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol, "timeframe": timeframe,
                            "kind": "extreme", "direction": "high", "level": target_level, "rsi": rsi_value,
                        })
                        last_announced = target_level

                    state_store.upsert_rsi_alert_state(scope, raw_symbol, symbol, timeframe, "high", last_announced)

                elif rsi_value <= low_first:
                    steps_past = int((low_first - rsi_value) / low_step) if low_step > 0 else 0
                    target_level = low_first - steps_past * low_step

                    fresh_direction = existing is None or existing["direction"] != "low"
                    last_announced = None if fresh_direction else existing["lastAnnouncedLevel"]

                    if last_announced is None or target_level < last_announced:
                        events.append({
                            "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol, "timeframe": timeframe,
                            "kind": "extreme", "direction": "low", "level": target_level, "rsi": rsi_value,
                        })
                        last_announced = target_level

                    state_store.upsert_rsi_alert_state(scope, raw_symbol, symbol, timeframe, "low", last_announced)

                elif existing is not None and existing["direction"] == "high" and rsi_value <= retest_high:
                    # Was genuinely overbought (>= high_first) at some
                    # point, and has now pulled back down through the
                    # classic 70 line - a real retest. Fires ONCE, then
                    # the row is cleared (removed from the High Alert
                    # pool too) so the next 85+ push starts a fresh
                    # cycle. A pair with no rsi_alert_state row never
                    # reaches this branch at all, which is what keeps
                    # ordinary 70/30 crossings silent.
                    events.append({
                        "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol, "timeframe": timeframe,
                        "kind": "retest", "direction": "high", "level": retest_high, "rsi": rsi_value,
                    })
                    state_store.clear_rsi_alert_state(scope, raw_symbol, timeframe)

                elif existing is not None and existing["direction"] == "low" and rsi_value >= retest_low:
                    # Mirror of the above for a pair that was genuinely
                    # oversold (<= low_first) and has now bounced back
                    # up through the classic 30 line.
                    events.append({
                        "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol, "timeframe": timeframe,
                        "kind": "retest", "direction": "low", "level": retest_low, "rsi": rsi_value,
                    })
                    state_store.clear_rsi_alert_state(scope, raw_symbol, timeframe)

                # else: either coasting between the retest line and the
                # extreme line (not extreme anymore, hasn't retested
                # yet - row is left as-is, still counts toward the High
                # Alert pool), or genuinely neutral and was never
                # flagged extreme - nothing to do, nothing to clear.

    return events


async def _get_or_run_check(market: str, scopes: list, cfg: dict) -> list:
    """Same shared-cache-per-market pattern as the other watchers."""
    max_age = cfg.get("check_interval_seconds", 300)
    cached = _check_cache.get(market)
    now = time.time()
    if cached and (now - cached["ts"]) < max_age:
        return cached["result"]

    lock = _get_check_lock(market)
    async with lock:
        cached = _check_cache.get(market)
        now = time.time()
        if cached and (now - cached["ts"]) < max_age:
            return cached["result"]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(SCAN_EXECUTOR, _run_rsi_extreme_check, scopes, cfg)
        _check_cache[market] = {"result": result, "ts": time.time()}
        return result


async def tick(context) -> None:
    """The job_queue.run_repeating callback - one push-check for one chat."""
    job = context.job
    chat_id = job.chat_id
    market = (job.data or {}).get("market")

    if not state_store.is_mode_on(chat_id, MODE):
        job.schedule_removal()
        return

    if market not in MARKET_SCOPE_MAP:
        log.error(f"RSI extreme watch: unknown market {market!r} for chat {chat_id}, stopping job")
        job.schedule_removal()
        return

    settings = context.bot_data.get("settings", {})
    cfg = settings.get("rsi_extreme_watch", {})
    cooldown_seconds = cfg.get("cooldown_seconds", 3600)
    scopes = MARKET_SCOPE_MAP[market]

    try:
        events = await _get_or_run_check(market, scopes, cfg)
    except Exception as exc:
        log.error(f"RSI extreme watch: check failed for chat {chat_id} (market={market}): {exc}")
        return

    now = time.time()
    for event in events:
        cooldown_key = (chat_id, event["rawSymbol"], event["timeframe"], event["kind"], event["level"])
        if now - _last_push.get(cooldown_key, 0) < cooldown_seconds:
            continue
        try:
            is_retest = event["kind"] == "retest"
            if is_retest:
                text = format_rsi_retest_alert(
                    pair=event["symbol"], market=market, direction=event["direction"],
                    level=event["level"], rsi_value=event["rsi"], timeframe=event["timeframe"],
                )
                verdict = "RSI_RETEST_HIGH" if event["direction"] == "high" else "RSI_RETEST_LOW"
            else:
                text = format_rsi_extreme_alert(
                    pair=event["symbol"], market=market, direction=event["direction"],
                    level=event["level"], rsi_value=event["rsi"], timeframe=event["timeframe"],
                )
                verdict = "RSI_HIGH" if event["direction"] == "high" else "RSI_LOW"

            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_push[cooldown_key] = now
            state_store.log_signal(
                chat_id, "watcher", event["scope"], event["symbol"], verdict, event["rsi"], message_text=text,
            )
        except Exception as exc:
            log.error(f"RSI extreme watch: failed to send push to chat {chat_id}: {exc}")