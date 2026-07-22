"""
bot/handlers/start.py

/start command - greets the user and shows the Phase 1.2 vertical
main menu (bot.keyboards.main_menu_keyboard()). No state to set up
here - the menu buttons are stateless text; each one's own handler
figures out what to do when pressed.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import main_menu_keyboard

log = logging.getLogger("crypto-telegram-bot")

WELCOME_TEXT = (
    "👋 *Welcome to the Crypto Signal Bot*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "📊 *24/7 Market Analyse*\n"
    "Alerts you the moment a pair suddenly spikes in volume or price.\n\n"
    "🔥 *Find 24/7 Strong Signal*\n"
    "Keeps scanning non-stop and pushes high-confidence (80%+) trade "
    "setups the moment they appear.\n\n"
    "🔎 *Search Signal*\n"
    "One-shot scan — gives you the current top 3 setups right now.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Not sure what a button does? Tap *ℹ️ Help* below anytime for a "
    "full breakdown of every option.\n\n"
    "Choose an option below to get started 👇"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info(f"/start from chat {update.effective_chat.id}")
    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )