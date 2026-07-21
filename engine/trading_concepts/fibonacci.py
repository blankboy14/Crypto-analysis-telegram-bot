# fibonacci.py - Fibonacci Retracement & Extension
# BUILDING BLOCK - premium_discount_zones.py uses the retracement
# midpoint (the 0.5 level) as its equilibrium line.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import find_swing_points

RETRACEMENT_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
EXTENSION_RATIOS = [1.272, 1.618, 2.0, 2.618]


def compute_fibonacci_levels(swing_high_price, swing_low_price, direction="up"):
    """
    `direction="up"` means the swing LOW came first and price is
    retracing DOWN from the high (the common "measure the last leg up"
    case); `direction="down"` is the mirror. Returns
    {"retracements": {ratio: price}, "extensions": {ratio: price}}.
    """
    span = swing_high_price - swing_low_price
    if span <= 0:
        return None

    if direction == "up":
        retracements = {r: swing_high_price - span * r for r in RETRACEMENT_RATIOS}
        extensions = {r: swing_high_price + span * (r - 1) for r in EXTENSION_RATIOS}
    else:
        retracements = {r: swing_low_price + span * r for r in RETRACEMENT_RATIOS}
        extensions = {r: swing_low_price - span * (r - 1) for r in EXTENSION_RATIOS}

    return {"retracements": retracements, "extensions": extensions}


def analyze_fibonacci(candles, lookback=3):
    """
    Auto-picks the most recent significant swing high/low pair and
    builds Fibonacci levels off it. Returns
    {"swingHigh", "swingLow", "direction", "retracements", "extensions"}
    or None if there isn't a usable swing pair yet.
    """
    swings = find_swing_points(candles, lookback)
    if len(swings) < 2:
        return None

    last_two = swings[-2:]
    high_swing = next((s for s in last_two if s["type"] == "high"), None)
    low_swing = next((s for s in last_two if s["type"] == "low"), None)
    if not high_swing or not low_swing:
        # The last two swings in the list can legitimately both be the same
        # type (e.g. two highs in a row before a low finally confirms), in
        # which case the check above finds nothing even though plenty of
        # swing history exists. Fall back to searching the whole list
        # backward for the most recent high and the most recent low,
        # wherever they actually are - this is what was producing "not
        # enough data" while 12 swings were visibly being tracked.
        high_swing = next((s for s in reversed(swings) if s["type"] == "high"), None)
        low_swing = next((s for s in reversed(swings) if s["type"] == "low"), None)
    if not high_swing or not low_swing:
        return None

    # If the low came after the high, price swung DOWN most recently -
    # measure the drop (direction="down"); otherwise it swung UP.
    direction = "down" if low_swing["index"] > high_swing["index"] else "up"
    levels = compute_fibonacci_levels(high_swing["price"], low_swing["price"], direction)
    if levels is None:
        return None

    return {
        "swingHigh": high_swing,
        "swingLow": low_swing,
        "direction": direction,
        **levels,
    }