# ema.py - Exponential Moving Average (all major periods)
# Works on a plain list of closing prices (oldest -> newest).
# compute_ema_series is exported because MACD (macd.py) needs the full
# series, not just the latest value, to build its signal line.


def compute_ema_series(values, period):
    """
    Full EMA series (not just the latest value) - needed internally
    by MACD, which requires an EMA-of-an-EMA calculation.
    """
    if len(values) < period:
        return []

    k = 2 / (period + 1)
    ema_values = [sum(values[:period]) / period]  # seed with SMA
    for price in values[period:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values


def compute_ema(values, period):
    """Latest EMA value only."""
    series = compute_ema_series(values, period)
    return series[-1] if series else None


def compute_all_ema_periods(values, periods=(9, 21, 50, 100, 200)):
    """
    Convenience helper for "EMA (All Major Periods)" from the spec -
    returns {period: value} for every period given, None where there
    isn't enough history yet.
    """
    return {p: compute_ema(values, p) for p in periods}