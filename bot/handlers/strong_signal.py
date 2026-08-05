"""
bot/handlers/strong_signal.py

Phase 2.2 - "Find 24/7 Strong Signal" / "Off 24/7 Find Signal".

ON: asks Spot/Future/Both; once picked, schedules
jobs.strong_signal_watcher.tick on this chat's job_queue every
`strong_signal_watch.scan_interval_seconds` (config/settings.yaml).
That job runs the same indicator/concept/order-flow pipeline as the
web version (via engine.signal_scanner.scan_market_above_confidence),
and pushes ONE signal per tick - the single best-ranked pair whose
confidence is at/above `strong_signal_watch.min_confidence_to_push`
(80 by default) and isn't still in this chat's per-pair cooldown.
This is deliberately one-at-a-time (not a burst of everything that
qualifies at once): every candidate gets the full itemized breakdown
(indicator performance, concept performance, order flow, funding
rate, open interest, trade reason, confidence), and other qualifying
pairs simply get their own turn on a later tick instead of being
dumped into the chat together.

OFF: cancels that job. No market prompt for OFF.

BUILD STATUS: bot.state_store and jobs.strong_signal_watcher are still
empty stubs. Contracts expected from them are documented inline.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.handlers import market_select
from jobs import strong_signal_watcher, meme_move_watcher, high_alert_watcher, rsi_extreme_watcher

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
    early_watch_interval = watch_cfg.get("early_watch_interval_seconds", 240)

    _cancel_existing_job(context, chat_id)
    context.job_queue.run_repeating(
        strong_signal_watcher.tick,
        interval=interval,
        first=0,
        chat_id=chat_id,
        data={"market": market},
        name=_job_name(chat_id),
    )
    # Separate, faster-cycling job - see jobs/strong_signal_watcher.py's
    # early_watch_tick() docstring for why this can't just be folded
    # into the main tick() above (it needs its OWN schedule, not just a
    # cache-freshness window inside the same callback).
    context.job_queue.run_repeating(
        strong_signal_watcher.early_watch_tick,
        interval=early_watch_interval,
        first=early_watch_interval,
        chat_id=chat_id,
        data={"market": market},
        name=_early_watch_job_name(chat_id),
    )
    # "Meme/Alt Coin Move" add-on - rides this same toggle (see
    # jobs/meme_move_watcher.py's module docstring), own schedule since
    # its check interval is independent of the two above.
    meme_watch_cfg = settings.get("meme_move_watch", {})
    meme_move_interval = meme_watch_cfg.get("check_interval_seconds", 60)
    context.job_queue.run_repeating(
        meme_move_watcher.tick,
        interval=meme_move_interval,
        first=meme_move_interval,
        chat_id=chat_id,
        data={"market": market},
        name=_meme_move_job_name(chat_id),
    )
    # "High Alert Pair Analyse" add-on - also rides this same toggle
    # (see jobs/high_alert_watcher.py's module docstring): runs the
    # FULL indicator engine, but only against pairs already flagged
    # overextended by the pump-reversal check above.
    high_alert_cfg = settings.get("high_alert_watch", {})
    high_alert_interval = high_alert_cfg.get("check_interval_seconds", 300)
    context.job_queue.run_repeating(
        high_alert_watcher.tick,
        interval=high_alert_interval,
        first=high_alert_interval,
        chat_id=chat_id,
        data={"market": market},
        name=_high_alert_job_name(chat_id),
    )
    # "RSI Extreme" add-on - also rides this same toggle (see
    # jobs/rsi_extreme_watcher.py's module docstring): 80/90/100
    # overbought and 25/20/15 oversold checkpoints, feeding pairs into
    # the SAME High Alert Pair pool as the pump/reversal add-on above.
    rsi_extreme_cfg = settings.get("rsi_extreme_watch", {})
    rsi_extreme_interval = rsi_extreme_cfg.get("check_interval_seconds", 300)
    context.job_queue.run_repeating(
        rsi_extreme_watcher.tick,
        interval=rsi_extreme_interval,
        # Deliberately offset from high_alert_interval above by 90s, not
        # equal to it - both used to fire their FIRST (and then every
        # subsequent) run at the exact same moment, since both defaulted
        # to the same 300s interval used for both `first` and `interval`.
        # Two heavy jobs spiking memory/CPU simultaneously (high_alert's
        # full 6-timeframe engine scan + rsi_extreme's threaded
        # whole-market candle fetches) is what crashed a free-tier
        # instance - staggering the phase spreads that load out instead.
        first=rsi_extreme_interval + 90,
        chat_id=chat_id,
        data={"market": market},
        name=_rsi_extreme_job_name(chat_id),
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


def _early_watch_job_name(chat_id: int) -> str:
    return f"{JOB_PREFIX}_early_watch:{chat_id}"


def _meme_move_job_name(chat_id: int) -> str:
    return f"{JOB_PREFIX}_meme_move:{chat_id}"


def _high_alert_job_name(chat_id: int) -> str:
    return f"{JOB_PREFIX}_high_alert:{chat_id}"


def _rsi_extreme_job_name(chat_id: int) -> str:
    return f"{JOB_PREFIX}_rsi_extreme:{chat_id}"


def _cancel_existing_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(_job_name(chat_id)):
        job.schedule_removal()
    for job in context.job_queue.get_jobs_by_name(_early_watch_job_name(chat_id)):
        job.schedule_removal()
    for job in context.job_queue.get_jobs_by_name(_meme_move_job_name(chat_id)):
        job.schedule_removal()
    for job in context.job_queue.get_jobs_by_name(_high_alert_job_name(chat_id)):
        job.schedule_removal()
    for job in context.job_queue.get_jobs_by_name(_rsi_extreme_job_name(chat_id)):
        job.schedule_removal()
