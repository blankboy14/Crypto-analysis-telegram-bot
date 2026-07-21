# vwap.py - Volume Weighted Average Price
# Works on full OHLCV candle dicts, oldest -> newest.


def compute_vwap(candles):
    """
    Cumulative VWAP across the given candle window (typical price =
    (H+L+C)/3). Note: real VWAP is normally reset at the start of each
    trading session; since crypto trades 24/7 with no fixed session,
    this is cumulative over whatever candle window is passed in (e.g.
    the current day's 30m candles).
    """
    if not candles:
        return None

    cum_pv, cum_vol = 0.0, 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical * c["volume"]
        cum_vol += c["volume"]

    return cum_pv / cum_vol if cum_vol > 0 else None