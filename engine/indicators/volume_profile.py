# volume_profile.py - Volume Profile
# Works on full OHLCV candle dicts, oldest -> newest.


def compute_volume_profile(candles, num_bins=24):
    """
    Distributes total volume across price bins over the given window
    and returns the Point of Control (POC - the price level with the
    most volume traded) plus the full bin breakdown. A simplified
    approximation: each candle's volume is credited entirely to the bin
    containing its close (not split proportionally across its H-L
    range), which is the standard lightweight approach when only
    OHLCV candles are available (no tick-level trade prices).
    """
    if not candles:
        return None

    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    if hi == lo:
        return None

    bin_size = (hi - lo) / num_bins
    bins = [0.0] * num_bins

    for c in candles:
        idx = int((c["close"] - lo) / bin_size)
        idx = min(max(idx, 0), num_bins - 1)
        bins[idx] += c["volume"]

    poc_idx = bins.index(max(bins))
    poc_price = lo + (poc_idx + 0.5) * bin_size

    return {
        "poc": poc_price,
        "bins": [
            {"priceLow": lo + i * bin_size, "priceHigh": lo + (i + 1) * bin_size, "volume": bins[i]}
            for i in range(num_bins)
        ],
    }