# trend_structure.py - Trend Structure
# BUILDING BLOCK - a strength/duration read built on top of
# market_structure.py's HH/HL vs LH/LL classification, plus
# break_of_structure.py to see if the trend is actively confirming.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import analyze_market_structure
from .break_of_structure import analyze_break_of_structure


def analyze_trend_structure(candles, lookback=3):
    """
    Returns {"trend", "strength" (0-100), "confirmedByBos", "swingCount"}.
    Strength scales with how many consecutive swings agree with the
    trend direction (more confirming swings = more established trend),
    and gets a boost if the latest BOS also agrees with it.
    None if there isn't enough structure yet.
    """
    structure_info = analyze_market_structure(candles, lookback)
    if structure_info is None:
        return None

    trend = structure_info["structure"]
    swings = structure_info["swings"]

    if trend == "ranging":
        return {"trend": "ranging", "strength": 0, "confirmedByBos": False, "swingCount": len(swings)}

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    # Count consecutive agreeing swings from the most recent backward.
    agreeing = 0
    for i in range(len(highs) - 1, 0, -1):
        if trend == "uptrend" and highs[i]["price"] > highs[i - 1]["price"]:
            agreeing += 1
        elif trend == "downtrend" and highs[i]["price"] < highs[i - 1]["price"]:
            agreeing += 1
        else:
            break
    for i in range(len(lows) - 1, 0, -1):
        if trend == "uptrend" and lows[i]["price"] > lows[i - 1]["price"]:
            agreeing += 1
        elif trend == "downtrend" and lows[i]["price"] < lows[i - 1]["price"]:
            agreeing += 1
        else:
            break

    bos = analyze_break_of_structure(candles, lookback)
    confirmed = bool(bos and bos["broke"] and (
        (trend == "uptrend" and bos["direction"] == "bullish") or
        (trend == "downtrend" and bos["direction"] == "bearish")
    ))

    strength = min(100, agreeing * 20 + (15 if confirmed else 0))

    return {"trend": trend, "strength": strength, "confirmedByBos": confirmed, "swingCount": len(swings)}
