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
# back CANDLE_LIMIT 1-minute candles (~8 hours) from now, not all the
# way back to when the signal was opened. A signal still open longer
# than that AND checked less often than every ~8 hours could have a
# level cross missed in the gap. In normal operation (this job ticking
# every few minutes) that's a very wide safety margin - it would take
# a multi-hour bot outage to actually lose a data point.
#
# FIXED (was the actual root cause of "Not yet" never updating, for
# EVERY signal, since before Trade IDs even existed): CANDLE_INTERVAL
# used to be "5m", which isn't a valid granularity key on EITHER
# Bitget endpoint (engine/bitget_api.py only maps 1m/15m/30m/1h/4h/1d/
# 1w/1M - there's no 5m). That silently made every candle fetch return
# None (no exception, so nothing ever got logged), which tick() reads
# as "nothing to check yet" and just skips - forever, for every pair.
# 1m is not just closer to what the person actually watches on their
# own chart - it's also simply the finest granularity Bitget offers,
# and now a value the API actually accepts.

import logging
import time
from datetime import datetime, timezone

from engine.bitget_api import fetch_bitget_spot_candles, fetch_bitget_futures_candles
from engine.risk_manager import pnl_at_price
from bot import state_store
from bot.formatters import format_signal_outcome_update, format_entry_arrived_update

log = logging.getLogger("crypto-telegram-bot")

CANDLE_INTERVAL = "1m"
CANDLE_LIMIT = 480  # ~8 hours of 1m candles


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _entry_arrived(outcome: dict, candles: list) -> int | None:
    """
    Returns the time (ms) of the first candle at/after the signal was
    ORIGINALLY GENERATED (openedAt) whose high/low range includes the
    entry price, or None if entry hasn't been touched since then.

    Anchored to openedAt, not activatedAt: entry is set to the market
    price at the moment the signal was generated, so by definition
    price is already sitting almost exactly on entry right then - the
    person activating a trade 10/30/60 minutes later shouldn't reset
    that clock and pretend nothing happened in between. Checking from
    openedAt forward means activating a trade whose price already
    moved through entry (and maybe further, into TP/SL territory)
    correctly reflects that catch-up in the very next tick, instead of
    sitting stuck on "Waiting Entry" forever because price already
    left the entry level before the button was pressed.
    """
    start_ms = _iso_to_ms(outcome["openedAt"])
    entry = outcome["entry"]
    for candle in sorted((c for c in candles if c["time"] >= start_ms), key=lambda c: c["time"]):
        if candle["low"] <= entry <= candle["high"]:
            return candle["time"]
    return None


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
    new_current_stop: float | None, event_time_ms: int) for the FIRST
    new thing that happened, or None if nothing new this tick. Status
    only ever moves forward, never back.

    event_time_ms is the candle time the event actually happened at -
    used by tick() to tell a genuinely-live update (just happened,
    this tick) apart from a catch-up one (happened earlier, before/
    shortly after activation, only just discovered because this is
    the first tick to check it) so the Telegram message can word it
    correctly instead of implying something "just" happened hours ago.

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
    first at 1m resolution), erring toward not overstating a win.
    """
    start_ms = _iso_to_ms(outcome["entryArrivedAt"]) if outcome.get("entryArrivedAt") else _iso_to_ms(outcome["openedAt"])
    verdict = outcome["verdict"]
    entry = outcome["entry"]
    stop = outcome["currentStop"]
    tps = [outcome.get("tp1"), outcome.get("tp2"), outcome.get("tp3")]
    highest = outcome["highestTpHit"]

    move_to_breakeven = risk_cfg.get("move_stop_to_breakeven_after_tp1", True)
    move_to_tp1 = risk_cfg.get("move_stop_to_tp1_after_tp2", True)

    relevant = sorted((c for c in candles if c["time"] >= start_ms), key=lambda c: c["time"])

    stop_moved = False
    last_tp_event_time = None
    for candle in relevant:
        high, low = candle["high"], candle["low"]

        if verdict == "BUY":
            sl_hit = low <= stop
        else:
            sl_hit = high >= stop

        if sl_hit:
            return "sl_hit", highest, True, (stop if stop_moved else None), candle["time"]

        while highest < 3 and tps[highest] is not None:
            target = tps[highest]
            reached = (high >= target) if verdict == "BUY" else (low <= target)
            if not reached:
                break
            highest += 1
            last_tp_event_time = candle["time"]
            if highest == 1 and move_to_breakeven:
                stop = entry
                stop_moved = True
            elif highest == 2 and move_to_tp1 and tps[0] is not None:
                stop = tps[0]
                stop_moved = True

        if highest >= 3:
            return "tp3_hit", 3, True, (stop if stop_moved else None), candle["time"]

    if highest > outcome["highestTpHit"]:
        return f"tp{highest}_hit", highest, False, (stop if stop_moved else None), last_tp_event_time
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

        if outcome["entryStatus"] == "waiting":
            try:
                arrived_ms = _entry_arrived(outcome, candles)
            except Exception as exc:
                log.error(f"Signal outcome tracker: entry check failed for trade {outcome.get('tradeId')}: {exc}")
                state_store.touch_signal_outcome_checked(outcome["id"])
                continue
            if arrived_ms is None:
                state_store.touch_signal_outcome_checked(outcome["id"])
                continue
            arrived_iso = _ms_to_iso(arrived_ms)
            state_store.mark_entry_arrived(outcome["id"], arrived_iso)
            outcome = dict(outcome, entryStatus="arrived", entryArrivedAt=arrived_iso)
            entry_is_catchup = arrived_ms < _iso_to_ms(outcome["activatedAt"])
            try:
                await context.bot.send_message(
                    chat_id=outcome["chatId"],
                    text=format_entry_arrived_update(outcome, entry_is_catchup, touch_time_ms=arrived_ms),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                log.error(f"Signal outcome tracker: failed to notify entry-arrival for chat {outcome['chatId']}: {exc}")
            # Fall through to also run the SL/TP check this same tick,
            # against the candles already fetched above - no need to
            # wait a whole extra poll cycle just because entry only
            # just arrived this tick.

        try:
            change = _check_outcome(outcome, candles, risk_cfg)
        except Exception as exc:
            log.error(f"Signal outcome tracker: check failed for outcome {outcome['id']}: {exc}")
            state_store.touch_signal_outcome_checked(outcome["id"])
            continue

        if change is None:
            state_store.touch_signal_outcome_checked(outcome["id"])
            continue

        new_status, new_highest_tp_hit, closed, new_current_stop, event_time_ms = change
        state_store.update_signal_outcome(
            outcome["id"], new_status, new_highest_tp_hit, closed=closed, new_current_stop=new_current_stop,
        )

        # "List with Balance" - this trade locked a slice of the
        # Wallet Balance as margin when activated (see
        # bot/handlers/trade_information.py's set_trade_balance_mode).
        # A TP1/TP2 touch (not terminal) only shows FLOATING P/L at
        # that level - the position's still open, so nothing about the
        # actual Wallet Balance changes yet. Only a terminal close
        # (sl_hit or tp3_hit, closed=True) actually credits margin +
        # P/L back - that's the one moment a real trade would too.
        balance_result = None
        if outcome.get("balanceMode") == "list_with_balance" and outcome.get("marginLocked"):
            if new_status == "sl_hit":
                touch_price = new_current_stop if new_current_stop is not None else outcome["currentStop"]
            elif new_status == "tp3_hit":
                touch_price = outcome.get("tp3")
            elif new_status == "tp1_hit":
                touch_price = outcome.get("tp1")
            elif new_status == "tp2_hit":
                touch_price = outcome.get("tp2")
            else:
                touch_price = None

            if touch_price is not None:
                pnl_usdt, pnl_pct = pnl_at_price(
                    outcome["positionNotional"], outcome["entry"], outcome["marginLocked"],
                    outcome["verdict"], touch_price,
                )
                if pnl_usdt is not None:
                    new_balance = None
                    if closed:
                        new_balance = state_store.adjust_wallet_balance(
                            outcome["chatId"], outcome["marginLocked"] + pnl_usdt,
                        )
                        log.info(
                            f"List with Balance: chat_id={outcome['chatId']} trade_id={outcome['tradeId']} "
                            f"closed, credited {outcome['marginLocked'] + pnl_usdt:.2f} USDT back "
                            f"(margin {outcome['marginLocked']:.2f} + P/L {pnl_usdt:.2f})"
                        )
                    balance_result = {"usdt": pnl_usdt, "pct": pnl_pct, "realized": closed, "newBalance": new_balance}

        is_catchup = event_time_ms is not None and event_time_ms < _iso_to_ms(outcome["activatedAt"])
        try:
            text = format_signal_outcome_update(
                outcome, new_status, new_highest_tp_hit, new_current_stop, is_catchup,
                touch_time_ms=event_time_ms, balance_result=balance_result,
            )
            await context.bot.send_message(chat_id=outcome["chatId"], text=text, parse_mode="Markdown")
        except Exception as exc:
            log.error(f"Signal outcome tracker: failed to notify chat {outcome['chatId']}: {exc}")