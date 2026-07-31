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
from datetime import datetime

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
    BTN_CLEAN_CHAT,
    BTN_MARKET_ANALYSE_OFF,
    BTN_MARKET_ANALYSE_ON,
    BTN_MARKET_ANALYSE_STATUS,
    BTN_MARKET_DETAILS,
    BTN_SEARCH_SIGNAL,
    BTN_SEARCH_SIGNAL_STATUS,
    BTN_TRADE_INFORMATION,
    BTN_SINGLE_PAIR_ANALYSE,
    BTN_STRONG_SIGNAL_OFF,
    BTN_STRONG_SIGNAL_ON,
    BTN_STRONG_SIGNAL_STATUS,
    BTN_WALLET_BALANCE,
    BTN_SERVER_INFORMATION,
)
from bot.handlers import (
    clean_chat,
    help as help_handler,
    market_analyse,
    market_details,
    market_select,
    search_signal,
    server_information,
    single_pair_analyse,
    start,
    status,
    strong_signal,
    trade_information,
    wallet_balance,
)
from bot.message_tracker import TrackingBot, track_incoming
from jobs import heartbeat, keepalive, signal_outcome_tracker
from web.dashboard import start_dashboard_server

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
        # One fresh log file per run (instead of always appending to the
        # same bot_log.txt), named with this run's start timestamp so
        # every run's logs are kept separately.
        configured_path = log_cfg.get("log_file_path", "logs/bot_log.txt")
        log_dir = os.path.join(ROOT_DIR, os.path.dirname(configured_path) or "logs")
        os.makedirs(log_dir, exist_ok=True)
        run_timestamp = datetime.now().strftime("%d_%m_%Y-%S_%M_%H")
        log_path = os.path.join(log_dir, f"{run_timestamp}.txt")
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
    application.add_handler(MessageHandler(filters.Text([BTN_SEARCH_SIGNAL_STATUS]), status.handle_search_signal_status))
    application.add_handler(MessageHandler(filters.Text([BTN_TRADE_INFORMATION]), trade_information.handle))
    application.add_handler(
        CallbackQueryHandler(trade_information.handle_menu_choice, pattern=r"^trade_info_menu:")
    )
    application.add_handler(
        CallbackQueryHandler(trade_information.handle_action_choice, pattern=r"^trade_info_action:")
    )
    application.add_handler(
        CallbackQueryHandler(trade_information.handle_balance_mode_choice, pattern=r"^trade_info_balmode:")
    )
    application.add_handler(MessageHandler(filters.Text([BTN_SINGLE_PAIR_ANALYSE]), single_pair_analyse.handle))
    application.add_handler(MessageHandler(filters.Text([BTN_MARKET_DETAILS]), market_details.handle))
    application.add_handler(MessageHandler(filters.Text([BTN_WALLET_BALANCE]), wallet_balance.handle))
    application.add_handler(MessageHandler(filters.Text([BTN_SERVER_INFORMATION]), server_information.handle))
    application.add_handler(
        CallbackQueryHandler(server_information.handle_choice, pattern=r"^server_info:")
    )
    application.add_handler(MessageHandler(filters.Text([BTN_CLEAN_CHAT]), clean_chat.handle))
    application.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_handler.handle))

    # message_tracker.track_incoming (see bot/message_tracker.py) - runs
    # in its own early group (-1) so it records every single incoming
    # message_id before any button/text handler above even looks at the
    # update, regardless of which one (if any) ends up handling it. This
    # is what lets "🧹 Clean Chat" find the user's own messages, not just
    # the bot's.
    application.add_handler(MessageHandler(filters.ALL, track_incoming), group=-1)

    # Catch-all for free-text typed after a button asks for it - a pair
    # name (Single Pair Analyse), a "how many pairs" number (Market
    # Details), or a wallet balance (Wallet Balance). MUST be
    # registered last (within this same group=0) - PTB only runs the
    # FIRST matching handler in a group per update, so every exact-label
    # button above still wins its own match first; this one only ever
    # sees text that didn't match any of them. Each of the handlers
    # below is a no-op unless THIS chat is actually waiting for its own
    # kind of input (see their own early-returns), so routing through
    # all of them in sequence is safe.
    async def _text_router(update, context):
        await single_pair_analyse.handle_pair_text(update, context)
        await market_details.handle_number_text(update, context)
        await wallet_balance.handle_balance_text(update, context)
        await trade_information.handle_activate_text(update, context)
        await trade_information.handle_by_id_text(update, context)
        await trade_information.handle_remove_text(update, context)

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
    # switches a mode ON, and cancel that job on OFF - none of THOSE
    # are scheduled here directly.
    # signal_outcome_tracker is the one exception: it's GLOBAL, not
    # per-chat (it just checks whatever's still open in signal_outcomes
    # across every chat), so it's scheduled once, right here, and runs
    # regardless of any chat's own 24/7 toggle state.
    #
    # Built on TrackingBot (bot/message_tracker.py) instead of the
    # default ExtBot, purely so "🧹 Clean Chat" can find every message
    # the bot ever sent - everything else (rate limiting included)
    # behaves exactly the same as before.
    bot = TrackingBot(token=token, rate_limiter=AIORateLimiter())
    application = Application.builder().bot(bot).build()
    application.bot_data["settings"] = settings

    outcome_cfg = settings.get("signal_outcome_tracker", {})
    if outcome_cfg.get("enabled", True):
        application.job_queue.run_repeating(
            signal_outcome_tracker.tick,
            interval=outcome_cfg.get("poll_interval_seconds", 300),
            first=15,
        )

    # Anti-sleep heartbeat (see jobs/heartbeat.py) - HEARTBEAT_CHAT_ID
    # lives in .env (not settings.yaml) since it's a personal chat id,
    # not a tunable number safe to commit.
    heartbeat_cfg = settings.setdefault("heartbeat", {})
    env_heartbeat_chat_id = os.getenv("HEARTBEAT_CHAT_ID")
    if env_heartbeat_chat_id:
        try:
            heartbeat_cfg["chat_id"] = int(env_heartbeat_chat_id)
        except ValueError:
            log.error("HEARTBEAT_CHAT_ID in .env is not a valid chat id - heartbeat disabled.")

    if heartbeat_cfg.get("enabled", True) and heartbeat_cfg.get("chat_id"):
        application.job_queue.run_repeating(
            heartbeat.tick,
            interval=heartbeat_cfg.get("interval_seconds", 3600),
            first=60,
        )
    else:
        log.info("Heartbeat not scheduled - set HEARTBEAT_CHAT_ID in .env to enable it.")

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
            # Uptime monitors (e.g. UptimeRobot) send HEAD requests by
            # default - without this, BaseHTTPRequestHandler has no
            # do_HEAD and replies "501 Not Implemented".
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

    # NOTE: _start_health_server() above is kept exactly as it was,
    # untouched - but it's intentionally NOT called anymore. It opens
    # the same $PORT that start_dashboard_server() below also opens,
    # and only one server can bind to a given port at a time - calling
    # both here would crash on startup with "address already in use".
    # start_dashboard_server() does everything the old function did
    # (opens $PORT, replies 200 to GET/HEAD so Render's free-tier port
    # check passes) plus the new status page, so it's the one that
    # actually runs. To go back to the old plain "OK" response, swap
    # the line below to _start_health_server() instead.
    start_dashboard_server()

    # Self-ping keep-alive (jobs/keepalive.py) - pings PUBLIC_URL (set
    # in .env) every KEEPALIVE_INTERVAL_MINUTES so Render's free tier
    # never sees 15 minutes of inactivity and never spins the service
    # down. No-ops with a log line if PUBLIC_URL isn't set.
    keepalive.start()

    application = build_application(settings)

    log.info("Bot starting - polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C is the normal, expected way to stop this bot locally -
        # it isn't a crash. Without this, python-telegram-bot's internal
        # asyncio shutdown re-raises KeyboardInterrupt on its way out and
        # prints a big traceback that looks like something broke, even
        # though the bot already stopped cleanly. This just swaps that
        # traceback for one plain line.
        log.info("Bot stopped (Ctrl+C).")