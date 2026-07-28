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
    format_signal_scan_block,
    format_full_analysis_block,
    format_progress_message,
    format_total_pairs_message,
)
from bot.scan_executor import SCAN_EXECUTOR
from engine.bitget_api import fetch_bitget_futures_contract_config
from engine.risk_manager import compute_live_trade_plan
from engine.signal_scanner import count_pairs, scan_market

log = logging.getLogger("crypto-telegram-bot")

CALLBACK_PREFIX = "market_select"
DEPTH_CALLBACK_PREFIX = "search_signal_mode"
MARKET_LABELS = {"spot": "Spot", "future": "Future", "both": "Both"}

# Fire a progress message the first time completed/total crosses each
# of these - per the spec's "10, 25, 50, 75, 100%" milestones.
PROGRESS_MILESTONES = (10, 25, 50, 75, 100)


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
    mm_cfg = settings.get("money_management", {})
    wallet_bal = state_store.get_wallet_balance(chat_id)

    # Full-analysis pairs are no longer sent the instant each one
    # finishes. With worker_count pairs completing in parallel, firing
    # a send per pair immediately (fire-and-forget, via _send_async)
    # let several Telegram calls land in the same second, which trips
    # Telegram's per-chat flood control (see "Flood control exceeded"
    # in the logs). Those dropped sends were never retried, so the
    # numbering the user saw in the chat had gaps/out-of-order arrival
    # (network timing on the surviving sends isn't guaranteed to match
    # completion order either). Fix: collect everything first (deduped
    # by symbol - scan_market can occasionally hand back the same pair
    # twice), then send one at a time, awaited, with a small delay
    # between each, after the scan finishes.
    collected_pairs = []
    seen_symbols = set()

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
        # Buffer only - nothing is sent to Telegram here anymore.
        # scan_market() already delivers these in-order (see
        # engine/signal_scanner.py's pending_for_on_pair flush), so
        # appending as they arrive keeps the final numbering correct.
        symbol_key = result.get("rawSymbol") or result.get("symbol")
        if symbol_key in seen_symbols:
            return  # duplicate pair from the scan - skip it, don't re-number it
        seen_symbols.add(symbol_key)
        collected_pairs.append(result)

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

    if full_analysis and collected_pairs:
        # Sent one at a time, awaited in order, with a short pause
        # between each - this is what actually respects Telegram's
        # per-chat flood limit (roughly 1 message/second). Sending
        # sequentially like this also means a failed send is retried
        # right here instead of just being logged and lost.
        for i, r in enumerate(collected_pairs, start=1):
            block = format_full_analysis_block(i, r, wallet_bal, mm_cfg)
            for attempt in range(3):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=block, parse_mode="Markdown")
                    break
                except Exception as exc:
                    log.error(
                        f"Search signal: full-analysis block #{i} failed for chat {chat_id} "
                        f"(attempt {attempt + 1}/3): {exc}"
                    )
                    await asyncio.sleep(3)
            await asyncio.sleep(1.2)  # stay comfortably under Telegram's per-chat rate limit

    # Final Signal Scan #1/#2/#3, strictly by confidence (highest
    # first) - distinct from scan_market()'s own topPicks, which ranks
    # by rankScore (magnitude x confidence).
    ranked = sorted(
        result["tradeable"],
        key=lambda r: r.get("multiTimeframe", {}).get("combinedConfidence", 0),
        reverse=True,
    )
    top3 = ranked[:3]

    if not top3:
        await context.bot.send_message(
            chat_id=chat_id, text="🔎 Scan complete — no tradeable setup found right now. Try again later.",
        )
        return

    batch_ts = state_store.now_iso()
    try:
        contract_config = await loop.run_in_executor(None, fetch_bitget_futures_contract_config)
    except Exception as exc:
        log.error(f"Search signal: couldn't fetch Bitget contract config for live-trade previews: {exc}")
        contract_config = {}

    for i, r in enumerate(top3, start=1):
        confidence = r.get("multiTimeframe", {}).get("combinedConfidence", 0)
        plan = r.get("tradePlan")
        trade_id = None
        if plan:
            trade_id = state_store.record_signal_outcome_tracking(
                chat_id, "search", r.get("exchange", ""), r.get("rawSymbol", ""),
                r.get("symbol", "?"), r.get("verdict", "?"),
                plan["entry"], plan["stopLoss"], plan.get("tp1"), plan.get("tp2"), plan.get("tp3"),
                confidence=confidence, scan_label=f"Signal Scan #{i}",
            )
        live_preview = None
        if plan and r.get("exchange") == "future" and wallet_bal:
            live_preview = compute_live_trade_plan(
                wallet_bal, plan["entry"], plan["stopLoss"], plan.get("tp1"), plan.get("tp2"), plan.get("tp3"),
                r.get("verdict", "?"), contract_config.get(r.get("rawSymbol", "")),
            )
        block_text = format_signal_scan_block(i, r, wallet_bal, mm_cfg, trade_id=trade_id, live_preview=live_preview)
        state_store.log_signal(
            chat_id, "search", r.get("exchange", ""), r.get("symbol", "?"),
            r.get("verdict", "?"), confidence,
            message_text=block_text, batch_ts=batch_ts, scan_index=i,
        )
        # Sent as its OWN message rather than joined with the other 2 -
        # each block now includes a full Money Management section, and
        # 3 joined together can exceed Telegram's ~4096-char hard limit
        # ("Message is too long"), which previously failed the ENTIRE
        # result silently (the person got nothing at all, not even a
        # partial result). One block alone is comfortably under that
        # limit even with Money Management included.
        await context.bot.send_message(chat_id=chat_id, text=block_text, parse_mode="Markdown")