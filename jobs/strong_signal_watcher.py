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
from bot import state_store
from bot.formatters import format_strong_signal

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
            None,
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
        return

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
        except Exception as exc:
            log.error(f"Strong signal watch: failed to send push to chat {chat_id}: {exc}")