# order_blocks.py - Order Blocks
# BUILDING BLOCK - used by ict.py and smc.py.
#
# Works on full OHLCV candle dicts, oldest -> newest.


def analyze_order_blocks(candles, displacement_atr_mult=2.0, atr_period=14, max_blocks=6):
    """
    An order block is the LAST opposite-colored candle right before a
    strong displacement move: a bearish (red) candle right before a
    sharp move up is a bullish order block (institutions' likely last
    sell fills before markup - price often returns to "mitigate" it
    before continuing up); a bullish (green) candle right before a
    sharp move down is a bearish order block.

    "Strong" = the displacement candle's body is >= `displacement_atr_mult`
    x the recent average true range. Only unmitigated blocks (price
    hasn't traded back through the block's full range since) are kept.

    Returns {"bullish": [{"top","bottom","time"}], "bearish": [...]}
    (most recent first) or None if there isn't enough history.
    """
    n = len(candles)
    if n < atr_period + 5:
        return None

    trs = []
    for i in range(1, n):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - prev["close"]), abs(c["low"] - prev["close"])))

    bullish, bearish = [], []

    for i in range(atr_period + 1, n):
        recent_atr = sum(trs[i - atr_period - 1:i - 1]) / atr_period
        if recent_atr == 0:
            continue

        move = candles[i]
        body = abs(move["close"] - move["open"])
        if body < recent_atr * displacement_atr_mult:
            continue

        ob_candle = candles[i - 1]
        ob_is_bearish_candle = ob_candle["close"] < ob_candle["open"]
        move_is_up = move["close"] > move["open"]

        if move_is_up and ob_is_bearish_candle:
            bullish.append({"top": ob_candle["high"], "bottom": ob_candle["low"], "time": ob_candle["time"], "mitigated": False})
        elif not move_is_up and not ob_is_bearish_candle:
            bearish.append({"top": ob_candle["high"], "bottom": ob_candle["low"], "time": ob_candle["time"], "mitigated": False})

    for block_list, is_bullish in ((bullish, True), (bearish, False)):
        for block in block_list:
            for c in candles:
                if c["time"] <= block["time"]:
                    continue
                if is_bullish and c["low"] <= block["bottom"]:
                    block["mitigated"] = True
                elif not is_bullish and c["high"] >= block["top"]:
                    block["mitigated"] = True

    fresh_bullish = [b for b in bullish if not b["mitigated"]][-max_blocks:][::-1]
    fresh_bearish = [b for b in bearish if not b["mitigated"]][-max_blocks:][::-1]

    return {"bullish": fresh_bullish, "bearish": fresh_bearish}
