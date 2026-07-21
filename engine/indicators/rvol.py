# rvol.py - Relative Volume
# Works on full OHLCV candle dicts, oldest -> newest.


def compute_rvol(candles, period=20):
    """
    The most recent candle's volume vs the average volume of the
    preceding `period` candles. 1.0 = normal, 2.0 = twice the usual
    volume, etc.
    """
    if len(candles) < period + 1:
        return None

    baseline = candles[-(period + 1):-1]
    avg_vol = sum(c["volume"] for c in baseline) / period
    if avg_vol == 0:
        return None

    return candles[-1]["volume"] / avg_vol
