"""
bot/handlers/clean_chat.py

The 🧹 Clean Chat button. Purely a cosmetic reset for the Telegram
CHAT VIEW - it never touches database/bot_state.db (signals_log,
signal_outcomes, wallet_balance, mode_state, etc.) or logs/*.txt.
Nothing tracked, tuned, or recorded there is affected; only the
messages visible on screen are removed.

Message ids to delete come from bot/message_tracker.py's in-memory
per-chat log - see that module's docstring for how it's filled (both
the bot's own sends and the user's own taps/typed text).

Flow:
  1. Send a countdown message, editing it once a second: 10 -> 0.
  2. At 0, delete every OTHER tracked message for this chat.
  3. Flip the countdown message itself to a short "Cleaned" line,
     wait a couple seconds so it's actually readable, then delete
     that too - so nothing at all is left behind, per the request
     that this is only for how the chat LOOKS.

Telegram note: bots can delete their own messages any time, and (in a
private chat like this one) the other party's incoming messages too -
so both "system" and "user" messages are deletable here. A handful of
very old messages can still fail (already deleted by the user,
outside Telegram's own deletion window, etc.) - those are just
skipped, never treated as a hard failure.
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import message_tracker

log = logging.getLogger("crypto-telegram-bot")

COUNTDOWN_SECONDS = 10


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    countdown_msg = await update.message.reply_text(
        f"🧹 Cleaning chat in {COUNTDOWN_SECONDS}s..."
    )

    for remaining in range(COUNTDOWN_SECONDS - 1, -1, -1):
        await asyncio.sleep(1)
        try:
            label = f"🧹 Cleaning chat in {remaining}s..." if remaining > 0 else "🧹 Cleaning chat now..."
            await countdown_msg.edit_text(label)
        except Exception as exc:
            # Never let one bad edit kill the countdown - just keep ticking.
            log.error(f"Clean chat: countdown edit failed for chat {chat_id}: {exc}")

    message_ids = message_tracker.pop_all(chat_id)
    # The countdown message is tracked too (reply_text goes through
    # TrackingBot) - pull it out and handle it last, once everything
    # else is already gone.
    message_ids = [mid for mid in message_ids if mid != countdown_msg.message_id]

    deleted, skipped = 0, 0
    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:
            # Expected sometimes (already deleted, too old, etc.) -
            # not worth a log line per miss.
            skipped += 1

    try:
        await countdown_msg.edit_text(f"✅ Cleaned {deleted} message(s).")
    except Exception as exc:
        log.error(f"Clean chat: final edit failed for chat {chat_id}: {exc}")

    await asyncio.sleep(2)
    try:
        await countdown_msg.delete()
    except Exception as exc:
        log.error(f"Clean chat: could not remove its own countdown message for chat {chat_id}: {exc}")

    if skipped:
        log.info(f"Clean chat: chat {chat_id} - deleted {deleted}, {skipped} could not be removed (already gone / too old).")