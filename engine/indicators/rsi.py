# rsi.py - Relative Strength Index
# Works on a plain list of closing prices (oldest -> newest).


def compute_rsi(values, period=14):
    """
    Relative Strength Index, using Wilder's original smoothing method -
    the same one TradingView's ta.rsi() uses (and what
    rsi_divergence.py's own internal RSI series already uses) - so this
    matches what a person would see charting the same pair anywhere
    else, and both RSI-based indicators in this package now agree with
    each other on the same data.

    Values above 70 typically mean overbought, below 30 typically mean
    oversold.
    """
    if len(values) < period + 1:
        return None

    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]

    # Seed with a plain average of the first `period` changes, then
    # smooth every change after that - this recursive step (not a
    # fresh windowed average each time) is what makes it Wilder's
    # method specifically, and it's what gives more recent data more
    # influence, same as an EMA.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_multi_period_rsi(values, periods=(6, 9, 14, 21)):
    """
    RSI at several commonly-used periods at once - each period suits a
    different read: ~5-7 for fast/scalp-style momentum shifts, ~9-11
    for short-term swings, 14 for the standard/default read, 21+ for
    longer-term/trend-confirmation. Returns {period: value}, None for
    any period without enough history yet.

    engine.signal_engine's vote reads this for AGREEMENT across
    periods specifically - RSI(7) and RSI(21) both flagging oversold
    at once is a materially stronger signal than RSI(14) alone saying
    so, since it means the condition holds across both fast and slow
    lookback windows, not just one arbitrary one.
    """
    return {p: compute_rsi(values, p) for p in periods}