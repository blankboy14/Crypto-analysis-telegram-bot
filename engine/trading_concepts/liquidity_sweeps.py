# liquidity_sweeps.py - Liquidity Sweeps
# BUILDING BLOCK - used by ict.py and smc.py, and reads
# liquidity_identification.py's pools to know WHAT was swept.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import find_swing_points


def analyze_liquidity_sweeps(candles, lookback=3, max_sweeps=6):
    """
    A liquidity sweep: a candle's wick pokes PAST a prior swing
    high/low (triggering stops resting there) but the candle then
    CLOSES back on the other side of it - a classic stop-hunt/fakeout,
    not a genuine breakout. Returns
    {"buySideSweeps": [...], "sellSideSweeps": [...]} (most recent
    first) - "buy side" = swept a high (stops of shorts / breakout
    buyers), "sell side" = swept a low.
    """
    swings = find_swing_points(candles, lookback)
    if len(swings) < 2:
        return None

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    buy_side, sell_side = [], []

    for high in highs:
        for c in candles:
            if c["time"] <= high["time"]:
                continue
            if c["high"] > high["price"] and c["close"] < high["price"]:
                buy_side.append({"level": high["price"], "wickHigh": c["high"], "time": c["time"]})
                break  # only the first sweep of this specific level counts

    for low in lows:
        for c in candles:
            if c["time"] <= low["time"]:
                continue
            if c["low"] < low["price"] and c["close"] > low["price"]:
                sell_side.append({"level": low["price"], "wickLow": c["low"], "time": c["time"]})
                break

    return {
        "buySideSweeps": buy_side[-max_sweeps:][::-1],
        "sellSideSweeps": sell_side[-max_sweeps:][::-1],
    }
