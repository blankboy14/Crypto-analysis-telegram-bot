"""
bot/handlers/high_alert_pairs.py

"🚨 High Alert Pairs" button - read-only, on-demand snapshot of the
SAME pool jobs/high_alert_watcher.py scans every high_alert_watch.
check_interval_seconds (see settings.yaml): every pair currently
sitting in EITHER of the two source tables that feed that pool -

  1. state_store.overextended_pairs  (pump_threshold_pct%+ cumulative
     pump, set by strong_signal_watcher.py's pump/reversal check)
  2. state_store.rsi_alert_state     (RSI pushed past high_first/
     low_first on any configured timeframe, set by
     rsi_extreme_watcher.py)

This button does NOT run the full indicator engine itself (that only
happens inside jobs/high_alert_watcher.py's own tick, on its own
schedule, and only produces a push once confidence clears
min_confidence_to_push) - it just answers "which pairs are IN the
pool right now, and why", which is otherwise invisible between one
High Alert push and the next. No extra Bitget API calls are made here
at all - both source tables are read straight from local state_store.

Same dedup rule as jobs/high_alert_watcher.py: a pair flagged by BOTH
sources at once is shown once, tagged "Pump" (the stronger/older
signal of the two) - kept identical on purpose so this button always
reflects exactly what the next High Alert scan will actually check.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.formatters import format_high_alert_pairs
from engine.signal_scanner import MARKET_SCOPE_MAP

log = logging.getLogger("crypto-telegram-bot")


def _build_pool(scopes: list[str]) -> dict[str, list[dict]]:
    """
    scope -> list of candidate dicts, same shape/precedence
    jobs/high_alert_watcher.py._run_high_alert_check builds internally,
    just exposed here instead of being consumed straight into a scan.
    """
    pool: dict[str, list[dict]] = {}

    for scope in scopes:
        candidates: dict[str, dict] = {}

        for ov in state_store.get_overextended(scope):
            candidates[ov["rawSymbol"]] = {
                "rawSymbol": ov["rawSymbol"],
                "symbol": ov["symbol"],
                "source": "pump",
                "expectedVerdict": "SELL",
                "cumulativePct": ov["cumulativePct"],
                "flaggedAt": ov["flaggedAt"],
            }

        for r in state_store.get_rsi_alert_pairs(scope):
            if r["rawSymbol"] in candidates:
                continue  # already in via pump - pump takes precedence, same as the watcher itself
            candidates[r["rawSymbol"]] = {
                "rawSymbol": r["rawSymbol"],
                "symbol": r["symbol"],
                "source": "rsi",
                "expectedVerdict": "SELL" if r["direction"] == "high" else "BUY",
                "timeframe": r["timeframe"],
                "flaggedAt": None,  # rsi_alert_state doesn't track a flagged-at timestamp
            }

        pool[scope] = sorted(candidates.values(), key=lambda c: c["symbol"])

    return pool


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    # Pool itself is global (shared across every chat, same as the
    # watcher job) - but which scope(s) to SHOW defaults to whatever
    # market this chat has Strong Signal set to, since High Alert
    # rides that same toggle. Falls back to "both" if the chat has
    # never turned Strong Signal on at all, so the button still works.
    info = state_store.get_mode_info(chat_id, "strong_signal")
    market = info["market"] or "both"
    scopes = MARKET_SCOPE_MAP.get(market, MARKET_SCOPE_MAP["both"])

    pool = _build_pool(scopes)
    await update.message.reply_text(format_high_alert_pairs(pool), parse_mode="Markdown")