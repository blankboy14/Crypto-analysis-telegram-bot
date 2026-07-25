"""
jobs/keepalive.py

Pings the bot's own PUBLIC_URL every KEEPALIVE_INTERVAL_MINUTES minutes
to prevent Render's free tier from spinning down after 15 min of inactivity.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests

log = logging.getLogger("crypto-telegram-bot")


def start() -> None:
    """Start the keep-alive background thread. No-ops if PUBLIC_URL is not set."""
    public_url = os.getenv("PUBLIC_URL", "").strip()
    if not public_url:
        log.info("Keepalive: PUBLIC_URL not set, skipping keep-alive.")
        return

    interval = int(os.getenv("KEEPALIVE_INTERVAL_MINUTES", "10")) * 60

    def _loop() -> None:
        log.info("Keepalive: started, pinging %s every %s min.", public_url, interval // 60)
        while True:
            try:
                requests.get(public_url, timeout=10)
                log.info("Keepalive: ping OK -> %s", public_url)
            except Exception as exc:
                log.warning("Keepalive: ping failed: %s", exc)
            time.sleep(interval)

    thread = threading.Thread(target=_loop, daemon=True, name="keepalive")
    thread.start()
