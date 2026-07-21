# liquidity_identification.py - Liquidity Identification (VERY IMPORTANT)
# BUILDING BLOCK - used by ict.py and smc.py, and consumed by
# liquidity_sweeps.py's caller to know which pools have already been
# swept vs are still resting.
#
# The single most load-bearing concept in ICT/SMC: price is drawn
# toward pools of resting stop-loss / breakout orders ("liquidity")
# before it makes its "real" move. Identifying WHERE that liquidity
# sits is the basis almost every other concept in this package reasons
# from (order blocks, FVGs, and sweeps all reference these pools).
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import find_swing_points


def analyze_liquidity_identification(candles, lookback=3, equal_tolerance_pct=0.1, max_pools=10):
    """
    Identifies three kinds of resting liquidity:
    1. Equal highs/lows ("EQH"/"EQL") - two or more swing highs (or
       lows) within `equal_tolerance_pct`% of each other. The tightest,
       most obvious stop-loss cluster - very high-probability sweep targets.
    2. Old (unswept) single swing highs/lows - buy-side / sell-side
       liquidity resting above/below the market.
    3. Session extremes (see session_analysis.py for the session
       windows) are a related but separate liquidity source and are
       NOT duplicated here to avoid double-counting the same pool.

    Returns {"equalHighs": [...], "equalLows": [...],
             "restingBuySide": [...], "restingSellSide": [...]}
    (each entry: {"price", "touches", "time"}), most significant first,
    or None if there isn't enough swing history yet.
    """
    swings = find_swing_points(candles, lookback)
    if len(swings) < 3:
        return None

    current_price = candles[-1]["close"]
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    def find_equal_clusters(points):
        clusters = []
        used = set()
        for i, a in enumerate(points):
            if i in used:
                continue
            group = [a]
            for j, b in enumerate(points):
                if j <= i or j in used:
                    continue
                if abs(a["price"] - b["price"]) / a["price"] * 100 <= equal_tolerance_pct:
                    group.append(b)
                    used.add(j)
            if len(group) >= 2:
                used.add(i)
                avg_price = sum(g["price"] for g in group) / len(group)
                clusters.append({"price": round(avg_price, 8), "touches": len(group), "time": group[-1]["time"]})
        return clusters

    equal_highs = sorted(find_equal_clusters(highs), key=lambda c: -c["touches"])[:max_pools]
    equal_lows = sorted(find_equal_clusters(lows), key=lambda c: -c["touches"])[:max_pools]

    equal_high_prices = {c["price"] for c in equal_highs}
    equal_low_prices = {c["price"] for c in equal_lows}

    # "Resting" single swings above/below current price that HAVEN'T since been swept.
    resting_buy_side = []
    for h in highs:
        if h["price"] in equal_high_prices or h["price"] <= current_price:
            continue
        swept = any(c["time"] > h["time"] and c["high"] > h["price"] for c in candles)
        if not swept:
            resting_buy_side.append({"price": h["price"], "touches": 1, "time": h["time"]})

    resting_sell_side = []
    for lo in lows:
        if lo["price"] in equal_low_prices or lo["price"] >= current_price:
            continue
        swept = any(c["time"] > lo["time"] and c["low"] < lo["price"] for c in candles)
        if not swept:
            resting_sell_side.append({"price": lo["price"], "touches": 1, "time": lo["time"]})

    resting_buy_side = sorted(resting_buy_side, key=lambda c: c["price"])[:max_pools]
    resting_sell_side = sorted(resting_sell_side, key=lambda c: -c["price"])[:max_pools]

    return {
        "equalHighs": equal_highs,
        "equalLows": equal_lows,
        "restingBuySide": resting_buy_side,
        "restingSellSide": resting_sell_side,
    }
