# premium_discount_zones.py - Premium & Discount Zones
# BUILDING BLOCK - used by ict.py. Splits the current dealing range
# (most recent swing high to swing low) into three zones using
# fibonacci.py's 0.5 retracement as the equilibrium line - ICT's rule
# of thumb is "buy in discount, sell in premium."
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .fibonacci import analyze_fibonacci


def analyze_premium_discount_zones(candles, lookback=3):
    """
    Returns {"equilibrium", "premiumZone": {"top","bottom"},
    "discountZone": {"top","bottom"}, "currentZone", "currentPrice"} -
    currentZone is one of "premium" | "equilibrium" | "discount".
    None if there isn't a usable swing range yet (see fibonacci.py).
    """
    fib = analyze_fibonacci(candles, lookback)
    if fib is None:
        return None

    high_price = fib["swingHigh"]["price"]
    low_price = fib["swingLow"]["price"]
    equilibrium = fib["retracements"][0.5]
    current_price = candles[-1]["close"]

    # A thin equilibrium band around the 0.5 line (5% of the range) counts
    # as neither premium nor discount - price sitting basically at fair value.
    band = (high_price - low_price) * 0.05
    if abs(current_price - equilibrium) <= band:
        zone = "equilibrium"
    elif current_price > equilibrium:
        zone = "premium"
    else:
        zone = "discount"

    return {
        "equilibrium": equilibrium,
        "premiumZone": {"top": high_price, "bottom": equilibrium + band},
        "discountZone": {"top": equilibrium - band, "bottom": low_price},
        "currentZone": zone,
        "currentPrice": current_price,
    }
