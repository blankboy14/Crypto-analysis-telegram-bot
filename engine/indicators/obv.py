# obv.py - On-Balance Volume
# Works on full OHLCV candle dicts, oldest -> newest.


def _obv_series(candles):
    """Full running OBV value at every bar (not just the final cumulative total) - needed to read a TREND, since a single OBV number has no inherent direction on its own."""
    series = [0.0]
    for i in range(1, len(candles)):
        prev = series[-1]
        if candles[i]["close"] > candles[i - 1]["close"]:
            series.append(prev + candles[i]["volume"])
        elif candles[i]["close"] < candles[i - 1]["close"]:
            series.append(prev - candles[i]["volume"])
        else:
            series.append(prev)
    return series


def compute_obv(candles, trend_lookback=14):
    """
    Cumulative volume, added on up-closes and subtracted on
    down-closes. Rising OBV alongside rising price confirms the trend;
    divergence between the two is a classic early-warning signal.

    Returns {"value", "trend"} - `trend` ("rising"/"falling"/"flat") is
    a comparison of OBV now vs OBV `trend_lookback` bars ago, which is
    what engine.signal_engine's vote actually reads (the raw
    cumulative `value` alone can't be bullish or bearish - it's an
    arbitrary running total, not a level with any inherent meaning).
    """
    if not candles:
        return None

    series = _obv_series(candles)
    value = series[-1]

    trend = "flat"
    if len(series) > trend_lookback:
        prior = series[-1 - trend_lookback]
        # Scale the "meaningful change" bar to this pair's own recent
        # OBV swings, not a fixed number - OBV's units are raw volume,
        # which varies by orders of magnitude between pairs.
        recent_span = max(series[-trend_lookback:]) - min(series[-trend_lookback:])
        threshold = recent_span * 0.1 if recent_span > 0 else 0
        if value - prior > threshold:
            trend = "rising"
        elif prior - value > threshold:
            trend = "falling"

    return {"value": value, "trend": trend}