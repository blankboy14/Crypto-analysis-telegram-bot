"""
bot/handlers/strong_signal.py

Phase 2.2 - "Find 24/7 Strong Signal" / "Off 24/7 Find Signal".

ON: asks Spot/Future/Both; once picked, schedules
jobs.strong_signal_watcher.tick on this chat's job_queue every
`strong_signal_watch.scan_interval_seconds` (config/settings.yaml).
That job runs the same indicator/concept/order-flow pipeline as the
web version (via engine.signal_scanner.scan_market_above_confidence),
and pushes every pair whose confidence is at/above
`strong_signal_watch.min_confidence_to_push` (80 by default) - not
just a top-3 shortlist, since the point of this mode is "don't miss
a high-conviction setup", however many show up.

OFF: cancels that job. No market prompt for OFF.

BUILD STATUS: bot.state_store and jobs.strong_signal_watcher are still
empty stubs. Contracts expected from them are documented inline.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.handlers import market_select
from jobs import strong_signal_watcher

log = logging.getLogger("crypto-telegram-bot")

MODE = "strong_signal"
JOB_PREFIX = "strong_signal"

MARKET_LABELS = {"spot": "Spot", "future": "Future", "both": "Spot + Future"}


async def handle_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if state_store.is_mode_on(chat_id, MODE):
        await update.message.reply_text("Find 24/7 Strong Signal is already running.")
        return
    await market_select.ask_market(
        update, context,
        pending_action="strong_signal_on",
        prompt="Choose a market for Find 24/7 Strong Signal:",
    )


async def handle_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not state_store.is_mode_on(chat_id, MODE):
        await update.message.reply_text("Find 24/7 Strong Signal is already off.")
        return
    _cancel_existing_job(context, chat_id)
    state_store.set_mode_off(chat_id, MODE)
    await update.message.reply_text("🛑 Find 24/7 Strong Signal turned off.")


async def start_watching(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str) -> None:
    """Called by market_select.handle_choice once Spot/Future/Both is picked."""
    settings = context.bot_data.get("settings", {})
    watch_cfg = settings.get("strong_signal_watch", {})
    interval = watch_cfg.get("scan_interval_seconds", 900)

    _cancel_existing_job(context, chat_id)
    context.job_queue.run_repeating(
        strong_signal_watcher.tick,
        interval=interval,
        first=0,
        chat_id=chat_id,
        data={"market": market},
        name=_job_name(chat_id),
    )
    state_store.set_mode_on(chat_id, MODE, market)

    label = MARKET_LABELS.get(market, market)
    min_conf = watch_cfg.get("min_confidence_to_push", 80)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Find 24/7 Strong Signal is ON for {label}. "
             f"You'll get a trade plan here for any pair that reaches "
             f"{min_conf}%+ confidence.",
    )


def _job_name(chat_id: int) -> str:
    return f"{JOB_PREFIX}:{chat_id}"


def _cancel_existing_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(_job_name(chat_id)):
        job.schedule_removal()