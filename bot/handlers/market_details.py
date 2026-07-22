"""
bot/handlers/market_details.py

New button - "Market Details". Flow:
  1. Button press -> handle() asks Spot/Future/Both via the shared
     market_select.ask_market() (same inline keyboard every other mode
     uses).
  2. Once a market is picked, market_select.handle_choice() routes here
     (action="market_details") -> ask_detail_type() shows a second
     inline keyboard: All Pairs / Higher Movers / Lower Movers / Top by
     Volume (a typed number).
  3. handle_type_choice() fires on that answer. "all"/"higher"/"lower"
     run immediately. "number" instead asks the user to type a whole
     number (free text) and marks this chat as waiting for it via
     context.chat_data["awaiting_market_details_number"] - same
     "catch-all text handler + chat_data flag" pattern
     single_pair_analyse.py uses for its pair-name prompt. bot/main.py's
     text router calls handle_number_text() here after checking that
     flag.
  4. _run_listing() does the actual work: pulls every pair in the
     chosen market via engine.bitget_api.get_token_list() (already
     cached there for a few seconds, so this is cheap even right after
     another mode just fetched it), sorts/filters per the chosen type,
     and sends the result as a header message + batched pair-line
     messages (same batching idea as market_select.py's Full Analysis
     detail blocks, to stay well under Telegram's per-message length
     and flood limits for a multi-hundred-pair market).

This is a read-only, on-demand listing - unlike market_analyse.py /
strong_signal.py it doesn't persist any ON/OFF mode or schedule a
job_queue watcher.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.formatters import (
    format_market_details_ask_number,
    format_market_details_bad_number,
    format_market_details_batch,
    format_market_details_empty,
    format_market_details_fetch_error,
    format_market_details_header,
    format_market_details_line,
)
from bot.handlers import market_select
from bot.scan_executor import SCAN_EXECUTOR
from engine.bitget_api import get_token_list
from engine.signal_scanner import MARKET_SCOPE_MAP

log = logging.getLogger("crypto-telegram-bot")

CALLBACK_PREFIX = "market_details_type"
SCOPE_TAGS = {"bitget-spot": "Spot", "bitget-futures": "Future"}

DETAIL_TYPES = {
    "all": "📋 All Pairs",
    "higher": "🚀 Higher Movers",
    "lower": "🔻 Lower Movers",
    "number": "🔢 Top by Volume (pick a number)",
}

# Rows per Telegram message for the pair-line listing - keeps each
# message comfortably under the 4096-char limit even for long pair
# names, and avoids tripping flood limits on a multi-hundred-pair "all".
BATCH_SIZE = 40


def _keyboard(market: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}:{market}:{dtype}")]
        for dtype, label in DETAIL_TYPES.items()
    ]
    return InlineKeyboardMarkup(rows)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button press entry point."""
    await market_select.ask_market(
        update, context,
        pending_action="market_details",
        prompt="Choose a market for Market Details:",
    )


async def ask_detail_type(context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str) -> None:
    """Called by market_select.handle_choice() once Spot/Future/Both is picked."""
    await context.bot.send_message(
        chat_id=chat_id,
        text="What would you like to see?",
        reply_markup=_keyboard(market),
    )


async def handle_type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on 'market_details_type:<market>:<all|higher|lower|number>' callback_data."""
    query = update.callback_query
    await query.answer()

    try:
        _, market, detail_type = query.data.split(":")
    except ValueError:
        log.error(f"Malformed market_details_type callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)

    if detail_type == "number":
        context.chat_data["awaiting_market_details_number"] = market
        await context.bot.send_message(chat_id=chat_id, text=format_market_details_ask_number(market), parse_mode="Markdown")
        return

    if detail_type not in ("all", "higher", "lower"):
        log.error(f"Unknown market_details_type: {detail_type!r}")
        return

    await _run_listing(context, chat_id, market, detail_type, top_n=None)


async def handle_number_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all text handler (routed via bot/main.py) - only does
    anything if this chat is currently waiting for a "how many pairs"
    number (see module docstring). No-op otherwise, so it never
    interferes with anything else typed in the chat.
    """
    market = context.chat_data.pop("awaiting_market_details_number", None)
    if not market:
        return

    chat_id = update.effective_chat.id
    raw_text = (update.message.text or "").strip()
    try:
        top_n = int(raw_text)
        if top_n <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(format_market_details_bad_number(), parse_mode="Markdown")
        return

    await _run_listing(context, chat_id, market, "number", top_n=top_n)


def _fetch_pairs(market: str) -> list:
    """
    Blocking - runs on SCAN_EXECUTOR. Merges every scope in `market`
    into one flat list of (token dict, scope) pairs. get_token_list()
    caches its response for a few seconds, so calling it once per scope
    here is cheap.
    """
    scopes = MARKET_SCOPE_MAP[market]
    merged = []
    for scope in scopes:
        tokens = get_token_list(scope)["tokens"]
        merged.extend((token, scope) for token in tokens)
    return merged


async def _run_listing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str, detail_type: str, top_n: int | None) -> None:
    loop = asyncio.get_running_loop()
    try:
        pairs = await loop.run_in_executor(SCAN_EXECUTOR, _fetch_pairs, market)
    except Exception as exc:
        log.error(f"Market details: token list fetch failed for market={market!r}: {exc}")
        await context.bot.send_message(chat_id=chat_id, text=format_market_details_fetch_error())
        return

    def _vol(entry):
        return entry[0].get("usdtVolume24h") or 0

    def _chg(entry):
        return entry[0].get("change24h")

    if detail_type == "all":
        selected = sorted(pairs, key=_vol, reverse=True)
    elif detail_type == "higher":
        # Positive movers only, highest % first, down towards 0.
        selected = sorted(
            (p for p in pairs if isinstance(_chg(p), (int, float)) and _chg(p) > 0),
            key=_chg, reverse=True,
        )
    elif detail_type == "lower":
        # Negative movers only, lowest (most negative) % first, up towards 0.
        selected = sorted(
            (p for p in pairs if isinstance(_chg(p), (int, float)) and _chg(p) < 0),
            key=_chg,
        )
    else:  # "number" - top N by 24h traded volume
        selected = sorted(pairs, key=_vol, reverse=True)[: top_n or 0]

    total_matching = len(selected)
    if total_matching == 0:
        await context.bot.send_message(
            chat_id=chat_id, text=format_market_details_empty(market, detail_type), parse_mode="Markdown",
        )
        return

    show_scope_tag = market == "both"
    header = format_market_details_header(market, detail_type, shown=len(selected), total=total_matching)
    await context.bot.send_message(chat_id=chat_id, text=header, parse_mode="Markdown")

    lines = []
    for i, (token, scope) in enumerate(selected, start=1):
        lines.append(format_market_details_line(
            i, token["symbol"], token.get("lastPrice"), token.get("change24h"),
            token.get("usdtVolume24h"), scope_tag=SCOPE_TAGS.get(scope) if show_scope_tag else None,
        ))
        if len(lines) >= BATCH_SIZE:
            await context.bot.send_message(chat_id=chat_id, text=format_market_details_batch(lines), parse_mode="Markdown")
            lines = []

    if lines:
        await context.bot.send_message(chat_id=chat_id, text=format_market_details_batch(lines), parse_mode="Markdown")