"""
bot/message_tracker.py

Purely in-memory (never written to database/bot_state.db or
logs/*.txt) record of every message_id sent to, or received from,
each chat - this exists ONLY so bot/handlers/clean_chat.py (the
"🧹 Clean Chat" button) can wipe a chat's visible Telegram history.
No trade/signal/wallet/log data is ever touched by any of this -
those all live in state_store.py / the log file, completely
untouched here.

Two feeds populate it:
  1. Incoming (the user's own taps/typed messages) - track_incoming(),
     registered in bot/main.py as a MessageHandler(filters.ALL, ...)
     in its own early group (-1), so it sees literally every update
     before any other handler runs and can never miss one.
  2. Outgoing (everything the bot itself sends - both direct handler
     replies and the 24/7 watcher jobs' pushes) - TrackingBot, an
     ExtBot subclass used in place of the default Bot class (see
     bot/main.py's build_application()). It just records the
     message_id every time send_message() actually returns one, then
     behaves exactly like ExtBot for everything else (rate limiting
     included, since that's handled deeper in the call chain and
     isn't affected by this override).

Capped at MAX_TRACKED per chat (oldest dropped first) so a chat that's
been running for months doesn't grow this forever - a clean can only
ever reach back that far anyway.
"""
import logging
from collections import defaultdict, deque

from telegram.ext import ExtBot

log = logging.getLogger("crypto-telegram-bot")

MAX_TRACKED = 500

_tracked: dict = defaultdict(lambda: deque(maxlen=MAX_TRACKED))


def _track(chat_id, message_id) -> None:
    if chat_id is None or message_id is None:
        return
    _tracked[int(chat_id)].append(message_id)


async def track_incoming(update, context) -> None:
    """group=-1 catch-all handler (see bot/main.py) - records the
    user's own message_id, then does nothing else; every other
    handler in group 0+ still processes the same update normally."""
    msg = update.effective_message
    if msg is not None:
        _track(msg.chat_id, msg.message_id)


def pop_all(chat_id) -> list:
    """Returns every tracked message_id for this chat and forgets
    them in the same step - clean_chat.py calls this once, right
    before it starts deleting, so a message that arrives mid-clean
    isn't silently dropped from being tracked for next time."""
    ids = list(_tracked.get(int(chat_id), ()))
    _tracked.pop(int(chat_id), None)
    return ids


class TrackingBot(ExtBot):
    """Identical to ExtBot in every way except it also records the
    message_id of every message it sends. Covers both
    context.bot.send_message(...) and update.message.reply_text(...)
    - the latter routes through this same send_message under the
    hood in python-telegram-bot, so no other file needed to change."""

    async def send_message(self, chat_id, *args, **kwargs):
        message = await super().send_message(chat_id, *args, **kwargs)
        try:
            if message is not None:
                _track(chat_id, message.message_id)
        except Exception as exc:
            log.error(f"message_tracker: failed to record outgoing message for chat {chat_id}: {exc}")
        return message