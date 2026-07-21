"""
bot/handlers/search_signal.py

Phase 2.3 - "Search Signal": one-shot scan of the chosen market,
returns the current top 3 tradeable setups, then it's done. Nothing
persists and there's no OFF button for this one - "auto off" per the
spec just means it doesn't keep running past this single reply,
unlike the two 24/7 modes.

All the actual work (running the scan, formatting the top-3 reply)
happens in market_select._run_search_signal() once the user answers
the Spot/Future/Both prompt below - this handler only needs to ask.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.handlers import market_select

log = logging.getLogger("crypto-telegram-bot")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await market_select.ask_market(
        update, context,
        pending_action="search_signal",
        prompt="Choose a market to search for a signal right now:",
    )