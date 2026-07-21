# atr.py - Average True Range
# Volatility measure. Works on full OHLCV candle dicts:
# {time, open, high, low, close, volume}, oldest -> newest.
# compute_atr_series is exported because SuperTrend (supertrend.py)
# needs the full smoothed series, not just the latest value.


def compute_atr_series(candles, period=14):
    """Full ATR series (Wilder-smoothed)."""
    if len(candles) < period + 1:
        return []

    trs = []
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))

    if len(trs) < period:
        return []

    series = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        series.append((series[-1] * (period - 1) + tr) / period)
    return series


def compute_atr(candles, period=14):
    """Latest ATR value only."""
    series = compute_atr_series(candles, period)
    return series[-1] if series else None