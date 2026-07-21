# cci.py - Commodity Channel Index
# Works on full OHLCV candle dicts, oldest -> newest.


def compute_cci(candles, period=20):
    """
    Above +100 often read as overbought / strong uptrend, below -100
    as oversold / strong downtrend.
    """
    if len(candles) < period:
        return None

    window = candles[-period:]
    typicals = [(c["high"] + c["low"] + c["close"]) / 3 for c in window]
    sma_tp = sum(typicals) / period
    mean_dev = sum(abs(tp - sma_tp) for tp in typicals) / period

    if mean_dev == 0:
        return 0.0

    return (typicals[-1] - sma_tp) / (0.015 * mean_dev)
