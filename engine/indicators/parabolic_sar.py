# parabolic_sar.py - Parabolic SAR
# Trailing stop-and-reverse indicator. Works on full OHLCV candle
# dicts, oldest -> newest.


def compute_parabolic_sar(candles, af_start=0.02, af_step=0.02, af_max=0.2):
    """
    Returns {value, trend}. The dots flip above/below price when
    momentum reverses, commonly used as a dynamic trailing-stop
    reference.
    """
    if len(candles) < 3:
        return None

    trend = "up" if candles[1]["close"] > candles[0]["close"] else "down"
    sar = candles[0]["low"] if trend == "up" else candles[0]["high"]
    ep = candles[0]["high"] if trend == "up" else candles[0]["low"]
    af = af_start

    for i in range(1, len(candles)):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)
        c = candles[i]

        if trend == "up":
            sar = min(sar, candles[i - 1]["low"], candles[i - 2]["low"] if i >= 2 else candles[i - 1]["low"])
            if c["low"] < sar:
                trend = "down"
                sar = ep
                ep = c["low"]
                af = af_start
            else:
                if c["high"] > ep:
                    ep = c["high"]
                    af = min(af + af_step, af_max)
        else:
            sar = max(sar, candles[i - 1]["high"], candles[i - 2]["high"] if i >= 2 else candles[i - 1]["high"])
            if c["high"] > sar:
                trend = "up"
                sar = ep
                ep = c["high"]
                af = af_start
            else:
                if c["low"] < ep:
                    ep = c["low"]
                    af = min(af + af_step, af_max)

    return {"value": sar, "trend": trend}