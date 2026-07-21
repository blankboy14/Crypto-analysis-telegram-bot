# smc.py - Smart Money Concepts (SMC)
# CONCEPT - overlaps heavily with ict.py (both are built from the same
# structure/order-block/liquidity primitives) but frames the read
# around BOS/CHoCH-confirmed structure and unmitigated order blocks
# specifically, which is the more "textbook SMC" framing vs ICT's
# liquidity-draw framing.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import analyze_market_structure
from .break_of_structure import analyze_break_of_structure
from .change_of_character import analyze_change_of_character
from .order_blocks import analyze_order_blocks
from .liquidity_sweeps import analyze_liquidity_sweeps


def analyze_smc(candles, lookback=3):
    """
    Returns {"structure", "bos", "choch", "nearestBullishOrderBlock",
    "nearestBearishOrderBlock", "recentSweep"}, or None if there isn't
    enough candle history for the underlying building blocks yet.
    """
    structure = analyze_market_structure(candles, lookback)
    if structure is None:
        return None

    bos = analyze_break_of_structure(candles, lookback)
    choch = analyze_change_of_character(candles, lookback)
    order_blocks = analyze_order_blocks(candles) or {"bullish": [], "bearish": []}
    sweeps = analyze_liquidity_sweeps(candles, lookback)

    current_price = candles[-1]["close"]

    def nearest(zone_list):
        if not zone_list:
            return None
        return min(zone_list, key=lambda z: abs((z["top"] + z["bottom"]) / 2 - current_price))

    recent_sweep = None
    if sweeps:
        candidates = sweeps["buySideSweeps"][:1] + sweeps["sellSideSweeps"][:1]
        if candidates:
            recent_sweep = max(candidates, key=lambda s: s["time"])

    return {
        "structure": structure["structure"],
        "bos": bos,
        "choch": choch,
        "nearestBullishOrderBlock": nearest(order_blocks["bullish"]),
        "nearestBearishOrderBlock": nearest(order_blocks["bearish"]),
        "recentSweep": recent_sweep,
    }
