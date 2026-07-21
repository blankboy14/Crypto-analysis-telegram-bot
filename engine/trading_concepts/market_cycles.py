# market_cycles.py - Market Cycles
# CONCEPT - the broader accumulation -> markup -> distribution ->
# markdown cycle (the same 4-phase idea Wyckoff formalized - see
# wyckoff.py for the more granular phase read). This one looks at a
# LONGER window than wyckoff.py to estimate roughly where in the
# bigger cycle price currently sits, using trend + how far price has
# already moved from its recent range extremes.
#
# Works on full OHLCV candle dicts, oldest -> newest.


def analyze_market_cycles(candles, cycle_lookback=100):
    """
    Returns {"cyclePhase", "cycleHigh", "cycleLow", "pctFromLow",
    "pctFromHigh"}. `cyclePhase` is a best-effort label:
    "accumulation" (near the cycle low, flattening), "markup" (trending
    up, well off the low), "distribution" (near the cycle high,
    flattening), "markdown" (trending down, well off the high).
    """
    if len(candles) < cycle_lookback:
        return None

    window = candles[-cycle_lookback:]
    cycle_high = max(c["high"] for c in window)
    cycle_low = min(c["low"] for c in window)
    current = candles[-1]["close"]
    span = cycle_high - cycle_low
    if span <= 0:
        return None

    pct_from_low = (current - cycle_low) / span * 100
    pct_from_high = (cycle_high - current) / span * 100

    recent = window[-max(cycle_lookback // 5, 5):]
    recent_range_pct = (max(c["high"] for c in recent) - min(c["low"] for c in recent)) / span * 100
    flattening = recent_range_pct <= 20  # recent price action confined to a small slice of the full cycle range

    if pct_from_low <= 25 and flattening:
        phase = "accumulation"
    elif pct_from_high <= 25 and flattening:
        phase = "distribution"
    elif pct_from_low > pct_from_high:
        phase = "markup"
    else:
        phase = "markdown"

    return {
        "cyclePhase": phase,
        "cycleHigh": cycle_high,
        "cycleLow": cycle_low,
        "pctFromLow": round(pct_from_low, 1),
        "pctFromHigh": round(pct_from_high, 1),
    }
