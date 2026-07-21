# ichimoku_cloud.py - Ichimoku Cloud
# Works on full OHLCV candle dicts, oldest -> newest.


def compute_ichimoku(candles, tenkan_period=9, kijun_period=26, senkou_b_period=52):
    """
    Returns the current values of all 5 lines. Senkou Span A/B are
    traditionally plotted 26 periods AHEAD (the "cloud" projected into
    the future) - `senkou_a`/`senkou_b` here are that forward-projected
    pair computed from the current window, and `chikou_span` is the
    current close plotted 26 periods BACK.
    """
    if len(candles) < senkou_b_period:
        return None

    def mid(period):
        window = candles[-period:]
        return (max(c["high"] for c in window) + min(c["low"] for c in window)) / 2

    tenkan = mid(tenkan_period)
    kijun = mid(kijun_period)
    senkou_a = (tenkan + kijun) / 2
    senkou_b = mid(senkou_b_period)
    chikou = candles[-1]["close"]
    price = candles[-1]["close"]
    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)

    # Bias is price's position relative to the cloud (above = bullish,
    # below = bearish, inside = neutral) - NOT senkou_a vs senkou_b, which
    # only describes the cloud's own color/thickness and says nothing
    # about where price actually is relative to it. Comparing senkou_a to
    # senkou_b alone could call a move "bullish" while price sits well
    # below the cloud, which is what was happening before this fix.
    if price > cloud_top:
        cloud_bias = "bullish"
    elif price < cloud_bottom:
        cloud_bias = "bearish"
    else:
        cloud_bias = "neutral"

    return {
        "tenkan_sen": tenkan,
        "kijun_sen": kijun,
        "senkou_span_a": senkou_a,
        "senkou_span_b": senkou_b,
        "chikou_span": chikou,
        "cloud_bias": cloud_bias,
    }