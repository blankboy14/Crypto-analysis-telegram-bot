"""
bot/handlers/single_pair_analyse.py

New button - "Single Pair Analyse". Flow:
  1. Button press -> ask_for_pair_name() asks Spot/Future/Both via the
     shared market_select.ask_market() (same inline keyboard every
     other mode uses).
  2. Once a market is picked, market_select.handle_choice() routes
     here (action="single_pair_analyse") -> ask_for_pair_name() sends
     "type a pair name" and marks this chat as waiting for free-text
     input via context.chat_data["awaiting_pair_for"].
  3. The user types a pair name as a plain message (e.g. "BTC/USDT",
     "btcusdt", "Btc-Usdt" - case/format don't matter, see
     _normalize_symbol below). bot/main.py's catch-all text handler
     (registered AFTER all the exact-label menu buttons, so it never
     intercepts them) sees the chat_data flag and calls
     handle_pair_text() here.
  4. The typed name is matched against the chosen market's token
     list(s), then run through the exact same engine.signal_scanner.
     analyze_one_pair() pipeline every other scan uses - so a single-
     pair result is the same trustworthy report as a full scan would
     give that pair, not a separate/lighter analysis path.

If "Both" was chosen and the pair exists on both Spot and Futures,
both get analyzed and reported separately (they're genuinely different
instruments with different price action/liquidity).
"""
import asyncio
import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.formatters import format_single_pair_not_found, format_single_pair_report
from bot.handlers import market_select
from bot.scan_executor import SCAN_EXECUTOR
from engine.bitget_api import get_token_list
from engine.news_service import get_news_feed
from engine.signal_scanner import MARKET_SCOPE_MAP, analyze_one_pair

log = logging.getLogger("crypto-telegram-bot")

MARKET_LABELS = {"spot": "Spot", "future": "Future", "both": "Spot + Future"}
SCOPE_LABELS = {"bitget-spot": "Spot", "bitget-futures": "Future"}


def _normalize_symbol(text: str) -> str:
    """
    "BTC/USDT", "btc/usdt", "Btc-Usdt", "BTCUSDT" all need to match the
    same token - strip everything but letters/digits and uppercase, so
    the comparison is purely against the exchange's raw slug
    ("BTCUSDT"), which is itself always upper-alnum-only.
    """
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def _find_token(scope: str, wanted: str) -> dict | None:
    try:
        tokens = get_token_list(scope)["tokens"]
    except Exception as exc:
        log.error(f"Single pair analyse: token list fetch failed for {scope}: {exc}")
        return None
    for token in tokens:
        if token["rawSymbol"].upper() == wanted:
            return token
    return None


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button press entry point."""
    await market_select.ask_market(
        update, context,
        pending_action="single_pair_analyse",
        prompt="Choose a market for Single Pair Analyse:",
    )


async def ask_for_pair_name(context: ContextTypes.DEFAULT_TYPE, chat_id: int, market: str) -> None:
    """Called by market_select.handle_choice() once Spot/Future/Both is picked."""
    context.chat_data["awaiting_pair_for"] = market
    label = MARKET_LABELS.get(market, market)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔍 {label} selected. Now type the pair name, e.g. `BTC/USDT` or `BTCUSDT` "
             f"(uppercase/lowercase and the slash don't matter).",
        parse_mode="Markdown",
    )


async def handle_pair_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all text handler in bot/main.py - only actually does anything
    if this chat is currently waiting for a pair name (see module
    docstring). Silently ignores every other message so it never
    interferes with anything else typed in the chat.
    """
    market = context.chat_data.pop("awaiting_pair_for", None)
    if not market:
        return

    chat_id = update.effective_chat.id
    raw_text = update.message.text or ""
    wanted = _normalize_symbol(raw_text)
    if not wanted:
        await update.message.reply_text("That doesn't look like a pair name. Please try Single Pair Analyse again.")
        return

    scopes = MARKET_SCOPE_MAP.get(market, [])
    matches = []
    for scope in scopes:
        token = _find_token(scope, wanted)
        if token:
            matches.append((scope, token))

    if not matches:
        checked = ", ".join(SCOPE_LABELS.get(s, s) for s in scopes) or market
        await update.message.reply_text(format_single_pair_not_found(raw_text.strip(), checked))
        return

    await update.message.reply_text(f"🔍 Analysing `{matches[0][1]['symbol']}`, please wait...", parse_mode="Markdown")

    enabled_indicators = state_store.get_enabled_indicators()
    enabled_concepts = None
    loop = asyncio.get_running_loop()

    try:
        news_items = await loop.run_in_executor(SCAN_EXECUTOR, lambda: get_news_feed(limit=100))
    except Exception as exc:
        log.error(f"Single pair analyse: news feed fetch failed, continuing without news context: {exc}")
        news_items = []

    for scope, token in matches:
        try:
            result = await loop.run_in_executor(
                SCAN_EXECUTOR,
                lambda scope=scope, token=token: analyze_one_pair(
                    token["rawSymbol"], token["symbol"], scope,
                    token.get("usdtVolume24h", 0), enabled_indicators, enabled_concepts, news_items,
                    change_24h=token.get("change24h"),
                ),
            )
        except Exception as exc:
            log.error(f"Single pair analyse: analysis failed for {token['rawSymbol']} ({scope}): {exc}")
            await update.message.reply_text(
                f"⚠️ Analysis of `{token['symbol']}` ({SCOPE_LABELS.get(scope, scope)}) "
                f"hit an unexpected error. Please try again.",
                parse_mode="Markdown",
            )
            continue

        await context.bot.send_message(chat_id=chat_id, text=format_single_pair_report(result), parse_mode="Markdown")
        if result.get("tradeable"):
            state_store.log_signal(
                chat_id, "single_pair", scope, result.get("symbol", "?"),
                result.get("verdict", "?"), result.get("multiTimeframe", {}).get("combinedConfidence", 0),
            )