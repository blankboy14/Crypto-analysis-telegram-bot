"""
jobs/backup_watcher.py

The periodic half of 🔁 Auto Backup (see bot/handlers/backup.py's
module docstring for the full "why" - in short: on Render, a bot's
local disk can vanish on a restart/redeploy, so a backup that only
lives on that same disk protects nothing; sending it AS a Telegram
message means it survives on Telegram's own servers instead).

Unlike every other watcher job in this bot, this one is GLOBAL, not
per-chat - scheduled exactly once at startup (bot/main.py), same
pattern as jobs/signal_outcome_tracker.py's tick. Each run builds ONE
backup zip and sends that SAME file to every chat currently flagged
with Auto Backup ON (state_store.get_active_chats_for_mode), instead
of rebuilding it once per chat.
"""
import logging
import os

from bot import state_store
from bot.handlers.backup import MODE, build_backup_zip

log = logging.getLogger("crypto-telegram-bot")


async def tick(context) -> None:
    chats = state_store.get_active_chats_for_mode(MODE)
    if not chats:
        return

    try:
        zip_path = build_backup_zip()
    except Exception as exc:
        log.error(f"Backup watch: failed to build zip: {exc}")
        return

    try:
        for chat_id, _market in chats:
            try:
                with open(zip_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id, document=f, filename=os.path.basename(zip_path),
                        caption=f"🔁 Scheduled backup — {os.path.basename(zip_path)}",
                    )
            except Exception as exc:
                log.error(f"Backup watch: failed to send zip to chat {chat_id}: {exc}")
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)