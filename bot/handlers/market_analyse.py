"""
bot/handlers/market_analyse.py

Phase 2.1 - "24/7 Market Analyse" / "24/7 Off Market Analyse".

ON: asks Spot/Future/Both (via market_select.ask_market); once picked,
schedules jobs.volume_spike_watcher.tick on this chat's job_queue every
`volume_spike_watch.poll_interval_seconds` (config/settings.yaml).
That job is what actually fetches ticks, detects a pair suddenly
spiking, applies BTC's separate tighter threshold, and pushes the
alert message - none of that logic lives here.

OFF: cancels that job. No market prompt for OFF - off is just off.

BUILD STATUS: bot.state_store and jobs.volume_spike_watcher are still
empty stubs. Contracts expected from them are documented inline.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.handlers import market_select
from jobs import volume_spike_watcher

log = logging.getLogger("crypto-telegram-bot")

MODE = "market_analyse"
JOB_PREFIX = "volume_spike"

MARKET_LABELS = {"spot": "Spot", "future": "Future", "both": "Spot + Future"}


async def handle_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # state_store.is_mode_on(chat_id, mode) -> bool, read from the
    # per-chat toggle state (SQLite, database/bot_state.db).
    if state_store.is_mode_on(chat_id, MODE):
        await update.message.reply_text("24/7 Market Analyse is already running.")
        return
    await market_select.ask_market(
        update, context,
        pending_action="market_analyse_on",
        prompt="Choose a market for 24/7 Market Analyse:",
    )


async def handle_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not state_store.is_mode_on(chat_id, MODE):
        await update.message.reply_text("24/7 Market Analyse is already off.")
        return
    _cancel_existing_job(context, chat_id)
    # state_store.set_mode_off(chat_id, mode) -> persists OFF so a bot
    # restart doesn't silently resurrect a watcher the user turned off.
    state_store.set_mode_off(chat_id, MODE)
    await update.message.reply_text("🔕 24/7 Market Analyse turned off.")


async def start_watching(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str) -> None:
    """Called by market_select.handle_choice once Spot/Future/Both is picked."""
    settings = context.bot_data.get("settings", {})
    interval = settings.get("volume_spike_watch", {}).get("poll_interval_seconds", 15)

    _cancel_existing_job(context, chat_id)
    context.job_queue.run_repeating(
        volume_spike_watcher.tick,
        interval=interval,
        first=0,
        chat_id=chat_id,
        data={"market": market},
        name=_job_name(chat_id),
    )
    # state_store.set_mode_on(chat_id, mode, market) -> persists ON +
    # which market, so a bot restart can re-schedule this job instead
    # of the user silently losing their running watcher.
    state_store.set_mode_on(chat_id, MODE, market)

    label = MARKET_LABELS.get(market, market)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ 24/7 Market Analyse is ON for {label}. "
             f"You'll get an alert here whenever a pair suddenly spikes.",
    )


def _job_name(chat_id: int) -> str:
    return f"{JOB_PREFIX}:{chat_id}"


def _cancel_existing_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(_job_name(chat_id)):
        job.schedule_removal()