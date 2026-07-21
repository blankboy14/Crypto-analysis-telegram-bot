# analysis.py - runs every indicator in this package against one candle
# window and returns everything in one dict. This is what /api/indicators
# and the Phase 4.1 Indicators page ultimately hand back to the frontend.
#
# Each entry is None (or omitted where naturally empty, e.g. volume
# spikes) if there wasn't enough history for that particular indicator
# yet, rather than raising an error - a young pair with only 30 candles
# should still get back whatever indicators ARE computable instead of a
# hard failure.
#
# `enabled` (optional) is the on/off toggle map from the Indicators page
# ("ACTIVE ALL INDICATOR" / per-indicator toggles) - INDICATOR_KEYS name
# -> True/False. When given, disabled indicators are skipped entirely
# (not computed, not included in the result) instead of being computed
# and thrown away, so toggling indicators off on a 300+ pair sweep
# actually saves CPU time, not just screen space.

import logging

from .sma import compute_sma
from .ema import compute_ema, compute_all_ema_periods
from .rsi import compute_rsi
from .stochastic_rsi import compute_stochastic_rsi
from .macd import compute_macd
from .bollinger_bands import compute_bollinger
from .atr import compute_atr
from .adx import compute_adx
from .supertrend import compute_supertrend
from .vwap import compute_vwap
from .volume_profile import compute_volume_profile
from .pivot_points import compute_pivot_points
from .ichimoku import compute_ichimoku
from .parabolic_sar import compute_parabolic_sar
from .cci import compute_cci
from .mfi import compute_mfi
from .obv import compute_obv
from .volume_analysis import compute_volume_analysis
from .buy_sell_volume import compute_buy_sell_volume
from .delta_volume import compute_delta_volume
from .rvol import compute_rvol
from .volume_spikes import detect_volume_spikes

log = logging.getLogger("crypto-analyzer-http")

# The 22 indicators from the Phase 4.1 spec, in spec order. Shared with
# http_server.py (toggle state defaults to True for every key here) and
# the Indicators page (renders one toggle switch + one card per key).
INDICATOR_KEYS = [
    "rsi", "macd", "ema", "sma", "bollinger", "stochRsi", "atr", "adx",
    "superTrend", "vwap", "volumeProfile", "pivotPoints", "ichimoku",
    "parabolicSar", "cci", "mfi", "obv", "volumeAnalysis",
    "buySellVolume", "deltaVolume", "rvol", "volumeSpikes",
]


def compute_all_indicators(candles, enabled=None, return_errors=False):
    """
    Runs every indicator above against one candle window. `enabled` is
    an optional {key: bool} map (see INDICATOR_KEYS) - indicators
    mapped to False are skipped and simply absent from the result.
    Omitting `enabled` entirely computes everything (default: all on).

    Each indicator runs in its own try/except - previously a single
    indicator throwing an unexpected exception (e.g. on unusual candle
    data for one specific pair) would bubble all the way up and fail
    the ENTIRE pair for every other indicator too, which made a narrow
    bug look like "this whole pair has no data" instead of pointing at
    the one indicator actually responsible. Failures are now isolated
    and (if return_errors=True) reported back per-indicator so a
    genuinely broken indicator is actually diagnosable instead of just
    silently blank.
    """
    def is_on(key):
        return enabled is None or enabled.get(key, True)

    closes = [c["close"] for c in candles]
    out = {}
    errors = {}

    def run(key, fn):
        if not is_on(key):
            return
        try:
            out[key] = fn()
        except Exception as exc:
            log.error(f"Indicator '{key}' failed: {exc}")
            errors[key] = str(exc)
            out[key] = None

    if is_on("sma"):
        try:
            out["sma20"] = compute_sma(closes, 20)
            out["sma50"] = compute_sma(closes, 50)
            out["sma200"] = compute_sma(closes, 200)
        except Exception as exc:
            log.error(f"Indicator 'sma' failed: {exc}")
            errors["sma"] = str(exc)
    else:
        out.pop("sma", None)

    run("ema", lambda: compute_all_ema_periods(closes))
    run("rsi", lambda: compute_rsi(closes))
    run("stochRsi", lambda: compute_stochastic_rsi(closes))
    run("macd", lambda: compute_macd(closes))
    run("bollinger", lambda: compute_bollinger(closes))
    run("atr", lambda: compute_atr(candles))
    run("adx", lambda: compute_adx(candles))
    run("superTrend", lambda: compute_supertrend(candles))
    run("vwap", lambda: compute_vwap(candles))
    run("volumeProfile", lambda: compute_volume_profile(candles))
    run("pivotPoints", lambda: compute_pivot_points(candles[-2]) if len(candles) >= 2 else None)
    run("ichimoku", lambda: compute_ichimoku(candles))
    run("parabolicSar", lambda: compute_parabolic_sar(candles))
    run("cci", lambda: compute_cci(candles))
    run("mfi", lambda: compute_mfi(candles))
    run("obv", lambda: compute_obv(candles))
    run("volumeAnalysis", lambda: compute_volume_analysis(candles))
    run("buySellVolume", lambda: compute_buy_sell_volume(candles[-50:]))
    run("deltaVolume", lambda: compute_delta_volume(candles[-50:]))
    run("rvol", lambda: compute_rvol(candles))
    run("volumeSpikes", lambda: detect_volume_spikes(candles))

    if return_errors:
        return out, errors
    return out