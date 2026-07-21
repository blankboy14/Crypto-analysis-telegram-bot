# market_structure.py - Market Structure
# BUILDING BLOCK - order_blocks.py, break_of_structure.py,
# change_of_character.py, trend_structure.py, ict.py, and smc.py all
# depend on the swing points this file finds.
#
# Works on full OHLCV candle dicts, oldest -> newest.


def find_swing_points(candles, lookback=3):
    """
    A swing HIGH is a candle whose high is higher than `lookback`
    candles on both sides; a swing LOW is the mirror. Returns
    [{"index", "time", "price", "type": "high"|"low"}, ...] in candle
    order. This is the classic fractal-style swing detector most
    structure/SMC/ICT tooling is built on.
    """
    swings = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window = candles[i - lookback:i + lookback + 1]
        this_high = candles[i]["high"]
        this_low = candles[i]["low"]

        if this_high == max(c["high"] for c in window):
            swings.append({"index": i, "time": candles[i]["time"], "price": this_high, "type": "high"})
        elif this_low == min(c["low"] for c in window):
            swings.append({"index": i, "time": candles[i]["time"], "price": this_low, "type": "low"})

    return swings


def analyze_market_structure(candles, lookback=3):
    """
    Walks the swing points in order and classifies the sequence as:
    - "uptrend"   : most recent swings are higher-highs + higher-lows
    - "downtrend" : most recent swings are lower-highs + lower-lows
    - "ranging"   : mixed / no clear sequence yet
    Returns {"structure", "swings", "lastSwingHigh", "lastSwingLow"} or
    None if there isn't enough candle history for even one swing pair yet.
    """
    swings = find_swing_points(candles, lookback)
    if len(swings) < 4:
        return None

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None

    higher_highs = highs[-1]["price"] > highs[-2]["price"]
    higher_lows = lows[-1]["price"] > lows[-2]["price"]
    lower_highs = highs[-1]["price"] < highs[-2]["price"]
    lower_lows = lows[-1]["price"] < lows[-2]["price"]

    # Whichever of the last swing high / last swing low price has most
    # recently closed beyond always wins - that single break is more
    # current than a static HH/HL or LH/LL pattern, and it's the exact
    # same reactive signal break_of_structure.py and change_of_character.py
    # already act on. Previously structure only used the HH/HL pattern, so
    # it could keep reporting "uptrend" (or fall back to "ranging") for a
    # full extra swing pair's worth of candles after price had already
    # broken down - which is what produced "Bearish BOS" next to "Ranging"
    # structure, and cascaded into ICT/SMC/Trend Structure/CHoCH all
    # reading that same stale field. The HH/HL pattern is now only a
    # fallback for the rare case where no break has happened yet.
    last_high = highs[-1]
    last_low = lows[-1]
    break_up_times = [c["time"] for c in candles if c["time"] > last_high["time"] and c["close"] > last_high["price"]]
    break_down_times = [c["time"] for c in candles if c["time"] > last_low["time"] and c["close"] < last_low["price"]]

    if break_up_times or break_down_times:
        latest_up = max(break_up_times) if break_up_times else -1
        latest_down = max(break_down_times) if break_down_times else -1
        structure = "uptrend" if latest_up > latest_down else "downtrend"
    elif higher_highs and higher_lows:
        structure = "uptrend"
    elif lower_highs and lower_lows:
        structure = "downtrend"
    else:
        structure = "ranging"

    return {
        "structure": structure,
        "swings": swings[-12:],  # trim to the most recent 12 for a manageable payload
        "lastSwingHigh": highs[-1],
        "lastSwingLow": lows[-1],
    }