"""
bot/handlers/wallet_balance.py

New button - "Wallet Balance" (Money Management add-on). Flow:
  1. Button press -> handle() sends format_wallet_balance_ask() and
     marks this chat as waiting for free-text input via
     context.chat_data["awaiting_wallet_balance"] - same "catch-all
     text handler + chat_data flag" pattern market_details.py uses for
     its "how many pairs" number prompt.
  2. The user types a plain number (e.g. "500" or "1250.50").
     bot/main.py's catch-all text handler (registered AFTER all the
     exact-label menu buttons, so it never intercepts them) sees the
     chat_data flag and calls handle_balance_text() here.
  3. handle_balance_text() validates the number and saves it via
     bot/state_store.set_wallet_balance(). From then on, every signal
     message (Search Signal, Find Strong Signal, Single Pair Analyse)
     looks it up via state_store.get_wallet_balance() and attaches a
     Money Management block computed by engine.risk_manager.

This button/handler never reads or touches a real exchange account -
the bot has no exchange trading access at all (see engine/bitget_api.py
- it's market-data only) and never places, changes, or closes any
order. The number saved here is purely a figure the user tells the
bot to size suggestions against.
"""
import logging
import math

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.formatters import (
    format_wallet_balance_ask,
    format_wallet_balance_bad_number,
    format_wallet_balance_saved,
)

log = logging.getLogger("crypto-telegram-bot")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button press entry point."""
    context.chat_data["awaiting_wallet_balance"] = True
    await update.message.reply_text(format_wallet_balance_ask(), parse_mode="Markdown")


async def handle_balance_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all text handler (routed via bot/main.py) - only does
    anything if this chat is currently waiting for a wallet balance
    (see module docstring). No-op otherwise, so it never interferes
    with anything else typed in the chat.
    """
    if not context.chat_data.pop("awaiting_wallet_balance", None):
        return

    chat_id = update.effective_chat.id
    raw_text = (update.message.text or "").strip().replace(",", "")

    try:
        balance = float(raw_text)
        if balance <= 0 or not math.isfinite(balance):
            raise ValueError
    except ValueError:
        await update.message.reply_text(format_wallet_balance_bad_number(), parse_mode="Markdown")
        # Keep waiting - give the user another shot instead of making
        # them press the button again.
        context.chat_data["awaiting_wallet_balance"] = True
        return

    state_store.set_wallet_balance(chat_id, balance)
    log.info(f"Wallet balance set for chat_id={chat_id}: {balance}")
    await update.message.reply_text(format_wallet_balance_saved(balance), parse_mode="Markdown")
