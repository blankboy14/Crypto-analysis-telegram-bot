"""
bot/main.py

Entry point - run with:  python -m bot.main

Loads the token from .env, sets up logging from config/settings.yaml,
builds the python-telegram-bot Application, registers every handler,
and starts polling.

All handlers (start, market_select, market_analyse, strong_signal,
search_signal, single_pair_analyse, status) and state_store/formatters
are implemented. Search Signal (Phase 2.3) has an extra step after
Spot/Future/Both: a total-pair-count message, then a Full Analysis /
Skip Analysis Detail choice, both handled inside
bot/handlers/market_select.py. Single Pair Analyse adds a third step
after Spot/Future/Both - a free-text pair name, caught by the catch-all
MessageHandler registered last in register_handlers() below.

ISOLATION: a global error_handler is registered below so an unhandled
exception anywhere (a handler, a job tick) is logged and reported
without ever crashing the polling loop or affecting any other chat/
mode. An AIORateLimiter is also attached so bursts of messages (e.g.
Full Analysis's per-pair detail batches) queue smoothly instead of
tripping Telegram's flood limits.

Requires these extras:
    pip install "python-telegram-bot[job-queue,rate-limiter]"
"""

import logging
import os
import re
import sys

# Make sure the project root (parent of this 'bot' folder) is on sys.path.
# This lets the file work whether it's launched as `python -m bot.main`
# or run directly as `python bot/main.py` (some hosting platforms do the latter).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import yaml
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.keyboards import (
    BTN_HELP,
    BTN_MARKET_ANALYSE_OFF,
    BTN_MARKET_ANALYSE_ON,
    BTN_MARKET_ANALYSE_STATUS,
    BTN_MARKET_DETAILS,
    BTN_SEARCH_SIGNAL,
    BTN_SINGLE_PAIR_ANALYSE,
    BTN_STRONG_SIGNAL_OFF,
    BTN_STRONG_SIGNAL_ON,
    BTN_STRONG_SIGNAL_STATUS,
)
from bot.handlers import (
    help as help_handler,
    market_analyse,
    market_details,
    market_select,
    search_signal,
    single_pair_analyse,
    start,
    status,
    strong_signal,
)

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


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global safety net (ISOLATION FIX): catches ANY exception an async
    handler or job callback raises that wasn't already caught closer
    to its source. Without this registered, python-telegram-bot still
    won't crash the process, but it silently swallows the error with
    no visibility and no message back to the user. This makes failures
    loud in the logs and, where possible, tells the affected chat
    what happened - while guaranteeing every OTHER chat and mode keeps
    running untouched, since PTB dispatches each update/job
    independently.
    """
    log.error("Unhandled exception while processing an update", exc_info=context.error)
    chat_id = None
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id
    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Something went wrong handling that. It's isolated to this action - "
                     "please try again; your other running modes are unaffected.",
            )
        except Exception:
            pass  # best-effort only - never let error reporting itself raise


def register_handlers(application: Application) -> None:
    application.add_error_handler(_on_error)

    application.add_handler(CommandHandler("start", start.start_command))

    # Inline Spot/Future/Both keyboard shown after any of the 3 mode
    # buttons below - market_select.py owns building/answering it.
    application.add_handler(
        CallbackQueryHandler(market_select.handle_choice, pattern=r"^market_select:")
    )
    # Phase 2.3's second prompt (Full Analysis / Skip Analysis Detail),
    # shown after Search Signal's Spot/Future/Both answer.
    application.add_handler(
        CallbackQueryHandler(market_select.handle_analysis_depth_choice, pattern=r"^search_signal_mode:")
    )
    # Market Details' second prompt (All / Higher / Lower / Top by
    # Volume), shown after its own Spot/Future/Both answer.
    application.add_handler(
        CallbackQueryHandler(market_details.handle_type_choice, pattern=r"^market_details_type:")
    )

    # Phase 1.2's vertical main menu is a ReplyKeyboardMarkup, so button
    # presses arrive as plain text messages - route by exact label match
    # rather than callback_data.
    application.add_handler(MessageHandler(filters.Text([BTN_MARKET_ANALYSE_ON]), market_analyse.handle_on))
    application.add_handler(MessageHandler(filters.Text([BTN_MARKET_ANALYSE_OFF]), market_analyse.handle_off))
    application.add_handler(MessageHandler(filters.Text([BTN_MARKET_ANALYSE_STATUS]), status.handle_market_analyse_status))
    application.add_handler(MessageHandler(filters.Text([BTN_STRONG_SIGNAL_ON]), strong_signal.handle_on))
    application.add_handler(MessageHandler(filters.Text([BTN_STRONG_SIGNAL_OFF]), strong_signal.handle_off))
    application.add_handler(MessageHandler(filters.Text([BTN_STRONG_SIGNAL_STATUS]), status.handle_strong_signal_status))
    application.add_handler(MessageHandler(filters.Text([BTN_SEARCH_SIGNAL]), search_signal.handle))
    application.add_handler(MessageHandler(filters.Text([BTN_SINGLE_PAIR_ANALYSE]), single_pair_analyse.handle))
    application.add_handler(MessageHandler(filters.Text([BTN_MARKET_DETAILS]), market_details.handle))
    application.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_handler.handle))

    # Catch-all for free-text typed after a button asks for it - a pair
    # name (Single Pair Analyse) or a "how many pairs" number (Market
    # Details). MUST be registered last (within this same group=0) -
    # PTB only runs the FIRST matching handler in a group per update, so
    # every exact-label button above still wins its own match first;
    # this one only ever sees text that didn't match any of them. Each
    # of the two handlers below is a no-op unless THIS chat is actually
    # waiting for its own kind of input (see their own early-returns),
    # so routing through both in sequence is safe.
    async def _text_router(update, context):
        await single_pair_analyse.handle_pair_text(update, context)
        await market_details.handle_number_text(update, context)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text_router))


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
    application = Application.builder().token(token).rate_limiter(AIORateLimiter()).build()
    application.bot_data["settings"] = settings

    register_handlers(application)
    return application


def _start_health_server() -> None:
    """Start a tiny HTTP server so Render detects an open port (free Web Service requirement)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silence access logs
            pass

    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health-check server listening on port %s", port)


def main() -> None:
    settings = load_settings()
    configure_logging(settings)

    _start_health_server()

    application = build_application(settings)

    log.info("Bot starting - polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()