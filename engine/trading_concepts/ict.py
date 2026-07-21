# ict.py - ICT (Inner Circle Trader)
# CONCEPT - combines market structure, order blocks, fair value gaps,
# liquidity identification, and premium/discount zones into one
# ICT-style read: the trend, the two most relevant "draw on liquidity"
# targets, and the nearest unmitigated order block / open FVG the
# current price could react from.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import analyze_market_structure
from .order_blocks import analyze_order_blocks
from .fair_value_gaps import analyze_fair_value_gaps
from .liquidity_identification import analyze_liquidity_identification
from .premium_discount_zones import analyze_premium_discount_zones


def analyze_ict(candles, lookback=3):
    """
    Returns a combined ICT read: {"bias", "zone", "drawOnLiquidity":
    {"buySide", "sellSide"}, "nearestOrderBlock", "nearestFvg"}, or
    None if there isn't enough candle history for the underlying
    building blocks yet.
    """
    structure = analyze_market_structure(candles, lookback)
    if structure is None:
        return None

    order_blocks = analyze_order_blocks(candles) or {"bullish": [], "bearish": []}
    fvgs = analyze_fair_value_gaps(candles) or {"bullish": [], "bearish": []}
    liquidity = analyze_liquidity_identification(candles, lookback)
    zones = analyze_premium_discount_zones(candles, lookback)

    current_price = candles[-1]["close"]
    bias = "bullish" if structure["structure"] == "uptrend" else "bearish" if structure["structure"] == "downtrend" else "neutral"

    # "Draw on liquidity" - the nearest untouched pool above and below
    # price, which ICT treats as the most likely next target.
    draw_on_liquidity = {"buySide": None, "sellSide": None}
    if liquidity:
        buy_pools = sorted(liquidity["restingBuySide"] + liquidity["equalHighs"], key=lambda p: p["price"])
        buy_pools = [p for p in buy_pools if p["price"] > current_price]
        sell_pools = sorted(liquidity["restingSellSide"] + liquidity["equalLows"], key=lambda p: -p["price"])
        sell_pools = [p for p in sell_pools if p["price"] < current_price]
        draw_on_liquidity["buySide"] = buy_pools[0] if buy_pools else None
        draw_on_liquidity["sellSide"] = sell_pools[0] if sell_pools else None

    def nearest(zone_list):
        if not zone_list:
            return None
        return min(zone_list, key=lambda z: abs((z["top"] + z["bottom"]) / 2 - current_price))

    nearest_ob = nearest(order_blocks["bullish"] if bias == "bullish" else order_blocks["bearish"])
    nearest_fvg = nearest(fvgs["bullish"] if bias == "bullish" else fvgs["bearish"])

    return {
        "bias": bias,
        "zone": zones["currentZone"] if zones else None,
        "drawOnLiquidity": draw_on_liquidity,
        "nearestOrderBlock": nearest_ob,
        "nearestFvg": nearest_fvg,
    }
