# supertrend.py - SuperTrend
# Trend-following overlay built from ATR bands around the candle
# midpoint. Depends on atr.py, since SuperTrend is by definition built
# from ATR. Works on full OHLCV candle dicts, oldest -> newest.

from .atr import compute_atr_series


def compute_supertrend(candles, period=10, multiplier=3):
    """
    Returns {value, trend} where trend is "up" or "down". Widely used
    for a fast visual "is this bullish or bearish right now" read.
    """
    if len(candles) < period + 1:
        return None

    atr_series = compute_atr_series(candles, period)
    if not atr_series:
        return None

    offset = len(candles) - len(atr_series)
    trend = "up"
    final_upper = final_lower = None
    supertrend_value = None

    for i, atr in enumerate(atr_series):
        c = candles[offset + i]
        mid = (c["high"] + c["low"]) / 2
        basic_upper = mid + multiplier * atr
        basic_lower = mid - multiplier * atr

        if final_upper is None:
            final_upper, final_lower = basic_upper, basic_lower
        else:
            prev_close = candles[offset + i - 1]["close"]
            final_upper = basic_upper if (basic_upper < final_upper or prev_close > final_upper) else final_upper
            final_lower = basic_lower if (basic_lower > final_lower or prev_close < final_lower) else final_lower

        if c["close"] > final_upper:
            trend = "up"
        elif c["close"] < final_lower:
            trend = "down"
        # else: trend unchanged (still inside the band)

        supertrend_value = final_lower if trend == "up" else final_upper

    return {"value": supertrend_value, "trend": trend}