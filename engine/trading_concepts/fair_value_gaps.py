# fair_value_gaps.py - Fair Value Gaps (FVG)
# BUILDING BLOCK - used by ict.py and smc.py.
#
# Works on full OHLCV candle dicts, oldest -> newest.


def analyze_fair_value_gaps(candles, max_gaps=8):
    """
    A (3-candle) Fair Value Gap / imbalance: candle 1's high doesn't
    overlap candle 3's low (bullish FVG - a gap up the market skipped
    over), or candle 1's low doesn't overlap candle 3's high (bearish
    FVG - gap down). Price often "fills" back into these gaps before
    continuing. Only gaps not yet fully filled are kept.

    Returns {"bullish": [{"top","bottom","time"}], "bearish": [...]}
    (most recent first) or None if there isn't enough history.
    """
    n = len(candles)
    if n < 5:
        return None

    bullish, bearish = [], []

    for i in range(2, n):
        c1, c3 = candles[i - 2], candles[i]

        if c1["high"] < c3["low"]:
            bullish.append({"top": c3["low"], "bottom": c1["high"], "time": candles[i - 1]["time"], "filled": False})
        elif c1["low"] > c3["high"]:
            bearish.append({"top": c1["low"], "bottom": c3["high"], "time": candles[i - 1]["time"], "filled": False})

    for gap_list, is_bullish in ((bullish, True), (bearish, False)):
        for gap in gap_list:
            for c in candles:
                if c["time"] <= gap["time"]:
                    continue
                if is_bullish and c["low"] <= gap["bottom"]:
                    gap["filled"] = True
                elif not is_bullish and c["high"] >= gap["top"]:
                    gap["filled"] = True

    open_bullish = [g for g in bullish if not g["filled"]][-max_gaps:][::-1]
    open_bearish = [g for g in bearish if not g["filled"]][-max_gaps:][::-1]

    return {"bullish": open_bullish, "bearish": open_bearish}
