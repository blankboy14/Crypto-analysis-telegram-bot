# exchange_adapter.py
# Maps a "source" tag (set by the extension per content script, or by a
# manually-opened token card) to the right fetch function and its
# supported granularity set - this is what lets the dashboard
# transparently support Bitget spot and Bitget futures through one
# unified /api/candles-fresh endpoint without the route itself needing
# to know exchange details.
#
# MEXC support has been removed entirely (was only ever reachable via
# the extension's tradingview-scraper.js, which is no longer part of
# this project) - Bitget spot + futures are the only exchanges tracked.

from .bitget_api import (
    fetch_bitget_spot_candles,
    fetch_bitget_futures_candles,
    SUPPORTED_GRANULARITIES,
    BITGET_FUTURES_GRANULARITIES,
)

EXCHANGE_ADAPTERS = {
    "bitget-spot": {
        "fetch": fetch_bitget_spot_candles,
        "granularities": SUPPORTED_GRANULARITIES,
    },
    "bitget-futures": {
        "fetch": fetch_bitget_futures_candles,
        "granularities": BITGET_FUTURES_GRANULARITIES,
    },
}