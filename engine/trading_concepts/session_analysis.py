# session_analysis.py - Session Analysis
# CONCEPT - Asian / London / New York session ranges, using candle
# `time` (unix seconds, UTC - matches services/bitget_api.py's candle
# format) to bucket candles into sessions.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from datetime import datetime, timezone

# Rough, widely-used UTC session hour windows (there's no single
# official definition - exchanges/desks vary by up to an hour). Asian
# wraps midnight UTC.
SESSION_WINDOWS_UTC = {
    "asian": (0, 8),
    "london": (7, 16),
    "newYork": (12, 21),
}


def _session_for_hour(hour):
    sessions = []
    for name, (start, end) in SESSION_WINDOWS_UTC.items():
        if start <= hour < end:
            sessions.append(name)
    return sessions


def analyze_session_analysis(candles, lookback_candles=48):
    """
    Returns {"asian": {"high","low","open","close"}, "london": {...},
    "newYork": {...}, "currentSession": [...]} built from the most
    recent `lookback_candles` (default 48 - two full days at 1h). A
    session dict is None if no candles fell in that window yet.
    """
    if not candles:
        return None

    window = candles[-lookback_candles:]
    buckets = {"asian": [], "london": [], "newYork": []}

    for c in window:
        # candle "time" is Bitget's raw timestamp, which is MILLISECONDS
        # (see services/bitget_api.py) - dividing by 1000 here matches
        # every other place in the app that reads this field (e.g. the
        # chart's Math.floor(c.time / 1000) in analysis.html). Without
        # this, fromtimestamp() gets a value ~1000x too large, which is
        # out of datetime's representable range - on Windows that raises
        # "OSError: [Errno 22] Invalid argument" instead of a normal
        # ValueError, which is what was showing up as this concept's
        # error card.
        hour = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc).hour
        for session in _session_for_hour(hour):
            buckets[session].append(c)

    result = {}
    for name, bucket in buckets.items():
        if not bucket:
            result[name] = None
            continue
        result[name] = {
            "high": max(c["high"] for c in bucket),
            "low": min(c["low"] for c in bucket),
            "open": bucket[0]["open"],
            "close": bucket[-1]["close"],
        }

    now_hour = datetime.fromtimestamp(candles[-1]["time"] / 1000, tz=timezone.utc).hour
    result["currentSession"] = _session_for_hour(now_hour)
    return result