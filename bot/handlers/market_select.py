"""
bot/handlers/market_select.py

Common "Spot / Future / Both" inline keyboard used after all four of
the mode buttons that need a market choice (24/7 Market Analyse, Find
Strong Signal, Search Signal, Single Pair Analyse). Each of those calls
ask_market() below instead of building its own keyboard; the action
that asked is encoded directly in callback_data
("market_select:<action>:<market>"), so one shared handle_choice() can
route the answer back to whichever mode requested it - no separate
"what was pending" lookup needed.

Phase 2.3 upgrade: Search Signal is no longer a single step from here.
Picking a market now leads into _ask_analysis_depth() (total pair
count message, then a "Full Analysis" / "Skip Analysis Detail" inline
choice), handled by handle_analysis_depth_choice() below once the user
answers that second prompt. See that function's docstring for the full
flow.

ISOLATION NOTE (issue raised: the 3 main modes must never take each
other down): every blocking scan call in this file goes through
bot.scan_executor.SCAN_EXECUTOR (a small, dedicated thread pool) via
run_in_executor, NOT the asyncio default executor - so a heavy Search
Signal scan can't starve threads the 24/7 watchers or any other chat's
button presses need. Every scan call is also wrapped in try/except so
one failed/slow scan reports an error to that chat instead of ever
propagating up and affecting anything else.
"""
import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.formatters import (
    format_final_signal_scan,
    format_pair_detail_batch,
    format_pair_detail_block,
    format_progress_message,
    format_total_pairs_message,
)
from bot.scan_executor import SCAN_EXECUTOR
from engine.signal_scanner import count_pairs, scan_market

log = logging.getLogger("crypto-telegram-bot")

CALLBACK_PREFIX = "market_select"
DEPTH_CALLBACK_PREFIX = "search_signal_mode"
MARKET_LABELS = {"spot": "Spot", "future": "Future", "both": "Both"}

# Fire a progress message the first time completed/total crosses each
# of these - per the spec's "10, 25, 50, 75, 100%" milestones.
PROGRESS_MILESTONES = (10, 25, 50, 75, 100)

# How many per-pair detail blocks to bundle into one Telegram message
# in "Full Analysis" mode - keeps a multi-hundred-pair scan from
# trying to send one message per pair (which would trip Telegram's
# flood limits and could stall/drop messages).
DETAIL_BATCH_SIZE = 8


def _keyboard(pending_action: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}:{pending_action}:{market}")
        for market, label in MARKET_LABELS.items()
    ]
    return InlineKeyboardMarkup([row])


async def ask_market(update: Update, context: ContextTypes.DEFAULT_TYPE, pending_action: str, prompt: str) -> None:
    """
    Call this instead of replying directly whenever a mode button needs
    the Spot/Future/Both choice first. `pending_action` identifies which
    mode asked (e.g. "market_analyse_on", "strong_signal_on",
    "search_signal") and is threaded straight through callback_data.
    """
    await update.message.reply_text(prompt, reply_markup=_keyboard(pending_action))


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on any 'market_select:<action>:<market>' callback_data."""
    query = update.callback_query
    await query.answer()

    try:
        _, action, market = query.data.split(":")
    except ValueError:
        log.error(f"Malformed market_select callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    # Persists the user's last market choice (SQLite, database/bot_state.db)
    # so it survives a bot restart.
    state_store.set_market_pref(chat_id, market)

    await query.edit_message_reply_markup(reply_markup=None)  # remove the buttons once answered

    if action == "market_analyse_on":
        from bot.handlers import market_analyse
        await market_analyse.start_watching(update, context, chat_id, market)
    elif action == "strong_signal_on":
        from bot.handlers import strong_signal
        await strong_signal.start_watching(update, context, chat_id, market)
    elif action == "search_signal":
        await _ask_analysis_depth(context, chat_id, market)
    elif action == "single_pair_analyse":
        from bot.handlers import single_pair_analyse
        await single_pair_analyse.ask_for_pair_name(context, chat_id, market)
    elif action == "market_details":
        from bot.handlers import market_details
        await market_details.ask_detail_type(context, chat_id, market)
    else:
        log.error(f"Unknown market_select action: {action!r}")


def _depth_keyboard(market: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔎 Full Analysis", callback_data=f"{DEPTH_CALLBACK_PREFIX}:{market}:full"),
        InlineKeyboardButton("⏭ Skip Analysis Detail", callback_data=f"{DEPTH_CALLBACK_PREFIX}:{market}:skip"),
    ]])


async def _ask_analysis_depth(context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str) -> None:
    """
    First step of the upgraded Search Signal flow: report the total
    pair count for the chosen market, then ask whether the user wants
    the full per-pair breakdown streamed as the scan runs, or just the
    progress % updates followed by the final top-3.
    """
    try:
        loop = asyncio.get_running_loop()
        counts = await loop.run_in_executor(SCAN_EXECUTOR, count_pairs, market)
    except Exception as exc:
        log.error(f"Search signal: count_pairs failed for market={market!r}: {exc}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Couldn't reach the exchange to count pairs right now. Please try Search Signal again shortly.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=format_total_pairs_message(market, counts["total"], counts["perScope"]),
        parse_mode="Markdown",
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Do you want per-pair analysis details as the scan runs, or just the final result?",
        reply_markup=_depth_keyboard(market),
    )


async def handle_analysis_depth_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on 'search_signal_mode:<market>:<full|skip>' callback_data."""
    query = update.callback_query
    await query.answer()

    try:
        _, market, depth = query.data.split(":")
    except ValueError:
        log.error(f"Malformed search_signal_mode callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    await _run_search_signal(context, chat_id, market, full_analysis=(depth == "full"))


async def _run_search_signal(context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str, full_analysis: bool) -> None:
    """
    Phase 2.3: one-shot scan -> (optional per-pair detail as it runs)
    -> top-3 sorted strictly by confidence, sent last -> done. No mode
    is persisted and there's nothing to turn off afterwards - "auto
    off" just means this doesn't keep running past this single reply.

    scan_market() is a blocking, potentially multi-minute call, so it
    runs on bot.scan_executor.SCAN_EXECUTOR rather than blocking the
    bot's event loop (or the shared asyncio default executor other
    chats/modes might need) while this one scans. Progress/per-pair
    callbacks run on that worker thread, so they hop back onto this
    coroutine's event loop via asyncio.run_coroutine_threadsafe to
    actually send messages.
    """
    settings = context.bot_data.get("settings", {})
    worker_count = settings.get("search_signal", {}).get("worker_count", 8)
    enabled_indicators = state_store.get_enabled_indicators()
    enabled_concepts = None

    await context.bot.send_message(
        chat_id=chat_id,
        text="🔎 Scanning the market now — this can take a few minutes...",
    )

    loop = asyncio.get_running_loop()
    fired_milestones = set()
    detail_buffer = []
    pair_counter = {"n": 0}

    def _send_async(coro) -> None:
        """Schedule an async send from this worker thread; log if it fails."""
        fut = asyncio.run_coroutine_threadsafe(coro, loop)

        def _log_if_failed(f):
            exc = f.exception()
            if exc:
                log.error(f"Search signal: failed to deliver message to chat {chat_id}: {exc}")

        fut.add_done_callback(_log_if_failed)

    def on_total(total: int) -> None:
        pass  # total pair count was already shown before the user chose Full/Skip

    def on_progress(completed: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(completed / total * 100)
        for milestone in PROGRESS_MILESTONES:
            if pct >= milestone and milestone not in fired_milestones:
                fired_milestones.add(milestone)
                _send_async(context.bot.send_message(
                    chat_id=chat_id,
                    text=format_progress_message(completed, total, milestone),
                    parse_mode="Markdown",
                ))

    def on_pair(result: dict) -> None:
        if not full_analysis:
            return
        pair_counter["n"] += 1
        detail_buffer.append(format_pair_detail_block(pair_counter["n"], result))
        if len(detail_buffer) >= DETAIL_BATCH_SIZE:
            batch_text = format_pair_detail_batch(detail_buffer)
            detail_buffer.clear()
            _send_async(context.bot.send_message(chat_id=chat_id, text=batch_text, parse_mode="Markdown"))

    try:
        result = await loop.run_in_executor(
            SCAN_EXECUTOR, lambda: scan_market(
                market, enabled_indicators, enabled_concepts, worker_count=worker_count,
                on_pair=on_pair, on_progress=on_progress, on_total=on_total,
            )
        )
    except Exception as exc:
        log.error(f"Search signal: scan failed for chat {chat_id} (market={market}): {exc}")
        state_store.log_scan(chat_id, "search", market, "failed", error=str(exc))
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ The scan hit an unexpected error and couldn't finish. This is isolated to this "
                 "Search Signal run only - your other running modes (if any) are unaffected. Please try again.",
        )
        return

    state_store.log_scan(chat_id, "search", market, "success", scanned_count=result.get("scanned"))

    # Flush any leftover buffered detail blocks that didn't fill a full batch.
    if full_analysis and detail_buffer:
        await context.bot.send_message(
            chat_id=chat_id, text=format_pair_detail_batch(detail_buffer), parse_mode="Markdown",
        )

    # Final Signal Scan #1/#2/#3, strictly by confidence (highest
    # first) - distinct from scan_market()'s own topPicks, which ranks
    # by rankScore (magnitude x confidence).
    ranked = sorted(
        result["tradeable"],
        key=lambda r: r.get("multiTimeframe", {}).get("combinedConfidence", 0),
        reverse=True,
    )
    top3 = ranked[:3]
    for r in top3:
        state_store.log_signal(
            chat_id, "search", r.get("exchange", ""), r.get("symbol", "?"),
            r.get("verdict", "?"), r.get("multiTimeframe", {}).get("combinedConfidence", 0),
        )
    text = format_final_signal_scan(top3)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")