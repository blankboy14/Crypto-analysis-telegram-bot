# rsi_divergence.py - RSI Divergence (Regular + Hidden, Bullish + Bearish)
#
# Converted from the "RSI Divergence Indicator" Pine Script (the widely
# used LonesomeTheBlue version on TradingView) at the user's request -
# same pivot-lookback method: a pivot low/high on the RSI oscillator is
# only CONFIRMED once pivot_lookback_right bars have passed on either
# side (matching the Pine script's ta.pivotlow/ta.pivothigh + its
# offset=-lbR plotting), then compared against the PREVIOUS confirmed
# pivot - as long as the two pivots are between range_lower and
# range_upper bars apart - to decide:
#   bullish        - price makes a LOWER low, RSI makes a HIGHER low
#                     (momentum fading on the way down - classic
#                     reversal-up warning)
#   hiddenBullish   - price makes a HIGHER low, RSI makes a LOWER low
#                     (trend-continuation-up signal)
#   bearish        - price makes a HIGHER high, RSI makes a LOWER high
#                     (momentum fading on the way up - classic
#                     reversal-down warning)
#   hiddenBearish   - price makes a LOWER high, RSI makes a HIGHER high
#                     (trend-continuation-down signal)
#
# Works on a plain list of OHLCV candle dicts (oldest -> newest), same
# convention as every other indicator in this package.


def _wilder_rsi_series(closes, period=14):
    """
    Full RSI value at EVERY bar (not just the latest one, unlike
    rsi.py's compute_rsi) - needed here since divergence compares RSI
    readings at two different points in time, not just now. Same
    Wilder's-smoothing method TradingView's ta.rsi() uses, so values
    line up with what the original Pine script would have plotted.
    Returns a list the same length as `closes`; entries before there's
    enough history yet are None.
    """
    n = len(closes)
    rsi = [None] * n
    if n < period + 1:
        return rsi

    gains, losses = [0.0] * n, [0.0] * n
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    return rsi


def _find_pivots(series, lb_left, lb_right, kind):
    """
    Confirmed pivot indices in `series`: a value strictly lower
    (kind="low") or higher (kind="high") than every other value in the
    lb_left-before/lb_right-after window around it. Only indices with
    enough room on both sides can ever be confirmed - same reason the
    original script's plot only appears lb_right bars after the actual
    turning point.
    """
    pivots = []
    n = len(series)
    for i in range(lb_left, n - lb_right):
        window = series[i - lb_left:i + lb_right + 1]
        if any(v is None for v in window):
            continue
        center = series[i]
        if kind == "low" and center == min(window) and window.count(center) == 1:
            pivots.append(i)
        elif kind == "high" and center == max(window) and window.count(center) == 1:
            pivots.append(i)
    return pivots


def compute_rsi_divergence(candles, period=14, pivot_lookback_left=5, pivot_lookback_right=5,
                            range_lower=5, range_upper=60):
    """
    Returns None if there isn't enough candle history yet, otherwise:
        {
            "rsi": <latest RSI value>,
            "signal": "bullish" | "hiddenBullish" | "bearish" | "hiddenBearish" | None,
            "confirmedBarsAgo": <int, only set when signal is not None>,
        }
    `signal` is the most recently CONFIRMED divergence, reported only
    while it's still fresh (confirmed within the last
    pivot_lookback_right bars - matching the original script's
    offset=-lbR plot, which is exactly how far behind "now" a
    confirmed pivot always sits). A divergence that scrolled further
    back than that isn't reported here - by then the move it warned
    about has typically already played out, so surfacing it as a
    live/current signal would be misleading.
    """
    if not candles:
        return None
    closes = [c["close"] for c in candles]
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    n = len(candles)
    if n < period + pivot_lookback_left + pivot_lookback_right + range_lower + 2:
        return None

    rsi_series = _wilder_rsi_series(closes, period)
    latest_rsi = rsi_series[-1]
    if latest_rsi is None:
        return None

    pivot_lows = _find_pivots(rsi_series, pivot_lookback_left, pivot_lookback_right, "low")
    pivot_highs = _find_pivots(rsi_series, pivot_lookback_left, pivot_lookback_right, "high")

    signal, confirmed_at = None, None

    # --- Bullish / hidden bullish: compare the two most recent RSI pivot LOWS ---
    if len(pivot_lows) >= 2:
        i2, i1 = pivot_lows[-1], pivot_lows[-2]
        if range_lower <= (i2 - i1) <= range_upper:
            osc_higher_low = rsi_series[i2] > rsi_series[i1]
            osc_lower_low = rsi_series[i2] < rsi_series[i1]
            price_lower_low = lows[i2] < lows[i1]
            price_higher_low = lows[i2] > lows[i1]
            if price_lower_low and osc_higher_low:
                signal, confirmed_at = "bullish", i2
            elif price_higher_low and osc_lower_low:
                signal, confirmed_at = "hiddenBullish", i2

    # --- Bearish / hidden bearish: compare the two most recent RSI pivot
    # HIGHS - only overrides the bullish result above if it's a MORE
    # recently confirmed pivot (i.e. whichever divergence is freshest wins). ---
    if len(pivot_highs) >= 2:
        i2, i1 = pivot_highs[-1], pivot_highs[-2]
        if range_lower <= (i2 - i1) <= range_upper:
            osc_lower_high = rsi_series[i2] < rsi_series[i1]
            osc_higher_high = rsi_series[i2] > rsi_series[i1]
            price_higher_high = highs[i2] > highs[i1]
            price_lower_high = highs[i2] < highs[i1]
            bear_signal, bear_at = None, None
            if price_higher_high and osc_lower_high:
                bear_signal, bear_at = "bearish", i2
            elif price_lower_high and osc_higher_high:
                bear_signal, bear_at = "hiddenBearish", i2
            if bear_signal and (confirmed_at is None or bear_at > confirmed_at):
                signal, confirmed_at = bear_signal, bear_at

    confirmed_bars_ago = None
    if confirmed_at is not None:
        confirmed_bars_ago = (n - 1) - confirmed_at
        if confirmed_bars_ago > pivot_lookback_right:
            signal, confirmed_bars_ago = None, None

    return {"rsi": round(latest_rsi, 2), "signal": signal, "confirmedBarsAgo": confirmed_bars_ago}