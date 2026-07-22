# bitget_api.py
# Every function here calls Bitget's PUBLIC market-data API - no API key
# needed anywhere in this file. Split out of http_server.py so that file
# doesn't keep growing every time an exchange-specific fix or feature
# gets added (it was pushing 2000+ lines before this split).

import time

from .http_client import http_session as _http_session, log


def _parse_pct_change(raw):
    """
    Bitget's change24h comes back as a raw STRING, and it's a decimal
    FRACTION of price change (e.g. "0.0523" for +5.23%), not already a
    percentage - confirmed from Bitget's own API docs example
    (BTCUSDT change24h: "0.00069"). Two bugs stacked here previously:
    this field was passed straight through unconverted, so (1) it was
    always a str, never an int/float, which meant every downstream
    `isinstance(change24h, (int, float))` display check (see
    bot/formatters.py) failed and showed "N/A" for essentially every
    pair - not intermittently, every single one; and (2) even a naive
    float(raw) without the *100 would have been off by 100x once that
    display bug was fixed. This returns an actual percentage float
    (or None if the field is missing/blank/unparseable, so callers can
    tell "no data" apart from a genuine 0.00%).
    """
    if raw is None or raw == "":
        return None
    try:
        return float(raw) * 100
    except (TypeError, ValueError):
        return None

# --- Bitget public candle APIs (spot + futures/mix) - no key needed ---
BITGET_SPOT_CANDLES_URL = "https://api.bitget.com/api/v2/spot/market/candles"
BITGET_FUTURES_CANDLES_URL = "https://api.bitget.com/api/v2/mix/market/candles"

# Bitget's REGULAR candle endpoints above only keep a rolling window of
# history per granularity (confirmed from Bitget's own docs: 1m/3m/5m
# ~1 month back, 15m ~52 days, 30m ~62 days, 1h ~83 days, 4h ~240 days -
# and 1day/3day/1week/1M turned out to be windowed even more
# aggressively in practice, which is exactly why those longer
# timeframes were only returning ~90/13/4 candles instead of the
# requested 2000). Bitget has a SEPARATE endpoint purpose-built for
# older data - once the regular endpoint runs dry we transparently fall
# back to these for the rest of the pagination (see
# _fetch_bitget_paginated). Confirmed via Bitget's official docs:
# https://www.bitget.com/api-doc/spot/market/Get-History-Candle-Data
# https://www.bitget.com/api-doc/contract/market/Get-History-Candle-Data
BITGET_SPOT_HISTORY_CANDLES_URL = "https://api.bitget.com/api/v2/spot/market/history-candles"
BITGET_FUTURES_HISTORY_CANDLES_URL = "https://api.bitget.com/api/v2/mix/market/history-candles"

# Futures' history-candles endpoint caps out lower than the regular one
# (200 vs 1000 per call), per Bitget's docs.
BITGET_FUTURES_HISTORY_MAX_LIMIT_PER_CALL = 200

# Both spot and futures (mix) use the IDENTICAL granularity string format
# on Bitget, confirmed from their official docs for each endpoint. This
# same long-form map is ALSO what Bitget's history-candles endpoint
# expects for BOTH spot and futures - futures' history-candles uses this
# long-form format even though futures' regular /candles endpoint uses
# the short-form BITGET_FUTURES_GRANULARITIES below instead. Confirmed
# directly from Bitget's history-candles docs for each.
SUPPORTED_GRANULARITIES = {
    "1m": "1min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1day",
    "3d": "3day",
    "1w": "1week",
    "1M": "1M",
}

# Bitget FUTURES (mix) uses a COMPLETELY DIFFERENT granularity string
# format than spot - confirmed directly from Bitget's own API validation
# error (the parameter description in their docs was wrong/outdated):
# "should be [1m,3m,5m,15m,30m,1H,4H,6H,12H,1D,1W,1M,...]"
# Note: short-form minutes (no "min" suffix), CAPITAL H/D/W for
# hour/day/week, and no plain "3D" option at all (only "3Dutc" exists,
# a UTC-offset variant) - so "3d" isn't offered for futures.
BITGET_FUTURES_GRANULARITIES = {
    "1m": "1m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
    "1w": "1W",
    "1M": "1M",
}

# Every timeframe maxed out at 2000 candles, per explicit request - more
# history depth for every timeframe (15m, 1h, 1d, 1M, everything) rather
# than the smaller per-timeframe defaults used before. Trade-off: since
# Bitget's single-call max is 1000, getting 2000 requires 2 sequential
# paginated calls (slightly slower than a single-page fetch, but still
# just 2 calls total, not more).
DEFAULT_CANDLE_COUNTS = {
    "1m": 2000,
    "15m": 2000,
    "30m": 2000,
    "1h": 2000,
    "4h": 2000,
    "1d": 2000,
    "3d": 2000,
    "1w": 2000,
    "1M": 2000,
}

# Bitget's docs claim up to 1000 rows per call, but empirically that's
# NOT reliable: requesting limit=1000 for 1day candles silently returned
# only 300 rows even though 1400+ rows of real history demonstrably
# exist just past that point (confirmed by requesting the same range
# with limit=200 instead, which returned full pages all the way back to
# 2022). This is what was actually truncating 1D/1W/4H history so
# aggressively - NOT an exchange-wide historical window as first
# suspected. Using a smaller, empirically-reliable page size costs a few
# extra API calls (well within Bitget's 20 req/s limit) but actually
# retrieves the real depth of history that's available.
BITGET_MAX_LIMIT_PER_CALL = 200

# Some symbols Bitget lists in its spot ticker feed (e.g. its tokenized
# real-world-stock products - "RHOOD", "RCOIN", "RTSLA"-style pairs
# mirroring actual stock tickers) don't support the regular kline/
# candles endpoint the same way normal crypto pairs do, and Bitget
# rejects them with a hard "Parameter validation failed" (code 48001) -
# not a transient error, the same call will fail again every time.
# Without this cache, every full market scan (Search Signal, Strong
# Signal watcher) re-requests candles for these same known-bad symbols
# every single cycle, forever - pure wasted API calls + log spam, since
# the outcome never changes. This remembers the failure per
# (base_url, symbol, granularity) and skips straight to returning None
# for BAD_SYMBOL_CACHE_TTL_SECONDS, then quietly retries once that
# expires (in case Bitget adds support later) instead of blocking it
# forever on what might turn out to be a stale assumption.
_bad_symbol_cache: dict[tuple, float] = {}
BAD_SYMBOL_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours - these are static "unsupported product" facts, not flaky


def _fetch_bitget_paginated(base_url, symbol, granularity_key, limit, granularity_map,
                             extra_params=None, history_url=None, history_granularity_map=None,
                             history_max_limit=None):
    """
    Shared pagination logic for both Bitget spot and Bitget futures - the
    request/response shape is identical between the two, only the base
    URL, granularity string format, and an extra `productType` param
    (futures only) differ.

    Bitget's REGULAR /candles endpoint only keeps a rolling window of
    history per granularity (see the comment above BITGET_SPOT_HISTORY_
    CANDLES_URL). Once we've walked endTime back past that window, the
    regular endpoint just returns an empty (or partial) page - previously
    we just stopped there, which is why 1D/1W/1M in particular came back
    with far fewer candles than requested even though plenty of older
    data actually exists on Bitget.

    Fix: once the regular endpoint runs dry, if a history_url was given
    we transparently switch to it and keep paginating from exactly the
    same endTime cursor - the caller never needs to know the switch
    happened, they just get however much real history Bitget has.
    """
    bitget_granularity = granularity_map.get(granularity_key)
    if not bitget_granularity:
        return None

    if limit is None:
        limit = DEFAULT_CANDLE_COUNTS.get(granularity_key, 200)

    clean_symbol = "".join(c for c in symbol if c.isalnum()).upper()

    cache_key = (base_url, clean_symbol, granularity_key)
    marked_bad_at = _bad_symbol_cache.get(cache_key)
    if marked_bad_at is not None and (time.time() - marked_bad_at) < BAD_SYMBOL_CACHE_TTL_SECONDS:
        return None

    all_candles = []
    end_time = None
    using_history = False

    try:
        while len(all_candles) < limit:
            if using_history:
                url = history_url
                gran = (history_granularity_map or granularity_map).get(granularity_key, bitget_granularity)
                call_max = history_max_limit or BITGET_MAX_LIMIT_PER_CALL
            else:
                url = base_url
                gran = bitget_granularity
                call_max = BITGET_MAX_LIMIT_PER_CALL

            page_limit = min(call_max, limit - len(all_candles))

            params = {"symbol": clean_symbol, "granularity": gran, "limit": page_limit}
            if extra_params:
                params.update(extra_params)
            if end_time is not None:
                params["endTime"] = end_time
            elif using_history:
                # history-candles requires endTime - if we're switching to
                # it on the very first call (regular endpoint returned
                # nothing at all), anchor it to "now" so the call is valid.
                params["endTime"] = int(time.time() * 1000)

            resp = _http_session.get(url, params=params, timeout=10)
            if not resp.ok:
                # Log Bitget's actual error message (e.g. "Parameter productType
                # error") instead of just "400 Bad Request" - much easier to
                # diagnose real parameter issues from this.
                log.error(f"Bitget API error {resp.status_code} for {params}: {resp.text[:300]}")
                if resp.status_code == 400:
                    try:
                        if resp.json().get("code") == "48001":
                            _bad_symbol_cache[cache_key] = time.time()
                    except ValueError:
                        pass  # response body wasn't JSON - nothing to cache, just fall through to raise_for_status
                resp.raise_for_status()
            rows = resp.json().get("data", [])

            if not rows:
                if not using_history and history_url:
                    # Regular endpoint's window is exhausted - hand off to
                    # the history endpoint and retry from the same cursor.
                    using_history = True
                    continue
                break

            page = [
                {
                    "time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
                for row in rows
            ]
            all_candles.extend(page)
            oldest_time_this_page = min(int(row[0]) for row in rows)

            if len(rows) < page_limit:
                if not using_history and history_url:
                    # Partial page from the regular endpoint - it's about
                    # to run dry. Switch to the history endpoint for the
                    # next call instead of stopping here.
                    using_history = True
                    end_time = oldest_time_this_page - 1
                    continue
                break

            end_time = oldest_time_this_page - 1

        deduped = {c["time"]: c for c in all_candles}
        candles = sorted(deduped.values(), key=lambda c: c["time"])
        return candles[-limit:]
    except Exception as e:
        log.error(f"Bitget candle fetch failed for {symbol} @ {granularity_key} ({base_url}): {e}")
        return all_candles if all_candles else None


def fetch_bitget_spot_candles(symbol, granularity_key, limit=None):
    """Bitget SPOT public candles."""
    return _fetch_bitget_paginated(
        BITGET_SPOT_CANDLES_URL, symbol, granularity_key, limit, SUPPORTED_GRANULARITIES,
        history_url=BITGET_SPOT_HISTORY_CANDLES_URL,
        history_granularity_map=SUPPORTED_GRANULARITIES,
    )


def format_symbol_display(raw):
    """Turns a raw exchange symbol slug ("BTCUSDT") into "BTC/USDT" for display."""
    quote_assets = ["USDT", "USDC", "BTC", "ETH", "EUR", "USD"]
    for quote in quote_assets:
        if raw.endswith(quote) and len(raw) > len(quote):
            return raw[: -len(quote)] + "/" + quote
    return raw


def fetch_bitget_spot_tickers():
    """
    One call returns live price + 24h stats for EVERY Bitget spot pair -
    this is what powers the "+ Add Token" search list, so users can pick
    any spot pair directly from the dashboard instead of manually opening
    it on bitget.com first.
    """
    resp = _http_session.get("https://api.bitget.com/api/v2/spot/market/tickers", timeout=10)
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    return [
        {
            "symbol": format_symbol_display(r["symbol"]),
            "rawSymbol": r["symbol"],
            "lastPrice": float(r.get("lastPr") or 0),
            "change24h": _parse_pct_change(r.get("change24h")),
            "high24h": r.get("high24h"),
            "low24h": r.get("low24h"),
            "usdtVolume24h": float(r.get("usdtVolume") or 0),
        }
        for r in rows
    ]


def fetch_bitget_futures_tickers(product_type="usdt-futures"):
    """Same idea as fetch_bitget_spot_tickers(), for USDT-margined futures contracts."""
    resp = _http_session.get(
        "https://api.bitget.com/api/v2/mix/market/tickers",
        params={"productType": product_type},
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    return [
        {
            "symbol": format_symbol_display(r["symbol"]),
            "rawSymbol": r["symbol"],
            "lastPrice": float(r.get("lastPr") or 0),
            "change24h": _parse_pct_change(r.get("change24h")),
            "high24h": r.get("high24h"),
            "low24h": r.get("low24h"),
            "usdtVolume24h": float(r.get("usdtVolume") or 0),
        }
        for r in rows
    ]


# Caches the full token list per exchange for a few seconds - fetching
# hundreds of tickers on every dashboard poll would be wasteful. The
# search modal only needs "fresh enough" prices, not tick-by-tick.
#
# "last_good_ts" tracks the last time `data` was ACTUALLY refreshed
# successfully - separate from the retry-gate timestamp below - so the
# frontend can be told honestly how old the displayed prices really are
# (e.g. show a "data may be delayed" warning instead of silently
# presenting stale numbers as if they were live).
_token_list_cache = {
    "bitget-spot": {"data": [], "attempt_ts": 0, "last_good_ts": 0},
    "bitget-futures": {"data": [], "attempt_ts": 0, "last_good_ts": 0},
}
TOKEN_LIST_CACHE_TTL_SECONDS = 10


def get_token_list(exchange):
    """
    Returns {"tokens": [...], "cached_at": <unix ts of last successful
    Bitget fetch>, "stale": bool}. `stale` is True once the last
    successful fetch is more than 3x the normal cache TTL old - a single
    slightly-late refresh isn't worth alarming over, but a fetch that's
    been failing for 30+ seconds straight is worth surfacing honestly
    rather than quietly showing old numbers as if they were current.
    """
    cache = _token_list_cache.get(exchange)
    if cache is None:
        return {"tokens": [], "cached_at": 0, "stale": True}

    now = time.time()
    if now - cache["attempt_ts"] > TOKEN_LIST_CACHE_TTL_SECONDS:
        # IMPORTANT: attempt_ts is updated whether this succeeds or not.
        # It used to only update on success, which meant a single failed
        # fetch (Bitget hiccup, rate limit, timeout) made EVERY subsequent
        # request re-attempt immediately with zero backoff - across
        # several open tabs/pages all polling this endpoint, that retry
        # storm could itself keep tripping Bitget's rate limiting,
        # permanently freezing the cache on old data with no way to
        # recover and no visible error anywhere in the UI.
        cache["attempt_ts"] = now
        try:
            if exchange == "bitget-spot":
                cache["data"] = fetch_bitget_spot_tickers()
            elif exchange == "bitget-futures":
                cache["data"] = fetch_bitget_futures_tickers()
            cache["last_good_ts"] = now
        except Exception as e:
            log.error(f"Token list fetch failed for {exchange}: {e}")

    age = now - cache["last_good_ts"] if cache["last_good_ts"] else float("inf")
    return {
        "tokens": cache["data"],
        "cached_at": cache["last_good_ts"],
        "stale": age > (TOKEN_LIST_CACHE_TTL_SECONDS * 3),
    }


def fetch_bitget_futures_candles(symbol, granularity_key, limit=None, product_type="usdt-futures"):
    """
    Bitget FUTURES (mix) public candles - USDT-margined perpetuals by default.

    productType="usdt-futures" (lowercase) confirmed working - Bitget's
    own parameter description table said uppercase "USDT-FUTURES" but
    that was wrong/outdated; the granularity format mismatch was the
    real bug (see BITGET_FUTURES_GRANULARITIES above), confirmed
    directly from Bitget's own API validation error message.
    """
    return _fetch_bitget_paginated(
        BITGET_FUTURES_CANDLES_URL, symbol, granularity_key, limit, BITGET_FUTURES_GRANULARITIES,
        extra_params={"productType": product_type},
        history_url=BITGET_FUTURES_HISTORY_CANDLES_URL,
        # Bitget's doc page claimed futures' history-candles wants the
        # long-form (spot-style) granularity strings, but that's WRONG -
        # confirmed directly from Bitget's own live API validation error
        # ("k-line time range should be [1m,3m,5m,15m,30m,1H,4H,...]")
        # when we actually tried the long-form. It wants the SAME
        # short-form as the regular futures endpoint.
        history_granularity_map=BITGET_FUTURES_GRANULARITIES,
        history_max_limit=BITGET_FUTURES_HISTORY_MAX_LIMIT_PER_CALL,
    )