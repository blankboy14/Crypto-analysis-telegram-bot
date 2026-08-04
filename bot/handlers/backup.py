"""
bot/handlers/backup.py

💾 Download Database - button that zips database/bot_state.db (a
consistent snapshot, not a raw file copy - see _snapshot_database())
together with every file in logs/ and sends it back as a Telegram
document, right now, on demand.

🔁 Auto Backup ON/OFF - toggles jobs/backup_watcher.py's periodic
version of the exact same zip, sent automatically on a schedule (see
that module's docstring for why this matters specifically on Render:
free/low-tier instances can lose their local disk on a restart or
redeploy, so a backup that only ever lives on that same disk doesn't
actually protect anything - sending it AS a Telegram message means it
survives on Telegram's servers instead, recoverable even if the bot's
disk gets wiped without warning).

Every backup - whether from this button or from the periodic job - is
just a normal message in the chat, timestamped in its filename, so
they naturally stack up as historical exports too (per the explicit
request to be able to pull an OLDER export, not just the latest) -
nothing extra needed, just scroll back and download whichever one.
"""
import logging
import os
import sqlite3
import zipfile
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store

log = logging.getLogger("crypto-telegram-bot")

MODE = "auto_backup"
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRATCH_DIR = os.path.join(ROOT_DIR, "database", "_backup_scratch")


def _backup_filename() -> str:
    """day_month_year-second_minute_hour, matching the same timestamp format bot/main.py already names each run's log file with."""
    return datetime.now().strftime("%d_%m_%Y-%S_%M_%H") + ".zip"


def _snapshot_database(dest_path: str) -> None:
    """
    Uses SQLite's own backup API rather than a raw file copy - the bot
    is writing to bot_state.db continuously (every tick, every signal),
    so a plain file copy risks grabbing it mid-write and producing a
    corrupt snapshot. sqlite3's backup() takes a proper consistent
    snapshot regardless of what's happening concurrently.
    """
    source_con = sqlite3.connect(state_store.DB_PATH)
    dest_con = sqlite3.connect(dest_path)
    try:
        source_con.backup(dest_con)
    finally:
        dest_con.close()
        source_con.close()


def build_backup_zip() -> str:
    """
    Builds one zip containing a consistent snapshot of the database
    plus every log file, and returns its path. Caller is responsible
    for deleting it after use (see the try/finally at each call site
    below) - this always writes to SCRATCH_DIR, never anywhere a user
    could stumble onto it directly.
    """
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    filename = _backup_filename()
    zip_path = os.path.join(SCRATCH_DIR, filename)
    db_snapshot_path = os.path.join(SCRATCH_DIR, "bot_state.db")

    _snapshot_database(db_snapshot_path)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_snapshot_path, arcname="database/bot_state.db")
            log_dir = os.path.join(ROOT_DIR, "logs")
            if os.path.isdir(log_dir):
                for name in os.listdir(log_dir):
                    full = os.path.join(log_dir, name)
                    if os.path.isfile(full):
                        zf.write(full, arcname=f"logs/{name}")
    finally:
        os.remove(db_snapshot_path)

    return zip_path


async def handle_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("💾 Building backup (database + logs)...")
    try:
        zip_path = build_backup_zip()
    except Exception as exc:
        log.error(f"Backup: failed to build zip for chat {chat_id}: {exc}")
        await status_msg.edit_text("❌ Couldn't build the backup - check the logs for details.")
        return

    try:
        with open(zip_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id, document=f, filename=os.path.basename(zip_path),
                caption=f"💾 Database + logs backup — {os.path.basename(zip_path)}",
            )
        await status_msg.delete()
    except Exception as exc:
        log.error(f"Backup: failed to send zip for chat {chat_id}: {exc}")
        await status_msg.edit_text("❌ Backup was built but couldn't be sent - check the logs for details.")
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)


async def handle_auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    settings = context.bot_data.get("settings", {})
    interval_hours = settings.get("backup_watch", {}).get("interval_hours", 1)
    state_store.set_mode_on(chat_id, MODE, None)
    await update.message.reply_text(
        f"🔁 Auto Backup is ON - you'll get a database + logs export every {interval_hours}h automatically, "
        f"even if the bot restarts unexpectedly in between."
    )


async def handle_auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state_store.set_mode_off(chat_id, MODE)
    await update.message.reply_text("🔕 Auto Backup is OFF. You can still use 💾 Download Database any time.")