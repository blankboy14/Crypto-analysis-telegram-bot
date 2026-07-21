# change_of_character.py - Change of Character (CHoCH)
# BUILDING BLOCK - used by ict.py and smc.py. The early-warning
# opposite of break_of_structure.py: CHoCH is the FIRST break AGAINST
# the prevailing structure (a potential trend reversal), while BOS is
# a break WITH the prevailing structure (continuation).
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import analyze_market_structure, find_swing_points


def analyze_change_of_character(candles, lookback=3):
    """
    Returns {"changed": True/False, "from", "to", "level", "time"} -
    e.g. an uptrend's most recent higher-low getting broken to the
    downside is a bearish CHoCH (uptrend -> possible reversal down).
    None if there isn't enough structure yet to judge.
    """
    structure_info = analyze_market_structure(candles, lookback)
    if structure_info is None:
        return None

    structure = structure_info["structure"]
    if structure == "ranging":
        return {"changed": False, "from": "ranging", "to": None, "level": None, "time": None}

    swings = find_swing_points(candles, lookback)
    lows = [s for s in swings if s["type"] == "low"]
    highs = [s for s in swings if s["type"] == "high"]

    if structure == "uptrend" and lows:
        protected_low = lows[-1]
        for c in candles:
            if c["time"] > protected_low["time"] and c["close"] < protected_low["price"]:
                return {"changed": True, "from": "uptrend", "to": "downtrend", "level": protected_low["price"], "time": c["time"]}

    if structure == "downtrend" and highs:
        protected_high = highs[-1]
        for c in candles:
            if c["time"] > protected_high["time"] and c["close"] > protected_high["price"]:
                return {"changed": True, "from": "downtrend", "to": "uptrend", "level": protected_high["price"], "time": c["time"]}

    return {"changed": False, "from": structure, "to": None, "level": None, "time": None}
