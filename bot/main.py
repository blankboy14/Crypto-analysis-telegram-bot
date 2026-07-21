"""
bot/main.py

Entry point - run with:  python -m bot.main

Loads the token from .env, sets up logging from config/settings.yaml,
builds the python-telegram-bot Application, registers every handler,
and starts polling.

BUILD STATUS - read before running: the handler modules imported
below (bot/handlers/start.py, market_select.py, market_analyse.py,
strong_signal.py, search_signal.py) are still empty stubs, same as
bot/state_store.py and bot/formatters.py - only the file-plan skeleton
exists for them so far (see README's build-status table). This file
is written against the function names each of them is expected to
expose once filled in, so nothing here should need to change when
they are:

    handlers.start.start_command(update, context)
    handlers.market_select.handle_choice(update, context)   # inline Spot/Future/Both callback, pattern "market_select:*"
    handlers.market_analyse.handle_on(update, context)      # Phase 2.1 ON  -> "📊 24/7 Market Analyse"
    handlers.market_analyse.handle_off(update, context)     # Phase 2.1 OFF -> "🔕 24/7 Off Market Analyse"
    handlers.strong_signal.handle_on(update, context)       # Phase 2.2 ON  -> "🔥 Find 24/7 Strong Signal"
    handlers.strong_signal.handle_off(update, context)      # Phase 2.2 OFF -> "🛑 Off 24/7 Find Signal"
    handlers.search_signal.handle(update, context)          # Phase 2.3 one-shot -> "🔎 Search Signal"

Importing the (still empty) handler modules below works fine either
way - Python only complains the moment an actual button press or
command tries to call a function that isn't there yet.

Requires the job-queue extra for the "24/7" watchers (Phase 2.1/2.2)
to work once those handlers schedule jobs on it:
    pip install "python-telegram-bot[job-queue]"
"""

import logging
import os
import re
import sys

import yaml
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.keyboards import (
    BTN_MARKET_ANALYSE_OFF,
    BTN_MARKET_ANALYSE_ON,
    BTN_SEARCH_SIGNAL,
    BTN_STRONG_SIGNAL_OFF,
    BTN_STRONG_SIGNAL_ON,
)
from bot.handlers import market_analyse, market_select, search_signal, start, strong_signal

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(ROOT_DIR, "config", "settings.yaml")
ENV_PATH = os.path.join(ROOT_DIR, ".env")

log = logging.getLogger("crypto-telegram-bot")


def load_settings() -> dict:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class _RedactBotTokenFilter(logging.Filter):
    """
    Belt-and-suspenders redaction: strips any Telegram bot token
    (`.../bot<digits>:<token>/...`) out of every log record before it's
    emitted, regardless of which logger produced it. The main fix below
    (raising httpx/httpcore's own level) is what actually stops the
    noisy per-request URL logs in the first place - this filter is just
    a safety net so a token can never end up in logs/console even if
    some other library logs a URL containing it.
    """
    _TOKEN_RE = re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._TOKEN_RE.sub(r"\1<redacted>", record.msg)
        if record.args:
            record.args = tuple(
                self._TOKEN_RE.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def configure_logging(settings: dict) -> None:
    log_cfg = settings.get("logging", {})
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_cfg.get("log_to_file"):
        log_path = os.path.join(ROOT_DIR, log_cfg.get("log_file_path", "logs/bot_log.txt"))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    redact_filter = _RedactBotTokenFilter()
    for handler in handlers:
        handler.addFilter(redact_filter)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

    # httpx (the HTTP client python-telegram-bot uses under the hood)
    # logs every request's full URL at INFO level - and that URL
    # literally contains the bot token
    # (https://api.telegram.org/bot<TOKEN>/getUpdates). Raising these
    # two loggers to WARNING stops that leak at the source, regardless
    # of what `logging.level` is set to in settings.yaml.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start.start_command))

    # Inline Spot/Future/Both keyboard shown after any of the 3 mode
    # buttons below - market_select.py owns building/answering it.
    application.add_handler(
        CallbackQueryHandler(market_select.handle_choice, pattern=r"^market_select:")
    )

    # Phase 1.2's vertical main menu is a ReplyKeyboardMarkup, so button
    # presses arrive as plain text messages - route by exact label match
    # rather than callback_data.
    application.add_handler(MessageHandler(filters.Text([BTN_MARKET_ANALYSE_ON]), market_analyse.handle_on))
    application.add_handler(MessageHandler(filters.Text([BTN_MARKET_ANALYSE_OFF]), market_analyse.handle_off))
    application.add_handler(MessageHandler(filters.Text([BTN_STRONG_SIGNAL_ON]), strong_signal.handle_on))
    application.add_handler(MessageHandler(filters.Text([BTN_STRONG_SIGNAL_OFF]), strong_signal.handle_off))
    application.add_handler(MessageHandler(filters.Text([BTN_SEARCH_SIGNAL]), search_signal.handle))


def build_application(settings: dict) -> Application:
    load_dotenv(ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN not set - check your .env file "
            "(see README's Setup section for how to get one from @BotFather)."
        )

    # job_queue is what the "24/7" watchers (Phase 2.1/2.2) run on:
    # market_analyse.handle_on / strong_signal.handle_on are expected to
    # call context.job_queue.run_repeating(...) per-chat when a user
    # switches a mode ON, and cancel that job on OFF. Nothing is
    # scheduled here directly - this just needs the queue to exist.
    application = Application.builder().token(token).build()
    application.bot_data["settings"] = settings

    register_handlers(application)
    return application


def main() -> None:
    settings = load_settings()
    configure_logging(settings)

    application = build_application(settings)

    log.info("Bot starting - polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()