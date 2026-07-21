# buy_sell_volume.py - Buy vs Sell Volume
# Works on full OHLCV candle dicts, oldest -> newest.
#
# Honesty note: Bitget's kline/candle endpoint only returns OHLCV, not
# a buy/sell trade split, so there's no ground-truth per-candle buy vs
# sell breakdown available from candle history alone (that data only
# exists at the tick/trade level). This uses the standard
# charting-industry approximation instead: within each candle, volume
# is split by where the close landed in the candle's high-low range
# (close near the high -> more of that candle's volume treated as
# buy-side pressure, close near the low -> more sell-side). This is a
# well-known ESTIMATE, not exact tick data.
#
# For a REAL (non-estimated) buy/sell split, use services/order_flow.py,
# which reads Bitget's actual public trade prints - just only for the
# recent live window, not deep history. delta_volume.py builds on this
# same estimate for its own "Delta Volume" indicator.


def compute_buy_sell_volume(candles):
    """Returns {buyVolume, sellVolume, delta, estimated} totals across the given window."""
    if not candles:
        return None

    buy_vol, sell_vol = 0.0, 0.0
    for c in candles:
        rng = c["high"] - c["low"]
        if rng == 0:
            buy_vol += c["volume"] / 2
            sell_vol += c["volume"] / 2
            continue
        buy_fraction = (c["close"] - c["low"]) / rng
        buy_vol += c["volume"] * buy_fraction
        sell_vol += c["volume"] * (1 - buy_fraction)

    return {
        "buyVolume": round(buy_vol, 6),
        "sellVolume": round(sell_vol, 6),
        "delta": round(buy_vol - sell_vol, 6),
        "estimated": True,
    }
