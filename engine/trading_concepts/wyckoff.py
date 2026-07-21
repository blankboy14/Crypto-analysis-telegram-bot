# wyckoff.py - Wyckoff Method
# CONCEPT - a simplified, rule-of-thumb Wyckoff phase read. Full manual
# Wyckoff phase-lettering (A-E with springs/upthrusts/tests) needs
# discretionary judgment a formula can only approximate - this looks
# for the two most codifiable signatures: a trading range (proxy for
# "we're in accumulation or distribution somewhere") and which
# direction volume is confirming.
#
# Works on full OHLCV candle dicts, oldest -> newest.


def analyze_wyckoff(candles, range_lookback=30, range_tightness_pct=8.0):
    """
    Returns {"phaseGuess", "inTradingRange", "rangeHigh", "rangeLow",
    "volumeBias"}. `phaseGuess` is one of "accumulation",
    "distribution", "markup", "markdown", or "undetermined" - a
    best-effort label, not a certified Wyckoff phase letter.
    """
    if len(candles) < range_lookback:
        return None

    window = candles[-range_lookback:]
    range_high = max(c["high"] for c in window)
    range_low = min(c["low"] for c in window)
    range_pct = (range_high - range_low) / range_low * 100 if range_low > 0 else 0
    in_range = range_pct <= range_tightness_pct

    closes = [c["close"] for c in window]
    volumes = [c["volume"] for c in window]
    half = len(window) // 2
    early_vol = sum(volumes[:half]) / max(half, 1)
    late_vol = sum(volumes[half:]) / max(len(volumes) - half, 1)
    price_trend_up = closes[-1] > closes[0]

    if late_vol > early_vol * 1.15:
        volume_bias = "rising"
    elif late_vol < early_vol * 0.85:
        volume_bias = "falling"
    else:
        volume_bias = "flat"

    if in_range:
        # Range + rising volume with price holding near the range's lower
        # half looks more like accumulation; near the upper half with
        # rising volume looks more like distribution.
        position_in_range = (closes[-1] - range_low) / (range_high - range_low) if range_high > range_low else 0.5
        if volume_bias == "rising" and position_in_range < 0.5:
            phase_guess = "accumulation"
        elif volume_bias == "rising" and position_in_range >= 0.5:
            phase_guess = "distribution"
        else:
            phase_guess = "undetermined"
    else:
        phase_guess = "markup" if price_trend_up else "markdown"

    return {
        "phaseGuess": phase_guess,
        "inTradingRange": in_range,
        "rangeHigh": range_high,
        "rangeLow": range_low,
        "volumeBias": volume_bias,
    }
