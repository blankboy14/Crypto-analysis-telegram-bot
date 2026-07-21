# support_resistance.py - Support & Resistance
# BUILDING BLOCK - shares swing points with market_structure.py, then
# clusters them into horizontal levels that got tested more than once.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import find_swing_points


def analyze_support_resistance(candles, lookback=3, cluster_tolerance_pct=0.5, min_touches=1):
    """
    Groups swing highs into resistance clusters and swing lows into
    support clusters (any two swings within `cluster_tolerance_pct`%
    of each other count as the same level being retested).

    A single untested swing is still a real, watchable level (that's
    how every trader reads a chart - a lone swing high is resistance
    the moment it forms, not only after it's been hit twice), so
    `min_touches` defaults to 1; `touches` is still returned per level
    so a level retested multiple times can be told apart from a
    one-off wick.

    Returns {"resistance": [{"price", "touches"}], "support": [...]}
    sorted nearest-to-current-price first, or None if there's not
    enough swing history.
    """
    swings = find_swing_points(candles, lookback)
    if len(swings) < min_touches:
        return None

    current_price = candles[-1]["close"]

    def cluster(points):
        clusters = []
        for p in sorted(points, key=lambda s: s["price"]):
            placed = False
            for c in clusters:
                if abs(p["price"] - c["avgPrice"]) / c["avgPrice"] * 100 <= cluster_tolerance_pct:
                    c["prices"].append(p["price"])
                    c["avgPrice"] = sum(c["prices"]) / len(c["prices"])
                    c["touches"] += 1
                    placed = True
                    break
            if not placed:
                clusters.append({"avgPrice": p["price"], "prices": [p["price"]], "touches": 1})
        return [
            {"price": round(c["avgPrice"], 8), "touches": c["touches"]}
            for c in clusters if c["touches"] >= min_touches
        ]

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    # A resistance level only means something if it's still ABOVE current
    # price (a ceiling price could react down from); a support level only
    # means something if it's still BELOW current price (a floor price
    # could bounce off). Sorting by raw absolute distance alone let an old
    # swing-high cluster that's now sitting below current price (e.g. from
    # early consolidation before a big rally) surface as the "nearest
    # resistance" even though it's no longer above price at all.
    resistance_clusters = [c for c in cluster(highs) if c["price"] > current_price]
    support_clusters = [c for c in cluster(lows) if c["price"] < current_price]

    resistance = sorted(resistance_clusters, key=lambda c: abs(c["price"] - current_price))
    support = sorted(support_clusters, key=lambda c: abs(c["price"] - current_price))

    return {"resistance": resistance[:8], "support": support[:8]}