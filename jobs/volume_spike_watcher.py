"""
jobs/volume_spike_watcher.py

Phase 2.1 - "24/7 Market Analyse". Scheduled by
bot/handlers/market_analyse.py as a per-chat repeating job
(context.job_queue.run_repeating(tick, interval=poll_interval_seconds,
chat_id=..., data={"market": ...})) - this module owns everything that
happens on each tick: fetching prices, detecting a sudden move, applying
BTC's own tighter threshold, and pushing the alert.

HOW THE SPIKE CHECK WORKS: a single ticker snapshot only tells you a
pair's price NOW, not whether it just moved - "suddenly spiked" needs a
comparison against a price from shortly before. So this module keeps a
small in-memory rolling price history per (exchange, raw_symbol),
shared across every chat's tick (the market itself is the same for
everyone watching it - there's no reason each chat should keep its own
copy). Each tick compares the current price against the sample closest
to `poll_interval_seconds` old and flags a move past threshold. Only
the ALERT COOLDOWN is per-chat (so a chat that just turned this mode on
still gets told about an active spike, while a chat already alerted
about it recently doesn't get spammed every tick while the price stays
elevated).

This in-memory state (history + cooldowns) resets on a bot restart -
that's fine here, a missed alert or two right after a restart is a
minor cost, and it's what bot/state_store.py's SQLite-backed mode
ON/OFF state is for (that part does survive a restart - see
bot/main.py's startup re-scheduling via
state_store.get_active_chats_for_mode()).
"""
import logging
import time

from engine.bitget_api import get_token_list
from engine.signal_scanner import MARKET_SCOPE_MAP
from bot import state_store
from bot.formatters import format_volume_spike_alert

log = logging.getLogger("crypto-telegram-bot")

MODE = "market_analyse"

# --- shared, module-level state (see docstring above for why) ---

# (exchange, raw_symbol) -> [(timestamp, price), ...], oldest first,
# trimmed to _HISTORY_MAX_AGE_SECONDS on every append.
_price_history: dict[tuple[str, str], list[tuple[float, float]]] = {}

# (chat_id, raw_symbol, direction) -> unix ts of the last alert sent
# for that exact pair+direction, for this chat's cooldown.
_last_alert: dict[tuple[int, str, str], float] = {}

# Keep enough history that a slightly-late tick can still find a
# reasonable baseline to compare against.
_HISTORY_MAX_AGE_SECONDS = 180


def _trim_history(key: tuple, now: float) -> None:
    samples = _price_history.get(key)
    if not samples:
        return
    cutoff = now - _HISTORY_MAX_AGE_SECONDS
    # Samples are appended in time order, so the first ones still
    # within the cutoff mark where the stale prefix ends.
    i = 0
    while i < len(samples) and samples[i][0] < cutoff:
        i += 1
    if i:
        del samples[:i]


def _find_baseline(key: tuple, now: float, target_age: float):
    """
    Picks the sample whose age is closest to `target_age` (normally
    `poll_interval_seconds`) rather than strictly "oldest available" -
    so an irregular tick schedule (bot hiccup, a chat joining partway
    through another chat's cycle, etc.) still gets a sensible,
    consistent comparison point. Returns None if there's no sample old
    enough yet to compare against (this pair/scope is still "warming
    up" - not enough history yet).
    """
    samples = _price_history.get(key)
    if not samples:
        return None
    candidates = [(abs((now - ts) - target_age), price) for ts, price in samples if now - ts >= target_age * 0.5]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _detect_moves(scopes: list, cfg: dict) -> list:
    """
    Refreshes the shared price history for every pair in `scopes` and
    returns every pair currently past its spike threshold. Cheap to
    call from several chats' ticks in a row - get_token_list() already
    caches Bitget's response for a few seconds (engine/bitget_api.py),
    so this doesn't re-hit the exchange on every single chat's tick.
    """
    now = time.time()
    poll_interval = cfg.get("poll_interval_seconds", 15)
    normal_threshold = cfg.get("spike_pct_threshold", 20.0)
    btc_threshold = cfg.get("btc_spike_pct_threshold", 5.0)
    btc_symbol = cfg.get("btc_symbol", "BTCUSDT")

    events = []
    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Volume spike watch: token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            price = token.get("lastPrice")
            if not price:
                continue

            key = (scope, raw_symbol)
            baseline = _find_baseline(key, now, poll_interval)

            history = _price_history.setdefault(key, [])
            history.append((now, price))
            _trim_history(key, now)

            if baseline is None or baseline == 0:
                continue  # still warming up for this pair - nothing to compare against yet

            pct_change = (price - baseline) / baseline * 100
            threshold = btc_threshold if raw_symbol.upper() == btc_symbol.upper() else normal_threshold

            if abs(pct_change) >= threshold:
                events.append({
                    "exchange": scope,
                    "rawSymbol": raw_symbol,
                    "symbol": token["symbol"],
                    "lastPrice": price,
                    "pctChange": pct_change,
                    "direction": "up" if pct_change > 0 else "down",
                })

    return events


async def tick(context) -> None:
    """The job_queue.run_repeating callback - one poll for one chat."""
    job = context.job
    chat_id = job.chat_id
    market = (job.data or {}).get("market")

    # Self-heal: if this chat's mode got turned off but the job somehow
    # survived (shouldn't normally happen - handlers cancel it on OFF -
    # but a stale job outliving a state change is cheap to guard against).
    if not state_store.is_mode_on(chat_id, MODE):
        job.schedule_removal()
        return

    scopes = MARKET_SCOPE_MAP.get(market)
    if not scopes:
        log.error(f"Volume spike watch: unknown market {market!r} for chat {chat_id}, stopping job")
        job.schedule_removal()
        return

    settings = context.bot_data.get("settings", {})
    cfg = settings.get("volume_spike_watch", {})
    cooldown_seconds = cfg.get("cooldown_seconds", 900)

    try:
        events = _detect_moves(scopes, cfg)
    except Exception as exc:
        log.error(f"Volume spike watch: tick failed for chat {chat_id}: {exc}")
        return

    now = time.time()
    for event in events:
        cooldown_key = (chat_id, event["rawSymbol"], event["direction"])
        last = _last_alert.get(cooldown_key, 0)
        if now - last < cooldown_seconds:
            continue

        try:
            text = format_volume_spike_alert(
                pair=event["symbol"],
                last_price=event["lastPrice"],
                pct_change=event["pctChange"],
                direction=event["direction"],
                market=market,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_alert[cooldown_key] = now
        except Exception as exc:
            log.error(f"Volume spike watch: failed to send alert to chat {chat_id}: {exc}")