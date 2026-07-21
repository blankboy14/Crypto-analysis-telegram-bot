# volume_analysis.py - Volume Analysis
# A general-purpose volume summary (distinct from the more specific
# RVOL / Volume Spikes / Volume Profile / Buy-Sell / Delta indicators):
# average volume, the latest candle's volume vs that average, and a
# simple rising/falling/flat read comparing the newer half of the
# window against the older half. Works on full OHLCV candle dicts,
# oldest -> newest.


def compute_volume_analysis(candles):
    """Returns {avgVolume, latestVolume, volumeVsAvgPct, trend}."""
    if not candles:
        return None

    volumes = [c["volume"] for c in candles]
    avg_volume = sum(volumes) / len(volumes)
    latest_volume = volumes[-1]

    volume_vs_avg_pct = (
        ((latest_volume - avg_volume) / avg_volume) * 100 if avg_volume > 0 else 0.0
    )

    half = len(volumes) // 2
    trend = "flat"
    if half > 0:
        older_avg = sum(volumes[:half]) / half
        newer_avg = sum(volumes[half:]) / (len(volumes) - half)
        if older_avg > 0:
            change_pct = (newer_avg - older_avg) / older_avg
            if change_pct > 0.1:
                trend = "rising"
            elif change_pct < -0.1:
                trend = "falling"

    return {
        "avgVolume": avg_volume,
        "latestVolume": latest_volume,
        "volumeVsAvgPct": volume_vs_avg_pct,
        "trend": trend,
    }
