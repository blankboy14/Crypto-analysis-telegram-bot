"""
bot/handlers/status.py

The two new "... - Status" buttons - read-only reports, no toggling or
market prompt needed. Just pull the relevant history from
bot.state_store and format it.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.formatters import (
    format_market_analyse_status, format_strong_signal_status, format_search_signal_status,
)

log = logging.getLogger("crypto-telegram-bot")


async def handle_market_analyse_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    status = state_store.get_market_analyse_status(chat_id)
    await update.message.reply_text(format_market_analyse_status(status), parse_mode="Markdown")


async def handle_strong_signal_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    status = state_store.get_strong_signal_status(chat_id)
    watch_cfg = context.bot_data.get("settings", {}).get("strong_signal_watch", {})
    status["minConfidenceToPush"] = watch_cfg.get("min_confidence_to_push", 80)
    status["scanIntervalSeconds"] = watch_cfg.get("scan_interval_seconds", 900)
    await update.message.reply_text(format_strong_signal_status(status), parse_mode="Markdown")


async def handle_search_signal_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    status = state_store.get_search_signal_status(chat_id)
    await update.message.reply_text(format_search_signal_status(status), parse_mode="Markdown")