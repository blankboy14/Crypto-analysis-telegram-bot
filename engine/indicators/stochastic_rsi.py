# stochastic_rsi.py - Stochastic RSI
# A stochastic oscillator applied to RSI values (instead of price),
# giving a more sensitive overbought/oversold read than plain RSI.
# Range 0-100; >80 overbought, <20 oversold. Depends on rsi.py, since
# a Stochastic RSI is by definition derived from RSI values.

from .rsi import compute_rsi


def compute_stochastic_rsi(values, rsi_period=14, stoch_period=14):
    if len(values) < rsi_period + stoch_period:
        return None

    rsi_series = []
    for i in range(rsi_period + 1, len(values) + 1):
        r = compute_rsi(values[:i], rsi_period)
        if r is not None:
            rsi_series.append(r)

    if len(rsi_series) < stoch_period:
        return None

    window = rsi_series[-stoch_period:]
    lo, hi = min(window), max(window)
    if hi == lo:
        return 50.0

    return (window[-1] - lo) / (hi - lo) * 100