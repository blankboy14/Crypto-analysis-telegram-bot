# supply_demand.py - Supply & Demand
# BUILDING BLOCK - similar idea to order_blocks.py but zone-based
# (a small consolidation range) rather than single-candle based.
#
# Works on full OHLCV candle dicts, oldest -> newest.


def analyze_supply_demand(candles, base_len=3, move_atr_mult=1.5, lookahead=6, max_zones=6):
    """
    Looks for a short flat "base" (`base_len` candles with a narrow
    high-low range) immediately followed by a strong directional move
    away from it (>= `move_atr_mult` x that base's average range). A
    demand zone is a base followed by a strong move UP (price likely
    to react bullishly if it returns there); a supply zone is a base
    followed by a strong move DOWN. Only zones price hasn't fully
    returned through yet are kept - once price closes back through a
    zone, that zone is considered used up.

    Returns {"demand": [{"top","bottom","time"}], "supply": [...]}
    (most recent first) or None if there isn't enough candle history.
    """
    n = len(candles)
    if n < base_len + lookahead + 5:
        return None

    demand, supply = [], []

    # NOTE: previously this was `range(base_len, n - lookahead)`, which
    # silently excluded the most recent `lookahead` candles from ever being
    # scanned as a zone at all. That's exactly where a zone from a recent
    # reversal (e.g. the top of a rally that just dumped) would sit, so it
    # was systematically invisible rather than just filtered out later.
    # `lookahead` is kept as an accepted argument for compatibility but is
    # no longer used to shrink the scan range - the "usedUp" pass below
    # already checks all subsequent candles for follow-through/invalidation.
    for i in range(base_len, n):
        base = candles[i - base_len:i]
        base_ranges = [c["high"] - c["low"] for c in base]
        avg_range = sum(base_ranges) / len(base_ranges)
        if avg_range == 0:
            continue

        base_top = max(c["high"] for c in base)
        base_bottom = min(c["low"] for c in base)

        move_candle = candles[i]
        move_size = abs(move_candle["close"] - move_candle["open"])
        if move_size < avg_range * move_atr_mult:
            continue

        if move_candle["close"] > move_candle["open"]:
            demand.append({"top": base_top, "bottom": base_bottom, "time": base[0]["time"], "usedUp": False})
        else:
            supply.append({"top": base_top, "bottom": base_bottom, "time": base[0]["time"], "usedUp": False})

    # Mark zones as used up once price has since closed all the way through them.
    for zone_list, is_demand in ((demand, True), (supply, False)):
        for zone in zone_list:
            for c in candles:
                if c["time"] <= zone["time"]:
                    continue
                if is_demand and c["close"] < zone["bottom"]:
                    zone["usedUp"] = True
                elif not is_demand and c["close"] > zone["top"]:
                    zone["usedUp"] = True

    fresh_demand = [z for z in demand if not z["usedUp"]][-max_zones:][::-1]
    fresh_supply = [z for z in supply if not z["usedUp"]][-max_zones:][::-1]

    return {"demand": fresh_demand, "supply": fresh_supply}