# core/indicators/__init__.py
# Phase 4.1: this package replaces the old single core/indicators.py
# file - one file per indicator (22 total, see analysis.py's
# INDICATOR_KEYS), so each indicator can be read, tested, and tweaked
# on its own instead of scrolling one 650-line file.
#
# This __init__.py re-exports every function so existing imports
# elsewhere in the codebase (http_server.py, core/signal_engine.py,
# tests/test_indicators.py) keep working completely unchanged -
# `from core.indicators import compute_rsi` still works exactly as it
# did when indicators.py was a single file, it just now resolves to
# core/indicators/rsi.py under the hood.

from .sma import compute_sma
from .ema import compute_ema_series, compute_ema, compute_all_ema_periods
from .rsi import compute_rsi
from .stochastic_rsi import compute_stochastic_rsi
from .macd import compute_macd
from .bollinger_bands import compute_bollinger
from .atr import compute_atr_series, compute_atr
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
from .analysis import compute_all_indicators, INDICATOR_KEYS


def resample_candles(candles, factor):
    """
    Bitget doesn't offer a native "2h" granularity (confirmed against
    both SUPPORTED_GRANULARITIES and BITGET_FUTURES_GRANULARITIES in
    services/bitget_api.py - only 1h and 4h exist around it). To honor
    Phase 4's required 2H timeframe, this aggregates every `factor`
    consecutive 1h candles (factor=2) into one synthetic 2h candle:
    open = first candle's open, close = last candle's close, high/low
    = max/min across the group, volume = summed. Only full groups are
    kept (a leftover partial group at the start is dropped so every
    output candle represents a genuine `factor`-candle window, not a
    half-formed one). Not itself one of the 22 indicators - kept here
    as a shared candle utility since every indicator in this package
    depends on the candle window already being at the right timeframe.
    """
    if factor <= 1 or not candles:
        return list(candles)

    remainder = len(candles) % factor
    trimmed = candles[remainder:] if remainder else candles

    out = []
    for i in range(0, len(trimmed), factor):
        group = trimmed[i:i + factor]
        if len(group) < factor:
            continue
        out.append({
            "time": group[0]["time"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "volume": sum(c["volume"] for c in group),
        })
    return out
