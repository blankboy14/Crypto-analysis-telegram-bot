# adx.py - Average Directional Index
# Trend STRENGTH (not direction). Above ~25 typically means a trending
# market, below ~20 typically means a ranging one. Works on full OHLCV
# candle dicts: {time, open, high, low, close, volume}, oldest -> newest.


def compute_adx(candles, period=14):
    """Returns {adx, plus_di, minus_di}."""
    if len(candles) < period * 2:
        return None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        up_move = candles[i]["high"] - candles[i - 1]["high"]
        down_move = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
        h, l, prev_c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))

    def wilder_smooth(series, period):
        if len(series) < period:
            return []
        out = [sum(series[:period])]
        for v in series[period:]:
            out.append(out[-1] - (out[-1] / period) + v)
        return out

    smoothed_tr = wilder_smooth(trs, period)
    smoothed_plus_dm = wilder_smooth(plus_dm, period)
    smoothed_minus_dm = wilder_smooth(minus_dm, period)

    if not smoothed_tr or smoothed_tr[-1] == 0:
        return None

    n = min(len(smoothed_tr), len(smoothed_plus_dm), len(smoothed_minus_dm))
    dx_series = []
    for i in range(n):
        if smoothed_tr[i] == 0:
            continue
        plus_di = 100 * smoothed_plus_dm[i] / smoothed_tr[i]
        minus_di = 100 * smoothed_minus_dm[i] / smoothed_tr[i]
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum != 0 else 0
        dx_series.append((dx, plus_di, minus_di))

    if len(dx_series) < period:
        return None

    adx = sum(d[0] for d in dx_series[:period]) / period
    for d in dx_series[period:]:
        adx = (adx * (period - 1) + d[0]) / period

    latest_plus_di, latest_minus_di = dx_series[-1][1], dx_series[-1][2]
    return {"adx": adx, "plus_di": latest_plus_di, "minus_di": latest_minus_di}