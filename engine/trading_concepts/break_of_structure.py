# break_of_structure.py - Break of Structure (BOS)
# BUILDING BLOCK - used by ict.py, smc.py, and trend_structure.py.
# Confirms the CURRENT trend by detecting a close beyond the most
# recent relevant swing IN the trend's direction (continuation, not
# reversal - see change_of_character.py for the reversal case).
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import find_swing_points


def analyze_break_of_structure(candles, lookback=3):
    """
    Returns {"broke": True/False, "direction", "level", "time"} for the
    most recent BOS found (a close beyond the last swing high while
    already in an uptrend = bullish BOS/continuation; the mirror for
    downtrend), or None if there isn't enough swing history to judge.
    """
    swings = find_swing_points(candles, lookback)
    if len(swings) < 3:
        return None

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if not highs or not lows:
        return None

    last_high = highs[-1]
    last_low = lows[-1]

    last_bullish_break = None
    last_bearish_break = None
    for c in candles:
        if c["time"] > last_high["time"] and c["close"] > last_high["price"]:
            last_bullish_break = c
    for c in candles:
        if c["time"] > last_low["time"] and c["close"] < last_low["price"]:
            last_bearish_break = c

    if last_bullish_break and (not last_bearish_break or last_bullish_break["time"] >= last_bearish_break["time"]):
        return {"broke": True, "direction": "bullish", "level": last_high["price"], "time": last_bullish_break["time"]}
    if last_bearish_break:
        return {"broke": True, "direction": "bearish", "level": last_low["price"], "time": last_bearish_break["time"]}

    return {"broke": False, "direction": None, "level": None, "time": None}
