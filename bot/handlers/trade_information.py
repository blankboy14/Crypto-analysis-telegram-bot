"""
bot/handlers/trade_information.py

"📈 Trade Information/Active" - replaces the old "Signal Outcomes"
button. Every signal (Search Signal, Find 24/7 Strong Signal, Single
Pair Analyse) now carries its own unique Trade ID, but nothing is
tracked in the background until the person explicitly activates that
ID here - see bot/state_store.py's Trade ID system and
jobs/signal_outcome_tracker.py for the tracking side.

Flow:
  1. Button press -> handle() shows an inline keyboard: "Trade
     Information" / "Active a Trade".
  2. "Active a Trade" -> handle_menu_choice() marks this chat as
     awaiting a Trade ID (context.chat_data["awaiting_trade_activate_id"])
     and asks for it. handle_activate_text() (routed via bot/main.py's
     catch-all text handler, same "chat_data flag" pattern as
     wallet_balance.py) picks up the next plain-text message, looks the
     ID up SCOPED TO THIS CHAT, and activates it if found.
  3. "Trade Information" -> handle_menu_choice() shows the summary
     counts plus a second inline keyboard: "See Last 12 Trade" / "See
     Active Trade By ID" / "Remove Trade".
     - "See Last 12 Trade" runs immediately.
     - "See Active Trade By ID" / "Remove Trade" each ask for a Trade
       ID the same chat_data-flag way as step 2, then
       handle_by_id_text() / handle_remove_text() do the lookup/delete.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot import state_store
from bot.formatters import (
    format_active_balance_confirmed,
    format_active_balance_error,
    format_active_balance_prompt,
    format_active_balance_spot_unsupported,
    format_last_trades_list,
    format_remove_trade_prompt,
    format_trade_activated,
    format_trade_already_active,
    format_trade_by_id_not_found,
    format_trade_by_id_prompt,
    format_trade_detail_block,
    format_trade_id_bad_input,
    format_trade_id_not_found,
    format_trade_id_prompt,
    format_trade_information_summary,
    format_trade_removed,
    format_trade_remove_not_found,
)
from engine.bitget_api import fetch_bitget_futures_contract_config, get_token_list
from engine.risk_manager import compute_live_trade_plan

log = logging.getLogger("crypto-telegram-bot")

MENU_CALLBACK_PREFIX = "trade_info_menu"
ACTION_CALLBACK_PREFIX = "trade_info_action"
BALMODE_CALLBACK_PREFIX = "trade_info_balmode"


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Trade Information", callback_data=f"{MENU_CALLBACK_PREFIX}:info")],
        [InlineKeyboardButton("✅ Active a Trade", callback_data=f"{MENU_CALLBACK_PREFIX}:active")],
    ])


def _actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 See Last 12 Trade", callback_data=f"{ACTION_CALLBACK_PREFIX}:last12")],
        [InlineKeyboardButton("🔍 See Active Trade By ID", callback_data=f"{ACTION_CALLBACK_PREFIX}:by_id")],
        [InlineKeyboardButton("🗑 Remove Trade", callback_data=f"{ACTION_CALLBACK_PREFIX}:remove")],
    ])


def _balance_mode_keyboard(trade_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Active Balance", callback_data=f"{BALMODE_CALLBACK_PREFIX}:balance:{trade_id}")],
    ])


async def _get_live_price(rawSymbol: str, scope: str) -> float | None:
    """
    Current price for `rawSymbol`, from the same 10-second ticker
    cache already used elsewhere in the bot (engine.bitget_api.get_token_list)
    - no dedicated extra API call in the common case. scope is
    'spot' or 'future' (matches signal_outcomes.scope).
    """
    exchange = "bitget-futures" if scope == "future" else "bitget-spot"
    loop = asyncio.get_running_loop()
    token_list = await loop.run_in_executor(None, get_token_list, exchange)
    for token in token_list.get("tokens", []):
        if token.get("rawSymbol") == rawSymbol:
            return token.get("lastPrice")
    return None


def _clean_id(raw_text: str) -> str | None:
    """A Trade ID is always a plain digit string - strips whitespace/backticks a person might paste along with it, returns None if what's left isn't all digits."""
    cleaned = (raw_text or "").strip().strip("`").replace(" ", "")
    return cleaned if cleaned.isdigit() else None


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button press entry point for '📈 Trade Information/Active'."""
    await update.message.reply_text(
        "📈 *Trade Information/Active*\n\nChoose an option:",
        parse_mode="Markdown",
        reply_markup=_menu_keyboard(),
    )


async def handle_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on 'trade_info_menu:<info|active>' callback_data."""
    query = update.callback_query
    await query.answer()

    try:
        _, choice = query.data.split(":")
    except ValueError:
        log.error(f"Malformed trade_info_menu callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)

    if choice == "active":
        context.chat_data["awaiting_trade_activate_id"] = True
        await context.bot.send_message(chat_id=chat_id, text=format_trade_id_prompt(), parse_mode="Markdown")
        return

    if choice == "info":
        stats = state_store.get_trade_summary(chat_id)
        await context.bot.send_message(
            chat_id=chat_id, text=format_trade_information_summary(stats), parse_mode="Markdown",
            reply_markup=_actions_keyboard(),
        )
        return

    log.error(f"Unknown trade_info_menu choice: {choice!r}")


async def handle_action_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on 'trade_info_action:<last12|by_id|remove>' callback_data."""
    query = update.callback_query
    await query.answer()

    try:
        _, action = query.data.split(":")
    except ValueError:
        log.error(f"Malformed trade_info_action callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)

    if action == "last12":
        trades = state_store.get_last_active_trades(chat_id, limit=12)
        if not trades:
            await context.bot.send_message(chat_id=chat_id, text=format_last_trades_list(trades), parse_mode="Markdown")
            return
        # Sent a few at a time rather than one giant message - same
        # reasoning as Search Signal's per-block sends: several
        # trade cards joined together can clear Telegram's ~4096-char
        # message limit long before 12 of them would.
        CHUNK = 4
        for start in range(0, len(trades), CHUNK):
            chunk_text = format_last_trades_list(trades[start:start + CHUNK])
            await context.bot.send_message(chat_id=chat_id, text=chunk_text, parse_mode="Markdown")
        return

    if action == "by_id":
        context.chat_data["awaiting_trade_by_id"] = True
        await context.bot.send_message(chat_id=chat_id, text=format_trade_by_id_prompt(), parse_mode="Markdown")
        return

    if action == "remove":
        context.chat_data["awaiting_trade_remove_id"] = True
        await context.bot.send_message(chat_id=chat_id, text=format_remove_trade_prompt(), parse_mode="Markdown")
        return

    log.error(f"Unknown trade_info_action: {action!r}")


async def handle_activate_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all text handler - only acts if this chat is awaiting a Trade ID to activate (see module docstring)."""
    if not context.chat_data.pop("awaiting_trade_activate_id", None):
        return

    chat_id = update.effective_chat.id
    trade_id = _clean_id(update.message.text)
    if trade_id is None:
        await update.message.reply_text(format_trade_id_bad_input(), parse_mode="Markdown")
        context.chat_data["awaiting_trade_activate_id"] = True
        return

    trade = state_store.activate_trade(chat_id, trade_id)
    if trade is None:
        await update.message.reply_text(format_trade_id_not_found(trade_id), parse_mode="Markdown")
        return
    if trade["alreadyActive"]:
        await update.message.reply_text(format_trade_already_active(trade), parse_mode="Markdown")
        return

    log.info(f"Trade activated: chat_id={chat_id} trade_id={trade_id}")
    await update.message.reply_text(format_trade_activated(trade), parse_mode="Markdown")
    await update.message.reply_text(
        format_active_balance_prompt(), parse_mode="Markdown", reply_markup=_balance_mode_keyboard(trade_id),
    )


async def handle_balance_mode_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires on 'trade_info_balmode:<only|balance>:<trade_id>' callback_data - the choice shown right after a trade is activated."""
    query = update.callback_query
    await query.answer()

    try:
        _, choice, trade_id = query.data.split(":")
    except ValueError:
        log.error(f"Malformed trade_info_balmode callback_data: {query.data!r}")
        return

    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)

    if choice != "balance":
        log.error(f"Unknown trade_info_balmode choice: {choice!r}")
        return

    trade = state_store.get_trade_by_id_for_chat(chat_id, trade_id)
    if trade is None:
        await context.bot.send_message(
            chat_id=chat_id, text=format_active_balance_error("this trade isn't open/active anymore."),
            parse_mode="Markdown",
        )
        return

    if trade["scope"] != "future":
        await context.bot.send_message(
            chat_id=chat_id, text=format_active_balance_spot_unsupported(), parse_mode="Markdown",
        )
        return

    wallet_balance = state_store.get_wallet_balance(chat_id)
    if not wallet_balance or wallet_balance <= 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_active_balance_error(
                "your Wallet Balance is 0 - set a balance, or wait for other Active-Balance trades to close first."
            ),
            parse_mode="Markdown",
        )
        return

    try:
        loop = asyncio.get_running_loop()
        contract_config = await loop.run_in_executor(None, fetch_bitget_futures_contract_config)
    except Exception as exc:
        log.error(f"Active Balance: couldn't fetch Bitget contract config for {trade['rawSymbol']}: {exc}")
        contract_config = {}

    plan = compute_live_trade_plan(
        wallet_balance, trade["entry"], trade["stopLoss"], trade.get("tp1"), trade.get("tp2"), trade.get("tp3"),
        trade["verdict"], contract_config.get(trade["rawSymbol"]),
    )
    if plan is None:
        await context.bot.send_message(
            chat_id=chat_id, text=format_active_balance_error("couldn't compute a position for this trade."),
            parse_mode="Markdown",
        )
        return
    if plan["belowMinSize"]:
        need = plan.get("minUsdtNeeded")
        need_str = f"you need at least {need:.2f} USDT" if need else "your balance is too small"
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_active_balance_error(f"{need_str} for this pair even at {plan['leverage']}x max leverage."),
            parse_mode="Markdown",
        )
        return

    locked = state_store.set_trade_balance_mode(
        chat_id, trade_id, margin_locked=plan["balance"], leverage=plan["leverage"],
        position_notional=plan["positionNotional"], quantity=plan["quantity"],
    )
    if not locked:
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_active_balance_error("this trade already has Active Balance set (only switchable once)."),
            parse_mode="Markdown",
        )
        return

    new_balance = state_store.adjust_wallet_balance(chat_id, -plan["balance"])
    log.info(f"Active Balance: chat_id={chat_id} trade_id={trade_id} margin_locked={plan['balance']:.2f}")
    await context.bot.send_message(
        chat_id=chat_id, text=format_active_balance_confirmed(trade, plan, new_balance), parse_mode="Markdown",
    )


async def handle_by_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all text handler for 'See Active Trade By ID'."""
    if not context.chat_data.pop("awaiting_trade_by_id", None):
        return

    chat_id = update.effective_chat.id
    trade_id = _clean_id(update.message.text)
    if trade_id is None:
        await update.message.reply_text(format_trade_id_bad_input(), parse_mode="Markdown")
        context.chat_data["awaiting_trade_by_id"] = True
        return

    trade = state_store.get_trade_by_id_for_chat(chat_id, trade_id)
    if trade is None:
        await update.message.reply_text(format_trade_by_id_not_found(trade_id), parse_mode="Markdown")
        return

    live_price = None
    if trade.get("balanceMode") == "list_with_balance" and trade["entryStatus"] == "arrived":
        try:
            live_price = await _get_live_price(trade["rawSymbol"], trade["scope"])
        except Exception as exc:
            log.error(f"See Active Trade By ID: live price fetch failed for {trade['rawSymbol']}: {exc}")

    await update.message.reply_text(format_trade_detail_block(trade, live_price), parse_mode="Markdown")


async def handle_remove_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all text handler for 'Remove Trade'."""
    if not context.chat_data.pop("awaiting_trade_remove_id", None):
        return

    chat_id = update.effective_chat.id
    trade_id = _clean_id(update.message.text)
    if trade_id is None:
        await update.message.reply_text(format_trade_id_bad_input(), parse_mode="Markdown")
        context.chat_data["awaiting_trade_remove_id"] = True
        return

    removed = state_store.remove_trade(chat_id, trade_id)
    if not removed:
        await update.message.reply_text(format_trade_remove_not_found(trade_id), parse_mode="Markdown")
        return

    log.info(f"Trade removed: chat_id={chat_id} trade_id={trade_id}")
    await update.message.reply_text(format_trade_removed(trade_id), parse_mode="Markdown")