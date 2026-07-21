# pivot_points.py - Standard (classic) Pivot Points
# Works on a single OHLC candle dict (the PREVIOUS completed candle).


def compute_pivot_points(prev_candle):
    """
    Calculated from the PREVIOUS completed candle's high/low/close -
    e.g. pass yesterday's daily candle to get today's levels.
    """
    if not prev_candle:
        return None

    h, l, c = prev_candle["high"], prev_candle["low"], prev_candle["close"]
    pp = (h + l + c) / 3
    return {
        "pp": pp,
        "r1": 2 * pp - l, "r2": pp + (h - l), "r3": h + 2 * (pp - l),
        "s1": 2 * pp - h, "s2": pp - (h - l), "s3": l - 2 * (h - pp),
    }