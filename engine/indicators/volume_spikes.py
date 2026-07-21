# volume_spikes.py - Volume Spikes
# Works on full OHLCV candle dicts, oldest -> newest.


def detect_volume_spikes(candles, period=20, threshold=2.0):
    """
    Flags candles whose volume exceeds `threshold`x the trailing
    average volume. Returns a list of {time, volume, rvol} for every
    spike found in the given window (not just the latest candle).
    """
    if len(candles) < period + 1:
        return []

    spikes = []
    for i in range(period, len(candles)):
        baseline = candles[i - period:i]
        avg_vol = sum(c["volume"] for c in baseline) / period
        if avg_vol == 0:
            continue
        rvol = candles[i]["volume"] / avg_vol
        if rvol >= threshold:
            spikes.append({"time": candles[i]["time"], "volume": candles[i]["volume"], "rvol": round(rvol, 2)})
    return spikes
