# price_action.py - Price Action
# CONCEPT - a general read of the last few candles' raw shape, not
# tied to any one named pattern (see candlestick_patterns.py for
# specific named patterns like engulfing/hammer/doji).
#
# Works on full OHLCV candle dicts, oldest -> newest.


def _candle_shape(c):
    body = abs(c["close"] - c["open"])
    full_range = c["high"] - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    lower_wick = min(c["open"], c["close"]) - c["low"]
    return {
        "bodyPct": (body / full_range * 100) if full_range > 0 else 0,
        "upperWickPct": (upper_wick / full_range * 100) if full_range > 0 else 0,
        "lowerWickPct": (lower_wick / full_range * 100) if full_range > 0 else 0,
        "bullish": c["close"] > c["open"],
    }


def analyze_price_action(candles, momentum_lookback=5):
    """
    Returns {"lastCandle": {...shape}, "rejection": "upper"|"lower"|None,
    "momentum": "strong_bullish"|"strong_bearish"|"weak"|"choppy"}.
    "rejection" flags a candle with a wick >= 60% of its range on one
    side (a long wick rejecting that side - classic pin-bar behavior).
    """
    if len(candles) < momentum_lookback + 1:
        return None

    last = candles[-1]
    shape = _candle_shape(last)

    rejection = None
    if shape["upperWickPct"] >= 60:
        rejection = "upper"
    elif shape["lowerWickPct"] >= 60:
        rejection = "lower"

    window = candles[-momentum_lookback:]
    bullish_count = sum(1 for c in window if c["close"] > c["open"])
    bearish_count = len(window) - bullish_count
    net_move_pct = (window[-1]["close"] - window[0]["open"]) / window[0]["open"] * 100 if window[0]["open"] > 0 else 0

    # Candle-color counting alone misses this case: one large impulsive
    # candle plus a couple of small consolidation candles of the other
    # color (e.g. 3 red / 2 green) used to fall through to the "choppy"
    # bucket below even when net_move_pct was clearly, strongly directional.
    # Measure the move's size against the window's own average range first,
    # so a genuinely large directional move is never mislabeled choppy just
    # because it wasn't spread evenly across every candle.
    avg_range_pct = sum(
        (c["high"] - c["low"]) / c["open"] * 100 for c in window if c["open"] > 0
    ) / len(window)
    strong_move = avg_range_pct > 0 and abs(net_move_pct) >= avg_range_pct * 1.5

    if net_move_pct > 0 and (strong_move or bullish_count >= momentum_lookback * 0.8):
        momentum = "strong_bullish"
    elif net_move_pct < 0 and (strong_move or bearish_count >= momentum_lookback * 0.8):
        momentum = "strong_bearish"
    elif abs(bullish_count - bearish_count) <= 1:
        momentum = "choppy"
    else:
        momentum = "weak"

    return {"lastCandle": shape, "rejection": rejection, "momentum": momentum}