# obv.py - On-Balance Volume
# Works on full OHLCV candle dicts, oldest -> newest.


def compute_obv(candles):
    """
    Cumulative volume, added on up-closes and subtracted on
    down-closes. Rising OBV alongside rising price confirms the trend;
    divergence between the two is a classic early-warning signal.
    """
    if not candles:
        return None

    obv = 0.0
    for i in range(1, len(candles)):
        if candles[i]["close"] > candles[i - 1]["close"]:
            obv += candles[i]["volume"]
        elif candles[i]["close"] < candles[i - 1]["close"]:
            obv -= candles[i]["volume"]
    return obv
