# candlestick_patterns.py - Candlestick Patterns
# CONCEPT - classic named single/multi-candle patterns, separate from
# price_action.py's general unnamed shape read.
#
# Works on full OHLCV candle dicts, oldest -> newest.


def _body(c):
    return abs(c["close"] - c["open"])


def _range(c):
    return c["high"] - c["low"]


def analyze_candlestick_patterns(candles):
    """
    Checks the last 1-3 candles against a set of classic patterns and
    returns every one that matches: [{"name", "type": "bullish"|
    "bearish"|"neutral", "time"}], most recent first. Empty list (not
    None) if nothing matched - None only if there isn't enough history.
    """
    if len(candles) < 3:
        return None

    found = []
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]  # oldest -> newest of the last 3

    # --- Doji: body is a tiny fraction of the full range ---
    if _range(c3) > 0 and _body(c3) / _range(c3) <= 0.1:
        found.append({"name": "Doji", "type": "neutral", "time": c3["time"]})

    # --- Hammer / Shooting Star: small body, one long wick, near a
    # local extreme in the last few candles ---
    upper_wick = c3["high"] - max(c3["open"], c3["close"])
    lower_wick = min(c3["open"], c3["close"]) - c3["low"]
    if _range(c3) > 0 and _body(c3) / _range(c3) <= 0.35:
        if lower_wick >= _body(c3) * 2 and upper_wick <= _body(c3):
            found.append({"name": "Hammer", "type": "bullish", "time": c3["time"]})
        elif upper_wick >= _body(c3) * 2 and lower_wick <= _body(c3):
            found.append({"name": "Shooting Star", "type": "bearish", "time": c3["time"]})

    # --- Engulfing: c3's body fully engulfs c2's body, opposite colors ---
    c2_bullish = c2["close"] > c2["open"]
    c3_bullish = c3["close"] > c3["open"]
    if c2_bullish != c3_bullish:
        if c3_bullish and c3["close"] >= c2["open"] and c3["open"] <= c2["close"]:
            found.append({"name": "Bullish Engulfing", "type": "bullish", "time": c3["time"]})
        elif not c3_bullish and c3["open"] >= c2["close"] and c3["close"] <= c2["open"]:
            found.append({"name": "Bearish Engulfing", "type": "bearish", "time": c3["time"]})

    # --- Morning Star / Evening Star: big down/small/big up (or mirror) ---
    if _body(c1) > 0 and _body(c2) / max(_body(c1), 1e-12) <= 0.4:
        c1_bearish = c1["close"] < c1["open"]
        if c1_bearish and c3_bullish and c3["close"] > (c1["open"] + c1["close"]) / 2:
            found.append({"name": "Morning Star", "type": "bullish", "time": c3["time"]})
        elif not c1_bearish and not c3_bullish and c3["close"] < (c1["open"] + c1["close"]) / 2:
            found.append({"name": "Evening Star", "type": "bearish", "time": c3["time"]})

    return found
