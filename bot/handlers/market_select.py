"""
bot/handlers/market_select.py

Common "Spot / Future / Both" inline keyboard used after all three of
the mode buttons that need a market choice (24/7 Market Analyse, Find
Strong Signal, Search Signal). Each of those calls ask_market() below
instead of building its own keyboard; the action that asked is encoded
directly in callback_data ("market_select:<action>:<market>"), so one
shared handle_choice() can route the answer back to whichever mode
requested it - no separate "what was pending" lookup needed.

BUILD STATUS: bot.state_store and bot.formatters are still empty
stubs. Contracts this file expects from them are documented inline
below, next to where each is called.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.formatters import format_search_signal_results
from engine.signal_scanner import scan_market

log = logging.getLogger("crypto-telegram-bot")

CALLBACK_PREFIX = "market_select"
MARKET_LABELS = {"spot": "Spot", "future": "Future", "both": "Both"}


def _keyboard(pending_action: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}:{pending_action}:{market}")
        for market, label in MARKET_LABELS.items()
    ]
    return InlineKeyboardMarkup([row])


async def ask_market(update: Update, context: ContextTypes.DEFAULT_TYPE, pending_action: str, prompt: str) -> None:
    """
    Call this instead of replying directly whenever a mode button needs
    the Spot/Future/Both choice first. `pending_action` identifies which
    mode asked (e.g. "market_analyse_on", "strong_signal_on",
    "search_signal") and is threaded straight through callback_data.
    """
    await update.message.reply_text(prompt, reply_markup=_keyboard(pending_action))


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on any 'market_select:<action>:<market>' callback_data."""
    query = update.callback_query
    await query.answer()

    try:
        _, action, market = query.data.split(":")
    except ValueError:
        log.error(f"Malformed market_select callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    # state_store.set_market_pref(chat_id, market) -> persists the
    # user's last market choice (SQLite, per database/bot_state.db in
    # the file plan) so it survives a bot restart.
    state_store.set_market_pref(chat_id, market)

    await query.edit_message_reply_markup(reply_markup=None)  # remove the buttons once answered

    if action == "market_analyse_on":
        from bot.handlers import market_analyse
        await market_analyse.start_watching(update, context, chat_id, market)
    elif action == "strong_signal_on":
        from bot.handlers import strong_signal
        await strong_signal.start_watching(update, context, chat_id, market)
    elif action == "search_signal":
        await _run_search_signal(context, chat_id, market)
    else:
        log.error(f"Unknown market_select action: {action!r}")


async def _run_search_signal(context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str) -> None:
    """
    Phase 2.3: one-shot scan -> top-3 -> done. No mode is persisted and
    there's nothing to turn off afterwards - "auto off" just means this
    doesn't keep running past the single reply.

    scan_market() is a blocking, potentially multi-minute call (see its
    own docstring in engine/signal_scanner.py), so it runs in a worker
    thread via run_in_executor rather than blocking the bot's event
    loop for every other chat while this one scans.
    """
    settings = context.bot_data.get("settings", {})
    worker_count = settings.get("search_signal", {}).get("worker_count", 8)
    # state_store.get_enabled_indicators() -> the {key: bool} map from
    # database/indicator_toggles.json. Concepts have no equivalent
    # toggle file yet, so `enabled_concepts=None` (= all on) for now.
    enabled_indicators = state_store.get_enabled_indicators()
    enabled_concepts = None

    await context.bot.send_message(
        chat_id=chat_id,
        text="🔎 Scanning the market now — this can take a few minutes...",
    )

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, scan_market, market, enabled_indicators, enabled_concepts, worker_count
    )

    # format_search_signal_results(top_picks) -> the "Signal Scan #1/#2/#3,
    # confidence /100, entry/SL/TP1-3" text block per the Phase 2.3 spec.
    text = format_search_signal_results(result["topPicks"])
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")