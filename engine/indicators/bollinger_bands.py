# bollinger_bands.py - Bollinger Bands
# Works on a plain list of closing prices (oldest -> newest).


def compute_bollinger(values, period=20, num_std=2):
    """
    Price near the lower band can suggest oversold / potential bounce,
    near the upper band can suggest overbought / potential pullback.
    """
    if len(values) < period:
        return None

    window = values[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance ** 0.5

    return {
        "middle": mean,
        "upper": mean + num_std * std,
        "lower": mean - num_std * std,
    }