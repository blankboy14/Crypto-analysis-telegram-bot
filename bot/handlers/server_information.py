"""
bot/handlers/server_information.py

"🖥 Server Information" - the on-demand replacement for what used to
be two auto-pushed, ever-accumulating messages in the chat:

  - "💓 Bot Heartbeat" (jobs/heartbeat.py) used to push every hour
  - "✅ Keep-Alive Check" (jobs/keepalive.py) used to push every ~10 min

Both jobs still run exactly as before under the hood (still genuinely
useful - proof the scheduler/self-ping loop is alive and on time) -
they just auto-delete their own chat messages now
(HEARTBEAT_MESSAGE_TTL_SECONDS / KEEPALIVE_MESSAGE_TTL_SECONDS) instead
of piling up forever. This button shows the exact same live data any
time it's asked for, and - since it's something the person explicitly
requested - it does NOT auto-delete.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from jobs import heartbeat, keepalive

log = logging.getLogger("crypto-telegram-bot")

CALLBACK_PREFIX = "server_info"


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Server Status", callback_data=f"{CALLBACK_PREFIX}:status")],
        [InlineKeyboardButton("🌐 Keep-Alive Status", callback_data=f"{CALLBACK_PREFIX}:keepalive")],
    ])


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button press entry point for '🖥 Server Information'."""
    await update.message.reply_text(
        "🖥 *Server Information*\n\nChoose what to check:", parse_mode="Markdown", reply_markup=_keyboard(),
    )


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on 'server_info:<status|keepalive>' callback_data."""
    query = update.callback_query
    await query.answer()

    try:
        _, choice = query.data.split(":")
    except ValueError:
        log.error(f"Malformed server_info callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)

    if choice == "status":
        text = heartbeat.build_heartbeat_text(context)
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        return

    if choice == "keepalive":
        text = keepalive.build_keepalive_text_from_state()
        if text is None:
            text = "🌐 *Keep-Alive Status*\n\nNo check has completed yet - the very first one runs shortly after startup."
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        return

    log.error(f"Unknown server_info choice: {choice!r}")