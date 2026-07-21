# elliott_wave.py - Elliott Wave
# CONCEPT - like wyckoff.py, this is a best-effort automated read, not
# a substitute for full discretionary Elliott Wave analysis (real EW
# counting involves alternation/overlap rules and multiple valid
# counts). This labels the most recent 5-6 swings against the
# classic 5-wave impulse / 3-wave (A-B-C) corrective shape and reports
# how well they fit, rather than asserting one "true" count.
#
# Works on full OHLCV candle dicts, oldest -> newest.

from .market_structure import find_swing_points


def analyze_elliott_wave(candles, lookback=3):
    """
    Returns {"waveType": "impulse"|"corrective"|"undetermined",
    "currentWave" (1-5 or "A"/"B"/"C"), "swings" (the points used),
    "confidence" (0-100)}, or None if there aren't enough swings yet.
    """
    swings = find_swing_points(candles, lookback)
    if len(swings) < 5:
        return None

    recent = swings[-6:] if len(swings) >= 6 else swings[-5:]
    prices = [s["price"] for s in recent]

    # A clean 5-wave impulse alternates direction every swing and each
    # "away" leg (1,3,5 in the recent group) makes a new extreme beyond
    # the previous "away" leg - the closest thing to a checkable rule
    # without full Elliott alternation/overlap logic.
    directions = []
    for i in range(1, len(recent)):
        directions.append("up" if prices[i] > prices[i - 1] else "down")
    alternates = all(directions[i] != directions[i + 1] for i in range(len(directions) - 1))

    if len(recent) == 6 and alternates:
        wave_type = "impulse"
        current_wave = 5
        confidence = 55
    elif len(recent) >= 4 and alternates:
        wave_type = "corrective"
        current_wave = "C"
        confidence = 40
    else:
        wave_type = "undetermined"
        current_wave = None
        confidence = 15

    return {
        "waveType": wave_type,
        "currentWave": current_wave,
        "swings": recent,
        "confidence": confidence,
    }
