# order_flow.py
# Powers the dashboard's "Order Flow" section. Fetches recent public
# trade prints (fills) from Bitget for a symbol and derives:
#
#   1. LIVE snapshot   - buy vs sell volume + net delta across the whole
#      fetched window, plus a tighter last-60-seconds reading so a
#      sudden shift in aggression shows up quickly.
#   2. HISTORY buckets - those same trades grouped into fixed time
#      windows (configurable - 1m/5m/15m/etc), oldest -> newest.
#   3. BIG TRADES       - the largest individual trades in the fetched
#      window by notional value (price x size), with price/time/side -
#      "where did a lot of money print at once", a proxy for where
#      real liquidity/interest showed up.
#   4. ORDER BOOK        - live bid/ask depth snapshot (top N levels).
#   5. RECENT TRADES tape - the raw trade list (most recent first), for
#      a Bitget-style "Market trades" table.
#
# DEPTH: Bitget's quick /fills endpoint only returns the most recent 500
# trades. For more (up to ~2000, configurable), we page backward through
# /fills-history using the `idLessThan` cursor - much simpler than
# time-window pagination since it just needs the smallest tradeId from
# the previous page, no clock/timezone math involved. Bitget's own docs
# confirm fills-history covers up to 90 days of public trade data.

import time

from .http_client import http_session as _http_session, log

BITGET_SPOT_FILLS_URL = "https://api.bitget.com/api/v2/spot/market/fills"
BITGET_FUTURES_FILLS_URL = "https://api.bitget.com/api/v2/mix/market/fills"
BITGET_SPOT_FILLS_HISTORY_URL = "https://api.bitget.com/api/v2/spot/market/fills-history"
BITGET_FUTURES_FILLS_HISTORY_URL = "https://api.bitget.com/api/v2/mix/market/fills-history"
BITGET_SPOT_ORDERBOOK_URL = "https://api.bitget.com/api/v2/spot/market/orderbook"
BITGET_FUTURES_ORDERBOOK_URL = "https://api.bitget.com/api/v2/mix/market/merge-depth"

FILLS_QUICK_LIMIT = 500       # regular /fills endpoint's max per call
FILLS_HISTORY_MAX_LIMIT = 1000  # /fills-history endpoint's max per call
DEFAULT_TRADE_DEPTH = 2000     # how many trades to accumulate by default (via pagination)


def fetch_bitget_trades(raw_symbol, exchange, product_type="USDT-FUTURES", depth=None):
    """
    Pulls recent public trade prints for one symbol, paginating via the
    fills-history endpoint's `idLessThan` cursor to go beyond the quick
    endpoint's 500-trade cap. Returns a list of {price, size, side, ts}
    sorted oldest -> newest, or None on total failure.
    """
    if depth is None:
        depth = DEFAULT_TRADE_DEPTH

    if exchange == "bitget-spot":
        quick_url, history_url = BITGET_SPOT_FILLS_URL, BITGET_SPOT_FILLS_HISTORY_URL
        extra = {}
    elif exchange == "bitget-futures":
        quick_url, history_url = BITGET_FUTURES_FILLS_URL, BITGET_FUTURES_FILLS_HISTORY_URL
        extra = {"productType": product_type}
    else:
        log.error(f"Order flow: unsupported exchange '{exchange}'")
        return None

    all_trades = []
    id_less_than = None

    try:
        while len(all_trades) < depth:
            if id_less_than is None:
                # First page: the quick endpoint gives the freshest
                # snapshot reliably.
                url = quick_url
                params = {"symbol": raw_symbol, "limit": FILLS_QUICK_LIMIT, **extra}
            else:
                # Subsequent pages: walk further back using the history
                # endpoint's tradeId cursor.
                url = history_url
                page_limit = min(FILLS_HISTORY_MAX_LIMIT, depth - len(all_trades))
                params = {"symbol": raw_symbol, "limit": page_limit, "idLessThan": id_less_than, **extra}

            resp = _http_session.get(url, params=params, timeout=10)
            if not resp.ok:
                log.error(f"Order flow fills error {resp.status_code} for {raw_symbol} ({exchange}): {resp.text[:300]}")
                if all_trades:
                    break  # keep whatever we already have rather than failing outright
                resp.raise_for_status()

            rows = resp.json().get("data", [])
            if not rows:
                break

            page = []
            for r in rows:
                try:
                    page.append({
                        "id": r.get("tradeId"),
                        "price": float(r.get("price") or 0),
                        "size": float(r.get("size") or 0),
                        "side": (r.get("side") or "").lower(),  # "buy" | "sell"
                        "ts": int(r.get("ts") or 0),  # ms epoch
                    })
                except (TypeError, ValueError):
                    continue

            all_trades.extend(page)

            if len(rows) < (FILLS_QUICK_LIMIT if id_less_than is None else params["limit"]):
                break  # exchange ran out of history to give us

            # Next page continues just before the smallest tradeId seen so far
            numeric_ids = [int(t["id"]) for t in page if t["id"] and t["id"].isdigit()]
            if not numeric_ids:
                break
            id_less_than = min(numeric_ids)

        # Dedupe by tradeId (page boundaries can overlap by one trade) and sort oldest->newest
        deduped = {t["id"]: t for t in all_trades if t["id"]}
        trades = sorted(deduped.values(), key=lambda t: t["ts"])
        return trades[-depth:]
    except Exception as e:
        log.error(f"Order flow trade fetch failed for {raw_symbol} ({exchange}): {e}")
        return all_trades if all_trades else None


def fetch_bitget_orderbook(raw_symbol, exchange, product_type="usdt-futures", limit=30):
    """
    Live bid/ask depth snapshot - top `limit` price levels each side.
    Returns {"bids": [[price, size], ...], "asks": [[price, size], ...]}
    (bids sorted highest-first, asks lowest-first, matching how an order
    book is normally displayed), or None on failure.
    """
    try:
        if exchange == "bitget-spot":
            resp = _http_session.get(
                BITGET_SPOT_ORDERBOOK_URL,
                params={"symbol": raw_symbol, "type": "step0", "limit": limit},
                timeout=10,
            )
        elif exchange == "bitget-futures":
            resp = _http_session.get(
                BITGET_FUTURES_ORDERBOOK_URL,
                params={"symbol": raw_symbol, "productType": product_type},
                timeout=10,
            )
        else:
            return None

        if not resp.ok:
            log.error(f"Order book error {resp.status_code} for {raw_symbol} ({exchange}): {resp.text[:300]}")
            resp.raise_for_status()

        data = resp.json().get("data", {})
        bids = [[float(p), float(s)] for p, s in data.get("bids", [])][:limit]
        asks = [[float(p), float(s)] for p, s in data.get("asks", [])][:limit]
        return {"bids": bids, "asks": asks}
    except Exception as e:
        log.error(f"Order book fetch failed for {raw_symbol} ({exchange}): {e}")
        return None


def _aggregate(trades):
    buy_vol = sum(t["size"] for t in trades if t["side"] == "buy")
    sell_vol = sum(t["size"] for t in trades if t["side"] == "sell")
    total = buy_vol + sell_vol
    buy_pct = (buy_vol / total * 100) if total > 0 else 50.0
    return {
        "buyVolume": round(buy_vol, 6),
        "sellVolume": round(sell_vol, 6),
        "delta": round(buy_vol - sell_vol, 6),
        "buyPct": round(buy_pct, 1),
    }


def compute_live_snapshot(trades):
    """
    "Overall" - aggregates buy vs sell volume across the WHOLE fetched
    window (however many trades were pulled - up to `depth`, e.g. 2000)
    into one combined reading, plus a narrower last-60-seconds reading
    for a fast-reacting signal.
    """
    if not trades:
        empty = _aggregate([])
        return {**empty, "tradeCount": 0, "windowSeconds": 0, "last60s": empty}

    now_ms = trades[-1]["ts"]
    full = _aggregate(trades)
    last_60s = _aggregate([t for t in trades if now_ms - t["ts"] <= 60_000])

    return {
        **full,
        "tradeCount": len(trades),
        "windowSeconds": round((trades[-1]["ts"] - trades[0]["ts"]) / 1000) if len(trades) > 1 else 0,
        "last60s": last_60s,
    }


def compute_history_buckets(trades, bucket_seconds=60, max_buckets=40):
    """
    Buckets the fetched trade tape into fixed-size time windows
    (configurable - 60s/300s/900s/etc, matching the dashboard's
    timeframe dropdown) and returns buy/sell volume + delta per bucket,
    oldest first.
    """
    if not trades:
        return []

    bucket_ms = bucket_seconds * 1000
    buckets = {}
    for t in trades:
        bucket_start = (t["ts"] // bucket_ms) * bucket_ms
        b = buckets.setdefault(bucket_start, {"buyVolume": 0.0, "sellVolume": 0.0})
        if t["side"] == "buy":
            b["buyVolume"] += t["size"]
        elif t["side"] == "sell":
            b["sellVolume"] += t["size"]

    ordered = sorted(buckets.items())[-max_buckets:]
    result = []
    for bucket_start, v in ordered:
        delta = v["buyVolume"] - v["sellVolume"]
        result.append({
            "time": bucket_start // 1000,  # seconds epoch
            "buyVolume": round(v["buyVolume"], 6),
            "sellVolume": round(v["sellVolume"], 6),
            "delta": round(delta, 6),
            "dominantSide": "buy" if delta > 0 else ("sell" if delta < 0 else "neutral"),
        })
    return result


def compute_big_trades(trades, top_n=10):
    """
    Finds the largest individual trades in the fetched window by
    NOTIONAL value (price x size, i.e. actual $ amount) rather than raw
    size - a 10-BTC trade and a 10-DOGE trade both have "size 10" but
    wildly different real money behind them, so notional is the
    meaningful ranking. Returns the top N (largest first), each with
    price/size/side/time/notional - these are the prints most likely to
    mark real liquidity/interest at that price level and moment.
    """
    if not trades:
        return []

    ranked = sorted(trades, key=lambda t: t["price"] * t["size"], reverse=True)
    return [
        {
            "price": t["price"],
            "size": t["size"],
            "side": t["side"],
            "ts": t["ts"],
            "notional": round(t["price"] * t["size"], 2),
        }
        for t in ranked[:top_n]
    ]


# Short-lived cache per (symbol, exchange, depth). The detail panel
# polls every few seconds while open; without this, every poll tick
# would re-run the full multi-page trade fetch even if nothing new has
# printed yet.
_trade_cache = {}
TRADE_CACHE_TTL_SECONDS = 3


def get_order_flow(raw_symbol, exchange, bucket_seconds=60, depth=None):
    """
    Returns {live, history, bigTrades, orderBook, recentTrades} for one
    symbol, or None if the trade fetch failed and there's nothing (even
    stale) to fall back to.
    """
    cache_key = (raw_symbol, exchange, depth or DEFAULT_TRADE_DEPTH)
    now = time.time()
    cached = _trade_cache.get(cache_key)

    if cached and now - cached["ts"] < TRADE_CACHE_TTL_SECONDS:
        trades = cached["trades"]
    else:
        trades = fetch_bitget_trades(raw_symbol, exchange, depth=depth)
        if trades is None:
            if cached:
                trades = cached["trades"]  # serve stale data rather than nothing
            else:
                return None
        else:
            _trade_cache[cache_key] = {"trades": trades, "ts": now}

    order_book = fetch_bitget_orderbook(raw_symbol, exchange)

    return {
        "live": compute_live_snapshot(trades),
        "history": compute_history_buckets(trades, bucket_seconds=bucket_seconds),
        "bigTrades": compute_big_trades(trades),
        "orderBook": order_book,
        "recentTrades": [
            {"price": t["price"], "size": t["size"], "side": t["side"], "ts": t["ts"]}
            for t in reversed(trades[-60:])  # most recent first, for the trade tape table
        ],
    }