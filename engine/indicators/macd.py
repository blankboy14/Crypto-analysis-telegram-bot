# macd.py - Moving Average Convergence Divergence
# Depends on ema.py's EMA series, since MACD is by definition the
# difference between two EMAs (plus a signal-line EMA of that
# difference).

from .ema import compute_ema_series


def compute_macd(values, fast=12, slow=26, signal=9):
    """
    Returns dict with 'macd', 'signal', and 'histogram' lines.
    Positive histogram = bullish momentum, negative = bearish.
    """
    if len(values) < slow + signal:
        return None

    ema_fast = compute_ema_series(values, fast)
    ema_slow = compute_ema_series(values, slow)

    min_len = min(len(ema_fast), len(ema_slow))
    if min_len == 0:
        return None

    macd_line = [ema_fast[-min_len:][i] - ema_slow[-min_len:][i] for i in range(min_len)]

    if len(macd_line) < signal:
        return None

    signal_line = compute_ema_series(macd_line, signal)
    if not signal_line:
        return None

    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1],
        "histogram": macd_line[-1] - signal_line[-1],
    }