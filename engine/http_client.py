# http_client.py
# Shared across exchange service modules (currently just bitget_api.py)
# so they all reuse ONE pooled connection instead of opening a fresh
# TCP/TLS handshake per call, and all log through the same named logger
# that http_server.py configures (console + this session's log file).

import logging
import requests
from requests.adapters import HTTPAdapter

http_session = requests.Session()
log = logging.getLogger("crypto-analyzer-http")

# The default HTTPAdapter pool only holds 10 connections. The indicator
# batch sweep (http_server.py, BATCH_WORKER_COUNT) runs 10 concurrent
# worker threads against api.bitget.com through this same session, and
# the live-price poll can be hitting it at the same time - 10 total
# wasn't enough headroom, so the pool was overflowing ("Connection pool
# is full, discarding connection" in the logs) and falling back to
# unpooled one-off connections, which is slower and less reliable right
# when a sweep is under the most load. 30 covers the sweep's 10 workers
# plus room for the concurrent ticker/candle polls without overflowing.
_adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
http_session.mount("https://", _adapter)
http_session.mount("http://", _adapter)