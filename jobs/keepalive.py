# jobs/keepalive.py
#
# Self-ping keep-alive loop for Render's free Web Service tier.
#
# WHY THIS EXISTS
# ----------------
# Render spins a FREE Web Service down after ~15 minutes with no inbound
# HTTP traffic (confirmed on Render's own docs, July 2026). The old
# health server in bot/main.py opens a port, but nothing was ever
# actually hitting it - so a free instance would still go to sleep and
# stop polling Telegram entirely.
#
# This module starts one background thread that, every
# KEEPALIVE_INTERVAL_MINUTES (default 10 - safely under Render's 15
# minute idle window), sends a real HTTP GET to this service's OWN
# public URL (PUBLIC_URL in .env). As long as that gap stays under ~15
# minutes, Render never sees the service go idle, so it never spins
# down.
#
# HONEST LIMITATION: this can only PREVENT sleep, not undo it. If the
# process is ever actually asleep, crashed, or mid-deploy, it isn't
# running - so it can't ping itself back awake. A 10-minute interval is
# what keeps that from happening in normal operation. As a free extra
# safety net you can ALSO point an external monitor (UptimeRobot,
# cron-job.org, etc.) at the same PUBLIC_URL - belt and suspenders,
# costs nothing, doesn't conflict with this.
#
# Render's free plan also includes 750 instance-hours/month/workspace -
# a single service running 24/7 uses ~730-745 hours in a month, so one
# free service kept awake like this fits comfortably inside that quota
# (running a second free service at the same time may not).
#
# Every check is also reported to HEARTBEAT_CHAT_ID (the same chat id
# already used by jobs/heartbeat.py) as a Telegram message, so you get
# a visible "still alive" ping roughly every 10 minutes on top of the
# existing hourly heartbeat. If that's too chatty, set
# KEEPALIVE_TELEGRAM_NOTIFY=false in .env - the web dashboard
# (web/dashboard.py) still shows the same last/next check info either
# way.

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("crypto-telegram-bot")

# Shared state the dashboard reads from. Plain dict + GIL is fine here:
# one writer thread (the loop below), any number of readers (HTTP
# handler threads), and every individual assignment is atomic.
_state = {
    "last_check_at": None,
    "last_check_ok": None,
    "next_check_at": None,
    "interval_minutes": 10,
    "started_at": datetime.now(timezone.utc),
    "public_url": None,
    "total_checks": 0,
    "total_failures": 0,
}


def get_state() -> dict:
    """Read-only snapshot for web/dashboard.py to render."""
    return dict(_state)


KEEPALIVE_MESSAGE_TTL_SECONDS = 30  # how long an automatic keep-alive push stays visible before auto-deleting


def _build_keepalive_text(now, next_at, ok, error_text) -> str:
    """Factored out so both the automatic push (_loop, below) and the on-demand '🖥 Server Information' -> 'Keep-Alive Status' button (bot/handlers/server_information.py) show identical text."""
    emoji = "✅" if ok else "⚠️"
    lines = [
        f"{emoji} *Keep-Alive Check*",
        f"🕐 Checked: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"⏭ Next check: {next_at.strftime('%H:%M:%S')} UTC",
        f"🌐 Render service: {'awake' if ok else 'NOT responding'}",
    ]
    if not ok:
        lines.append(f"Error: {error_text[:150]}")
    return "\n".join(lines)


def build_keepalive_text_from_state() -> str | None:
    """
    Same text as _build_keepalive_text(), but read from the last
    completed check in `_state` instead of a check that just happened
    - what the on-demand 'Keep-Alive Status' button shows (it doesn't
    force a fresh ping, just reports the most recent one, same as the
    web dashboard already does). Returns None if no check has run yet.
    """
    if _state["last_check_at"] is None:
        return None
    return _build_keepalive_text(
        _state["last_check_at"], _state["next_check_at"], _state["last_check_ok"], "",
    )


def _delete_telegram(token: str, chat_id: int, message_id: int) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )
    except Exception:
        pass  # already deleted/too old/etc - never worth erroring over


def _send_telegram(token: str, chat_id: int, text: str) -> None:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        message_id = resp.json().get("result", {}).get("message_id")
        if message_id:
            # This still pushes every ~10 minutes (proof the loop
            # itself is alive and on schedule) - it just no longer
            # sits in the chat forever. threading.Timer here (not
            # time.sleep) so it doesn't delay this thread's next check
            # by KEEPALIVE_MESSAGE_TTL_SECONDS every single cycle.
            threading.Timer(
                KEEPALIVE_MESSAGE_TTL_SECONDS, _delete_telegram, args=(token, chat_id, message_id),
            ).start()
    except Exception:
        log.error("Keepalive: failed to send Telegram notification", exc_info=True)


def _loop(public_url, interval_minutes, bot_token, chat_id, notify) -> None:
    _state["public_url"] = public_url
    _state["interval_minutes"] = interval_minutes

    while True:
        now = datetime.now(timezone.utc)
        ok, error_text = True, ""
        try:
            resp = requests.get(public_url, timeout=15)
            ok = resp.status_code < 500
        except Exception as exc:
            ok = False
            error_text = str(exc)
            log.error("Keepalive: self-ping failed: %s", exc)

        _state["last_check_at"] = now
        _state["last_check_ok"] = ok
        _state["total_checks"] += 1
        if not ok:
            _state["total_failures"] += 1

        next_at = now + timedelta(minutes=interval_minutes)
        _state["next_check_at"] = next_at

        if notify and bot_token and chat_id:
            _send_telegram(bot_token, chat_id, _build_keepalive_text(now, next_at, ok, error_text))

        time.sleep(interval_minutes * 60)


def start(public_url=None, interval_minutes=None, bot_token=None, chat_id=None, notify=None) -> None:
    """
    Call once from bot/main.py, right after the dashboard/health server
    starts. Starts a daemon background thread and returns immediately -
    never blocks, never raises (a misconfigured keep-alive should never
    stop the bot itself from starting).
    """
    public_url = public_url or os.getenv("PUBLIC_URL")
    if not public_url:
        log.info("Keepalive not started - set PUBLIC_URL in .env (your Render URL) to enable it.")
        return

    interval_minutes = interval_minutes or int(os.getenv("KEEPALIVE_INTERVAL_MINUTES", "10"))
    if notify is None:
        notify = os.getenv("KEEPALIVE_TELEGRAM_NOTIFY", "true").strip().lower() != "false"
    bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    env_chat_id = os.getenv("HEARTBEAT_CHAT_ID")
    if chat_id is None and env_chat_id:
        try:
            chat_id = int(env_chat_id)
        except ValueError:
            chat_id = None

    thread = threading.Thread(
        target=_loop,
        args=(public_url, interval_minutes, bot_token, chat_id, notify),
        daemon=True,
        name="keepalive",
    )
    thread.start()
    log.info(
        "Keepalive started - pinging %s every %s minute(s), telegram notify=%s",
        public_url, interval_minutes, notify,
    )