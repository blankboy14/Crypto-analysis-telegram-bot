# institutional_order_flow.py - Institutional Order Flow
# CONCEPT - combines core/indicators/buy_sell_volume.py's estimated
# buy/sell split with market_structure.py's trend read, to infer
# whether volume is confirming or diverging from price direction -
# a classic "smart money footprint" cross-check (same read a trader
# gets by eyeballing volume delta against structure).
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import analyze_market_structure
from ..indicators.buy_sell_volume import compute_buy_sell_volume


def analyze_institutional_order_flow(candles, lookback=3, recent_window=50):
    """
    Returns {"structure", "buyVolume", "sellVolume", "delta",
    "estimated", "confirmation": "confirming"|"diverging"|"neutral"}.
    "confirming" = volume delta agrees with the structural trend
    direction (real buying behind an uptrend, real selling behind a
    downtrend); "diverging" = the opposite (price trending one way on
    weak/opposite volume - a classic reversal warning); "neutral"
    otherwise. None if there isn't enough candle history.
    """
    structure_info = analyze_market_structure(candles, lookback)
    split = compute_buy_sell_volume(candles[-recent_window:])
    if structure_info is None or split is None:
        return None

    structure = structure_info["structure"]
    delta = split["delta"]

    if structure == "uptrend" and delta > 0:
        confirmation = "confirming"
    elif structure == "downtrend" and delta < 0:
        confirmation = "confirming"
    elif structure == "uptrend" and delta < 0:
        confirmation = "diverging"
    elif structure == "downtrend" and delta > 0:
        confirmation = "diverging"
    else:
        confirmation = "neutral"

    return {
        "structure": structure,
        "buyVolume": split["buyVolume"],
        "sellVolume": split["sellVolume"],
        "delta": delta,
        "estimated": split["estimated"],
        "confirmation": confirmation,
    }
