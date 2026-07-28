# jobs/heartbeat.py
#
# Anti-sleep / status heartbeat for free-tier hosts like Render, whose
# free Web Services can spin down after a period with no incoming HTTP
# traffic. The health-check server (bot/main.py) being hit by an
# uptime monitor already keeps the process itself awake at the HTTP
# level - this job is a separate, VISIBLE confirmation on top of that:
# once an hour it sends a Telegram message to HEARTBEAT_CHAT_ID (set
# in .env) proving the process never stopped, plus a live snapshot of
# everything actually running 24/7 right now - so "is it really still
# awake" never has to be guessed at from silence.
#
# Runs GLOBALLY on a single repeating job (like signal_outcome_tracker
# below it in bot/main.py) - not per-chat, since it reports on the
# whole process's health, not any one chat's own toggles.

import logging
from datetime import datetime, timezone

from telegram.ext import ContextTypes

from bot import state_store

log = logging.getLogger("crypto-telegram-bot")

# Set once, the moment this module is first imported (i.e. at process
# startup in bot/main.py) - every tick's "uptime" is measured from here.
_START_TIME = datetime.now(timezone.utc)


def _format_uptime() -> str:
    delta = datetime.now(timezone.utc) - _START_TIME
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


HEARTBEAT_MESSAGE_TTL_SECONDS = 60  # how long an automatic heartbeat push stays visible before auto-deleting


def build_heartbeat_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    The actual status snapshot - factored out so both the automatic
    hourly push (tick(), below) and the on-demand "🖥 Server
    Information" -> "Server Status" button (bot/handlers/
    server_information.py) show IDENTICAL, always-live data. Nothing
    here is a cached/stale copy - every call recomputes fresh from
    state_store and the process's own start time.
    """
    settings = context.bot_data.get("settings", {})
    now = datetime.now(timezone.utc)

    outcome_cfg = settings.get("signal_outcome_tracker", {})
    outcome_tracker_on = outcome_cfg.get("enabled", True)
    open_outcomes = len(state_store.get_open_signal_outcomes(limit=100000)) if outcome_tracker_on else 0

    market_analyse_chats = len(state_store.get_active_chats_for_mode("market_analyse"))
    strong_signal_chats = len(state_store.get_active_chats_for_mode("strong_signal"))

    lines = [
        "💓 *Bot Heartbeat — Still Alive!*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🕐 *Time:* {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"⏱ *Uptime since last restart:* {_format_uptime()}",
        "",
        "*✅ 24/7 Systems Currently Running:*",
        "🌐 Health-check server — keeping Render awake",
    ]
    if outcome_tracker_on:
        lines.append(f"📈 Signal Outcome Tracker — {open_outcomes} open signal(s) being tracked")
    else:
        lines.append("📈 Signal Outcome Tracker — disabled in settings.yaml")
    lines.append(f"📊 24/7 Market Analyse — ON in {market_analyse_chats} chat(s)")
    lines.append(f"🔥 Find 24/7 Strong Signal — ON in {strong_signal_chats} chat(s)")
    lines.append("")
    lines.append("No sleep detected — everything's running normally ✅")
    return "\n".join(lines)


async def _delete_later(context: ContextTypes.DEFAULT_TYPE) -> None:
    """job_queue.run_once callback - deletes ONE automatic heartbeat push after HEARTBEAT_MESSAGE_TTL_SECONDS. Only ever removes the message from the chat; nothing about the heartbeat data itself is touched (it's recomputed fresh every tick anyway, never stored per-message)."""
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass  # already deleted/too old/etc - never worth erroring over


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs every `heartbeat.interval_seconds` (config/settings.yaml,
    default 1 hour). Silently does nothing if no chat_id is configured
    (HEARTBEAT_CHAT_ID unset in .env) rather than erroring every tick.

    The push still happens every hour same as before (still useful
    proof the scheduler itself is alive and ticking on time) - it just
    no longer sits in the chat forever. It's deleted
    HEARTBEAT_MESSAGE_TTL_SECONDS later automatically. For a persistent,
    on-demand look at the exact same data any time, see "🖥 Server
    Information" -> "Server Status" (bot/handlers/server_information.py),
    which never auto-deletes since it's something the person asked for.
    """
    settings = context.bot_data.get("settings", {})
    cfg = settings.get("heartbeat", {})
    chat_id = cfg.get("chat_id")
    if not chat_id:
        return

    text = build_heartbeat_text(context)

    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        context.job_queue.run_once(
            _delete_later, when=HEARTBEAT_MESSAGE_TTL_SECONDS,
            data={"chat_id": chat_id, "message_id": sent.message_id},
        )
    except Exception:
        log.error("Heartbeat: failed to send status message", exc_info=True)