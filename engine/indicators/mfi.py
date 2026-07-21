# mfi.py - Money Flow Index
# Volume-weighted RSI. Works on full OHLCV candle dicts, oldest -> newest.


def compute_mfi(candles, period=14):
    """
    Uses typical price * volume ("money flow") instead of raw price
    change. Above 80 overbought, below 20 oversold.
    """
    if len(candles) < period + 1:
        return None

    typicals = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles]
    pos_flow, neg_flow = 0.0, 0.0

    for i in range(len(candles) - period, len(candles)):
        money_flow = typicals[i] * candles[i]["volume"]
        if typicals[i] > typicals[i - 1]:
            pos_flow += money_flow
        elif typicals[i] < typicals[i - 1]:
            neg_flow += money_flow

    if neg_flow == 0:
        return 100.0

    money_ratio = pos_flow / neg_flow
    return 100 - (100 / (1 + money_ratio))
