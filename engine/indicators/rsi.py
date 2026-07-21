# rsi.py - Relative Strength Index
# Works on a plain list of closing prices (oldest -> newest).


def compute_rsi(values, period=14):
    """
    Relative Strength Index. Values above 70 typically mean overbought,
    below 30 typically mean oversold.
    """
    if len(values) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))