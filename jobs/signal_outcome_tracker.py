# jobs/signal_outcome_tracker.py
#
# Answers "did my signals actually work?" automatically instead of
# someone tallying it up by hand (which is exactly how this feature
# started - the person doing this manually for their last 10 signals).
#
# Runs GLOBALLY on a single repeating job (not per-chat like
# volume_spike_watcher/strong_signal_watcher) - it just walks every
# still-open row in signal_outcomes across every chat and checks real
# candle data against each one's own Entry/SL/TP1-3 levels.
#
# LIMITATION (documented, not silently glossed over): each check looks
# back CANDLE_LIMIT 5-minute candles (~8 hours) from now, not all the
# way back to when the signal was opened. A signal still open longer
# than that AND checked less often than every ~8 hours could have a
# level cross missed in the gap. In normal operation (this job ticking
# every few minutes) that's a very wide safety margin - it would take
# a multi-hour bot outage to actually lose a data point.

import logging
import time
from datetime import datetime, timezone

from engine.bitget_api import fetch_bitget_spot_candles, fetch_bitget_futures_candles
from bot import state_store
from bot.formatters import format_signal_outcome_update

log = logging.getLogger("crypto-telegram-bot")

CANDLE_INTERVAL = "5m"
CANDLE_LIMIT = 100  # ~8 hours of 5m candles


def _opened_at_ms(opened_at_iso: str) -> int:
    return int(datetime.fromisoformat(opened_at_iso).timestamp() * 1000)


def _get_candles(cache: dict, scope: str, raw_symbol: str):
    """Per-tick cache so several open outcomes on the same pair share one candle fetch instead of one each."""
    key = (scope, raw_symbol)
    if key in cache:
        return cache[key]
    try:
        if scope == "bitget-futures":
            candles = fetch_bitget_futures_candles(raw_symbol, CANDLE_INTERVAL, limit=CANDLE_LIMIT)
        else:
            candles = fetch_bitget_spot_candles(raw_symbol, CANDLE_INTERVAL, limit=CANDLE_LIMIT)
    except Exception as exc:
        log.error(f"Signal outcome tracker: candle fetch failed for {raw_symbol} ({scope}): {exc}")
        candles = None
    cache[key] = candles
    return candles


def _check_outcome(outcome: dict, candles: list, risk_cfg: dict):
    """
    Walks candles chronologically from opened_at forward, checking
    each one's high/low against whichever levels haven't been cleared
    yet. Returns (new_status, new_highest_tp_hit, closed: bool,
    new_current_stop: float | None) for the FIRST new thing that
    happened, or None if nothing new this tick. Status only ever moves
    forward, never back.

    STOP TRAILING (fixes "reached TP1/TP2, then reversed all the way
    back to the ORIGINAL stop"): the stop level checked against price
    starts at outcome["currentStop"] (== stop_loss until moved) and is
    itself moved forward the moment a target is reached this same walk
    - to breakeven (entry) after TP1, then up to TP1 after TP2 - per
    risk_management.move_stop_to_breakeven_after_tp1 /
    move_stop_to_tp1_after_tp2 in settings.yaml. That moved level is
    what a LATER candle's SL check compares against, and is returned
    as new_current_stop so state_store persists it for the next tick.
    Worst case once TP1 is reached is now a scratch, not a full loss.

    Within a single candle, if BOTH the stop and a still-open target
    look touched, the stop is assumed to have come first - a
    conservative read (a wick could genuinely have gone either way
    first at 5m resolution), erring toward not overstating a win.
    """
    opened_ms = _opened_at_ms(outcome["openedAt"])
    verdict = outcome["verdict"]
    entry = outcome["entry"]
    stop = outcome["currentStop"]
    tps = [outcome.get("tp1"), outcome.get("tp2"), outcome.get("tp3")]
    highest = outcome["highestTpHit"]

    move_to_breakeven = risk_cfg.get("move_stop_to_breakeven_after_tp1", True)
    move_to_tp1 = risk_cfg.get("move_stop_to_tp1_after_tp2", True)

    relevant = sorted((c for c in candles if c["time"] >= opened_ms), key=lambda c: c["time"])

    stop_moved = False
    for candle in relevant:
        high, low = candle["high"], candle["low"]

        if verdict == "BUY":
            sl_hit = low <= stop
        else:
            sl_hit = high >= stop

        if sl_hit:
            return "sl_hit", highest, True, (stop if stop_moved else None)

        while highest < 3 and tps[highest] is not None:
            target = tps[highest]
            reached = (high >= target) if verdict == "BUY" else (low <= target)
            if not reached:
                break
            highest += 1
            if highest == 1 and move_to_breakeven:
                stop = entry
                stop_moved = True
            elif highest == 2 and move_to_tp1 and tps[0] is not None:
                stop = tps[0]
                stop_moved = True

        if highest >= 3:
            return "tp3_hit", 3, True, (stop if stop_moved else None)

    if highest > outcome["highestTpHit"]:
        return f"tp{highest}_hit", highest, False, (stop if stop_moved else None)
    return None


async def tick(context) -> None:
    """
    One global pass over every still-open tracked signal. Scheduled
    once at startup (bot/main.py) via job_queue.run_repeating - NOT
    tied to any chat's 24/7 toggle, since this tracks signals already
    sent regardless of whether that chat's watcher is still on.
    """
    open_outcomes = state_store.get_open_signal_outcomes()
    if not open_outcomes:
        return

    settings = context.bot_data.get("settings", {})
    risk_cfg = settings.get("risk_management", {})

    candle_cache: dict = {}

    for outcome in open_outcomes:
        candles = _get_candles(candle_cache, outcome["scope"], outcome["rawSymbol"])
        if not candles:
            state_store.touch_signal_outcome_checked(outcome["id"])
            continue

        try:
            change = _check_outcome(outcome, candles, risk_cfg)
        except Exception as exc:
            log.error(f"Signal outcome tracker: check failed for outcome {outcome['id']}: {exc}")
            state_store.touch_signal_outcome_checked(outcome["id"])
            continue

        if change is None:
            state_store.touch_signal_outcome_checked(outcome["id"])
            continue

        new_status, new_highest_tp_hit, closed, new_current_stop = change
        state_store.update_signal_outcome(
            outcome["id"], new_status, new_highest_tp_hit, closed=closed, new_current_stop=new_current_stop,
        )

        try:
            text = format_signal_outcome_update(outcome, new_status, new_highest_tp_hit, new_current_stop)
            await context.bot.send_message(chat_id=outcome["chatId"], text=text, parse_mode="Markdown")
        except Exception as exc:
            log.error(f"Signal outcome tracker: failed to notify chat {outcome['chatId']}: {exc}")