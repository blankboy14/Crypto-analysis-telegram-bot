# delta_volume.py - Delta Volume
# Net (buy - sell) volume, on top of buy_sell_volume.py's estimated
# split. Positive delta = net buying pressure over the window,
# negative = net selling pressure. Works on full OHLCV candle dicts,
# oldest -> newest.

from .buy_sell_volume import compute_buy_sell_volume


def compute_delta_volume(candles):
    """Returns {delta, estimated} for the given candle window."""
    split = compute_buy_sell_volume(candles)
    if split is None:
        return None
    return {"delta": split["delta"], "estimated": split["estimated"]}
