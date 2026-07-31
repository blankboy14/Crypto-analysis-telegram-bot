"""
jobs/volume_spike_watcher.py

Phase 2.1 - "24/7 Market Analyse". Scheduled by
bot/handlers/market_analyse.py as a per-chat repeating job
(context.job_queue.run_repeating(tick, interval=poll_interval_seconds,
chat_id=..., data={"market": ...})) - this module owns everything that
happens on each tick: fetching prices, detecting a sudden move, applying
BTC's own tighter threshold, and pushing the alert.

HOW THE SPIKE CHECK WORKS: a single ticker snapshot only tells you a
pair's price NOW, not whether it just moved - "suddenly spiked" needs a
comparison against a price from shortly before. So this module keeps a
small in-memory rolling price history per (exchange, raw_symbol),
shared across every chat's tick (the market itself is the same for
everyone watching it - there's no reason each chat should keep its own
copy). Each tick compares the current price against the sample closest
to `poll_interval_seconds` old and flags a move past threshold. Only
the ALERT COOLDOWN is per-chat (so a chat that just turned this mode on
still gets told about an active spike, while a chat already alerted
about it recently doesn't get spammed every tick while the price stays
elevated).

This in-memory state (history + cooldowns) resets on a bot restart -
that's fine here, a missed alert or two right after a restart is a
minor cost, and it's what bot/state_store.py's SQLite-backed mode
ON/OFF state is for (that part does survive a restart - see
bot/main.py's startup re-scheduling via
state_store.get_active_chats_for_mode()).
"""
import logging
import time
from datetime import datetime, timezone, timedelta

from engine.bitget_api import get_token_list, fetch_bitget_spot_candles, fetch_bitget_futures_candles
from engine.order_flow import get_order_flow
from engine.signal_scanner import MARKET_SCOPE_MAP
from bot import state_store
from bot.formatters import (
    format_volume_spike_alert, format_volume_burst_alert,
    format_absolute_volume_alert, format_daily_mover_alert, format_tier_move_alert,
    format_main_meme_move_alert, format_token_listing_alert,
)

log = logging.getLogger("crypto-telegram-bot")

MODE = "market_analyse"

# --- shared, module-level state (see docstring above for why) ---

# (exchange, raw_symbol) -> [(timestamp, price), ...], oldest first,
# trimmed to _HISTORY_MAX_AGE_SECONDS on every append.
_price_history: dict[tuple[str, str], list[tuple[float, float]]] = {}

# (chat_id, raw_symbol, direction) -> unix ts of the last alert sent
# for that exact pair+direction, for this chat's cooldown.
_last_alert: dict[tuple[int, str, str], float] = {}

# --- add-on #6: New/Delisted Token watch ---
# scope ("bitget-spot"/"bitget-futures") -> {rawSymbol: token_dict},
# the FULL token list as of the last tick that actually compared it -
# shared across every chat's tick, same reasoning as _price_history
# above. None until the first comparison tick has run.
_listing_snapshot: dict[str, dict] = {}

# A listing/delisting is inherently a one-time event (not a recurring
# threshold like a price move) - this is just a safety margin against
# a duplicate push if a cache boundary and a chat's tick land awkwardly
# close together, not a "notify again every N hours" cooldown.
_LISTING_ALERT_DEDUP_SECONDS = 21600  # 6 hours

# Keep enough history that a slightly-late tick can still find a
# reasonable baseline to compare against.
_HISTORY_MAX_AGE_SECONDS = 180

# --- volume burst tracking (separate from the price-spike state above) ---

# (exchange, raw_symbol) -> [(timestamp, usdtVolume24h), ...], same
# rolling-history idea as _price_history but for the ticker's rolling
# 24h volume figure. Consecutive samples' DIFFERENCE approximates how
# much actually traded in that interval (the 24h window itself slides
# forward continuously, so this is a proxy, not an exact figure - but
# it's the only volume signal a ticker snapshot gives us without
# pulling the full trade tape for every pair on every tick).
_volume_history: dict[tuple[str, str], list[tuple[float, float]]] = {}

# (exchange, raw_symbol) -> [interval_delta, ...], most recent last,
# capped at _VOLUME_DELTA_HISTORY_MAX - this is the rolling baseline a
# new interval's delta gets compared against.
_volume_deltas: dict[tuple[str, str], list[float]] = {}
_VOLUME_DELTA_HISTORY_MAX = 20

# --- absolute volume burst tracking (add-on #1 - fixed USDT bar, not relative) ---

# (exchange, raw_symbol) -> [(timestamp, usdtVolume24h), ...], kept much
# longer than _volume_history above since this needs a baseline up to
# window_seconds (e.g. 30 minutes) old, not just one poll_interval old.
_abs_volume_history: dict[tuple, list[tuple[float, float, float]]] = {}
_ABS_VOLUME_HISTORY_MAX_AGE_SECONDS = 3600  # 1 hour of headroom regardless of configured window_seconds

# --- daily reset big-mover tracking (add-on #2) ---

# (exchange, raw_symbol) -> (period_id, price_at_reset). period_id is
# whatever _daily_period_id() returns for the "trading day" a sample
# belongs to - shared across chats, since the reset baseline is the
# same for everyone watching the same market.
_daily_baseline_price: dict[tuple, tuple[str, float]] = {}

# (chat_id, exchange, raw_symbol) -> period_id already alerted for -
# per-CHAT (unlike the baseline above) so every chat still gets told
# once per trading day, same reasoning as _last_alert's cooldown split.
_daily_mover_alerted: dict[tuple, str] = {}

# --- volatility-tier tracking (add-on #3 - BTC/ETH/SOL etc, candle-confirmed) ---

# (exchange, raw_symbol, candle_interval) -> (fetched_at, [candle, ...]).
# Candle fetches are heavier than a ticker snapshot, so these are only
# refreshed every refresh_seconds regardless of how often tick() runs.
_tier_candle_cache: dict[tuple, tuple[float, list]] = {}


def _trim_volume_history(key: tuple, now: float) -> None:
    samples = _volume_history.get(key)
    if not samples:
        return
    cutoff = now - _HISTORY_MAX_AGE_SECONDS
    i = 0
    while i < len(samples) and samples[i][0] < cutoff:
        i += 1
    if i:
        del samples[:i]


def _find_volume_baseline(key: tuple, now: float, target_age: float):
    """Same "closest sample to target_age" logic as _find_baseline(), applied to the volume history instead of price."""
    samples = _volume_history.get(key)
    if not samples:
        return None
    candidates = [(abs((now - ts) - target_age), vol) for ts, vol in samples if now - ts >= target_age * 0.5]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _detect_volume_bursts(scopes: list, cfg: dict) -> list:
    """
    Flags pairs whose traded volume THIS interval is far above their
    own recent baseline - e.g. a pair that's been quiet suddenly sees a
    burst of real activity. Independent of the price-spike check above
    (a volume burst can happen with little price movement yet, which is
    exactly the "catch it before/as it moves" case being asked for).
    """
    now = time.time()
    poll_interval = cfg.get("poll_interval_seconds", 15)
    multiplier = cfg.get("volume_burst_multiplier", 5.0)
    min_samples = cfg.get("volume_burst_min_samples", 5)
    # Optional filter: only alert bursts on pairs whose baseline
    # interval volume was itself small (i.e. it really was "quiet"
    # before this) - 0 disables the filter and considers every pair.
    low_floor = cfg.get("volume_burst_low_floor_usdt", 0)

    events = []
    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Volume burst watch: token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            vol24h = token.get("usdtVolume24h")
            if not vol24h:
                continue

            key = (scope, raw_symbol)
            baseline_vol = _find_volume_baseline(key, now, poll_interval)

            history = _volume_history.setdefault(key, [])
            history.append((now, vol24h))
            _trim_volume_history(key, now)

            if baseline_vol is None:
                continue  # still warming up - nothing to compare against yet

            interval_delta = vol24h - baseline_vol
            deltas = _volume_deltas.setdefault(key, [])

            if interval_delta > 0 and len(deltas) >= min_samples:
                recent = deltas[-min_samples:]
                baseline_avg = sum(recent) / len(recent)
                if baseline_avg > 0 and interval_delta >= baseline_avg * multiplier:
                    if low_floor <= 0 or baseline_avg <= low_floor:
                        events.append({
                            "exchange": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                            "lastPrice": token.get("lastPrice"), "intervalVolume": interval_delta,
                            "baselineVolume": baseline_avg, "multiplier": interval_delta / baseline_avg,
                        })

            if interval_delta > 0:
                deltas.append(interval_delta)
                if len(deltas) > _VOLUME_DELTA_HISTORY_MAX:
                    del deltas[: len(deltas) - _VOLUME_DELTA_HISTORY_MAX]

    return events


def _trim_history(key: tuple, now: float) -> None:
    samples = _price_history.get(key)
    if not samples:
        return
    cutoff = now - _HISTORY_MAX_AGE_SECONDS
    # Samples are appended in time order, so the first ones still
    # within the cutoff mark where the stale prefix ends.
    i = 0
    while i < len(samples) and samples[i][0] < cutoff:
        i += 1
    if i:
        del samples[:i]


def _find_baseline(key: tuple, now: float, target_age: float):
    """
    Picks the sample whose age is closest to `target_age` (normally
    `poll_interval_seconds`) rather than strictly "oldest available" -
    so an irregular tick schedule (bot hiccup, a chat joining partway
    through another chat's cycle, etc.) still gets a sensible,
    consistent comparison point. Returns None if there's no sample old
    enough yet to compare against (this pair/scope is still "warming
    up" - not enough history yet).
    """
    samples = _price_history.get(key)
    if not samples:
        return None
    candidates = [(abs((now - ts) - target_age), price) for ts, price in samples if now - ts >= target_age * 0.5]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _detect_moves(scopes: list, cfg: dict) -> list:
    """
    Refreshes the shared price history for every pair in `scopes` and
    returns every pair currently past its spike threshold. Cheap to
    call from several chats' ticks in a row - get_token_list() already
    caches Bitget's response for a few seconds (engine/bitget_api.py),
    so this doesn't re-hit the exchange on every single chat's tick.
    """
    now = time.time()
    poll_interval = cfg.get("poll_interval_seconds", 15)
    normal_threshold = cfg.get("spike_pct_threshold", 20.0)
    btc_threshold = cfg.get("btc_spike_pct_threshold", 5.0)
    btc_symbol = cfg.get("btc_symbol", "BTCUSDT")

    events = []
    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Volume spike watch: token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            price = token.get("lastPrice")
            if not price:
                continue

            key = (scope, raw_symbol)
            baseline = _find_baseline(key, now, poll_interval)

            history = _price_history.setdefault(key, [])
            history.append((now, price))
            _trim_history(key, now)

            if baseline is None or baseline == 0:
                continue  # still warming up for this pair - nothing to compare against yet

            pct_change = (price - baseline) / baseline * 100
            threshold = btc_threshold if raw_symbol.upper() == btc_symbol.upper() else normal_threshold

            if abs(pct_change) >= threshold:
                events.append({
                    "exchange": scope,
                    "rawSymbol": raw_symbol,
                    "symbol": token["symbol"],
                    "lastPrice": price,
                    "pctChange": pct_change,
                    "direction": "up" if pct_change > 0 else "down",
                })

    return events


# --- add-on #6: New/Delisted Token watch ---
_listing_events_cache: dict = {}   # scope -> {"events": [...], "ts": float}


def _detect_token_listing_changes(scope: str, max_age_seconds: float) -> list:
    """
    Compares this scope's current token list against the snapshot from
    the last time this actually ran a comparison (_listing_snapshot),
    cached for max_age_seconds so several chats' ticks landing close
    together all see the same batch instead of only the first one
    detecting anything. The very FIRST call ever for a scope just
    seeds _listing_snapshot and returns no events - there's nothing to
    compare against yet, and treating "every pair currently listed" as
    "newly added" on a fresh startup would be exactly wrong.
    """
    cached = _listing_events_cache.get(scope)
    now = time.time()
    if cached and (now - cached["ts"]) < max_age_seconds:
        return cached["events"]

    try:
        tokens = get_token_list(scope)["tokens"]
    except Exception as exc:
        log.error(f"Token listing watch: token list fetch failed for {scope}: {exc}")
        return []

    current = {t["rawSymbol"]: t for t in tokens}
    previous = _listing_snapshot.get(scope)
    _listing_snapshot[scope] = current

    events = []
    if previous is not None:
        for raw_symbol, token in current.items():
            if raw_symbol not in previous:
                events.append({
                    "action": "added", "scope": scope, "rawSymbol": raw_symbol,
                    "symbol": token["symbol"], "details": token,
                })
        for raw_symbol, token in previous.items():
            if raw_symbol not in current:
                events.append({
                    "action": "removed", "scope": scope, "rawSymbol": raw_symbol,
                    "symbol": token["symbol"], "details": token,
                })

    _listing_events_cache[scope] = {"events": events, "ts": now}
    return events


def _detect_main_meme_coin_moves(scopes: list, main_cfg: dict, meme_cfg: dict) -> list:
    """
    24/7 Market Analyse add-on #4/#5. Unlike _detect_moves() above
    (which needs a rolling in-memory price history to compute "moved
    X% in the last poll window"), this reads the exchange's own
    ROLLING 24h% field straight off the ticker - no warm-up period,
    correct from the very first tick after a restart.

    main_cfg.symbols decides which pairs are "main coins" (tight,
    symmetric bar) - everything else is treated as a meme/alt coin
    (much wider, ASYMMETRIC bar: a meme coin dumping matters sooner
    than the same-size pump). Each function call checks BOTH tiers in
    one pass over the token list rather than two separate scans.
    """
    events = []
    if not main_cfg.get("enabled", True) and not meme_cfg.get("enabled", True):
        return events

    main_symbols = {s.upper() for s in main_cfg.get("symbols", [])}
    main_threshold = main_cfg.get("pct_threshold", 3.0)
    meme_up_threshold = meme_cfg.get("up_threshold_pct", 40.0)
    meme_down_threshold = meme_cfg.get("down_threshold_pct", 30.0)

    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Main/meme coin watch: token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            pct_change = token.get("change24h")
            if pct_change is None:
                continue

            is_main = raw_symbol.upper() in main_symbols
            if is_main:
                if not main_cfg.get("enabled", True) or abs(pct_change) < main_threshold:
                    continue
                tier = "main"
            else:
                if not meme_cfg.get("enabled", True):
                    continue
                threshold = meme_up_threshold if pct_change > 0 else meme_down_threshold
                if abs(pct_change) < threshold:
                    continue
                tier = "meme"

            events.append({
                "exchange": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                "lastPrice": token.get("lastPrice"), "pctChange": pct_change,
                "direction": "up" if pct_change > 0 else "down", "tier": tier,
            })

    return events


def _trim(samples: list, now: float, max_age: float) -> None:
    """Generic version of _trim_history/_trim_volume_history for any (timestamp, value) list."""
    if not samples:
        return
    cutoff = now - max_age
    i = 0
    while i < len(samples) and samples[i][0] < cutoff:
        i += 1
    if i:
        del samples[:i]


def _find_closest_with_ts(history: list, now: float, target_age: float):
    """
    Same "closest sample to target_age" idea as _find_baseline()/
    _find_volume_baseline(), but also returns the sample's OWN
    timestamp (not just its value) - callers here need the real
    elapsed time, since a tick isn't guaranteed to land exactly
    target_age seconds after the baseline.
    """
    if not history:
        return None
    candidates = [
        (abs((now - ts) - target_age), ts, val, price)
        for ts, val, price in history
        if now - ts >= target_age * 0.5
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, ts, val, price = candidates[0]
    return ts, val, price


def _detect_absolute_volume_bursts(scopes: list, cfg: dict, main_symbols: set) -> list:
    """
    24/7 Market Analyse add-on #1. Unlike _detect_volume_bursts() above
    (which flags a pair moving far past ITS OWN recent baseline), this
    flags a pair the moment its traded volume crosses a fixed absolute
    USDT amount within a rolling window - e.g. a pair that normally
    does ~100k/interval suddenly trading 100M+ within 30 minutes gets
    caught here even on its very first sample, with no "warm-up"
    baseline needed for the pair itself.

    `main_symbols` (from main_coin_watch.symbols in settings.yaml, the
    same shared list used by the main/meme 24h% watchers) decides
    which of cfg's two thresholds applies - see
    absolute_volume_watch's comment in settings.yaml for why a meme
    coin's bar is set HIGHER, not lower.

    Also tracks PRICE alongside volume in the same history now (not
    just volume) - added so the alert can say whether that huge amount
    of money pushed the price up or down over the window, not just
    "a lot of money moved".
    """
    if not cfg.get("enabled", True):
        return []

    now = time.time()
    window_seconds = cfg.get("window_seconds", 1800)
    main_threshold = cfg.get("main_coin_threshold_usdt", 100_000_000)
    meme_threshold = cfg.get("meme_coin_threshold_usdt", 200_000_000)

    events = []
    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Absolute volume watch: token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            vol24h = token.get("usdtVolume24h")
            price = token.get("lastPrice")
            if not vol24h or not price:
                continue
            key = (scope, raw_symbol)
            history = _abs_volume_history.setdefault(key, [])
            baseline = _find_closest_with_ts(history, now, window_seconds)
            history.append((now, vol24h, price))
            _trim(history, now, _ABS_VOLUME_HISTORY_MAX_AGE_SECONDS)

            if baseline is None:
                continue  # still warming up - not enough history for this pair yet

            baseline_ts, baseline_vol, baseline_price = baseline
            actual_window = now - baseline_ts
            if actual_window <= 0:
                continue

            interval_volume = vol24h - baseline_vol
            if interval_volume <= 0:
                continue

            is_main = raw_symbol.upper() in main_symbols
            abs_threshold = main_threshold if is_main else meme_threshold

            # Scale the threshold to the ACTUAL elapsed comparison
            # window rather than the configured one - this is what
            # keeps "X+ within Y minutes" correct even if a tick lands
            # late/irregular or window_seconds itself gets edited.
            scaled_threshold = abs_threshold * (actual_window / window_seconds)

            if interval_volume >= scaled_threshold:
                price_pct_change = ((price - baseline_price) / baseline_price * 100) if baseline_price else None
                events.append({
                    "exchange": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                    "lastPrice": price, "intervalVolume": interval_volume,
                    "windowSeconds": actual_window, "thresholdUsdt": scaled_threshold,
                    "priceWindowPctChange": price_pct_change,
                    "direction": "up" if (price_pct_change or 0) >= 0 else "down",
                    "isMain": is_main, "change24h": token.get("change24h"),
                })

    return events


def _fetch_multi_timeframe_trend(scope: str, raw_symbol: str) -> dict:
    """
    Only called once an absolute-volume alert is actually about to
    fire (NOT on every tick for every pair) - one open/close % move
    per timeframe, from that timeframe's own latest candle. Answers
    "which direction has this pair actually been trending on 30m/1h/
    4h/1D" alongside the raw volume number, so the alert says more
    than just "a lot of money moved" (see format_absolute_volume_alert).
    Any timeframe that fails to fetch is just omitted, not a bug -
    the alert still sends with whichever timeframes did come back.
    """
    trend = {}
    for label, granularity in (("30m", "30m"), ("1h", "1h"), ("4h", "4h"), ("1d", "1d")):
        try:
            if scope == "bitget-futures":
                candles = fetch_bitget_futures_candles(raw_symbol, granularity, limit=1)
            else:
                candles = fetch_bitget_spot_candles(raw_symbol, granularity, limit=1)
            if candles:
                c = candles[-1]
                if c.get("open") and c.get("close"):
                    trend[label] = (c["close"] - c["open"]) / c["open"] * 100
        except Exception as exc:
            log.error(f"Absolute volume watch: {label} trend fetch failed for {raw_symbol}: {exc}")
    return trend


def _daily_period_id(now_ts: float, reset_hour_utc: int) -> str:
    """
    Which "trading day" `now_ts` falls into, given a daily reset hour
    (e.g. reset_hour_utc=0 means the trading day rolls over at 00:00
    UTC = 06:00 in Bangladesh). Shifting the clock back by the reset
    hour before taking the date is what makes the boundary land at the
    right moment instead of always at UTC midnight.
    """
    shifted = datetime.fromtimestamp(now_ts, tz=timezone.utc) - timedelta(hours=reset_hour_utc)
    return shifted.date().isoformat()


def _detect_daily_movers(scopes: list, cfg: dict) -> list:
    """
    24/7 Market Analyse add-on #2. Once a day, at reset_hour_utc, each
    pair's price becomes that day's baseline; if a pair ever ends up
    up_threshold_pct+ above (or down_threshold_pct+ below) ITS OWN
    day-start price before the next reset, it's flagged - independent
    of the exchange's rolling 24h% field, which slides forward
    continuously and never lines up with any fixed clock time. Both
    directions are checked every tick - a pair only needs to clear its
    OWN direction's bar, not both.
    """
    if not cfg.get("enabled", True):
        return []

    now = time.time()
    reset_hour = cfg.get("reset_hour_utc", 0)
    up_threshold = cfg.get("up_threshold_pct", 60.0)
    down_threshold = cfg.get("down_threshold_pct", 40.0)
    period_id = _daily_period_id(now, reset_hour)

    events = []
    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Daily mover watch: token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            price = token.get("lastPrice")
            if not price:
                continue

            key = (scope, raw_symbol)
            stored = _daily_baseline_price.get(key)
            if stored is None or stored[0] != period_id:
                # First time seen, or a new trading day just started -
                # today's baseline is whatever price it is right now.
                _daily_baseline_price[key] = (period_id, price)
                continue

            _, baseline_price = stored
            if baseline_price <= 0:
                continue

            pct_change = (price - baseline_price) / baseline_price * 100
            threshold = up_threshold if pct_change > 0 else down_threshold
            if abs(pct_change) >= threshold:
                events.append({
                    "exchange": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                    "lastPrice": price, "pctChange": pct_change,
                    "direction": "up" if pct_change > 0 else "down", "periodId": period_id,
                })

    return events


def _get_tier_candles(scope: str, raw_symbol: str, candle_interval: str, refresh_seconds: float):
    """Cached last-2-candles fetch, refreshed at most every refresh_seconds per (scope, symbol, interval)."""
    now = time.time()
    key = (scope, raw_symbol, candle_interval)
    cached = _tier_candle_cache.get(key)
    if cached and now - cached[0] < refresh_seconds:
        return cached[1]

    try:
        if scope == "bitget-futures":
            candles = fetch_bitget_futures_candles(raw_symbol, candle_interval, limit=2)
        else:
            candles = fetch_bitget_spot_candles(raw_symbol, candle_interval, limit=2)
    except Exception as exc:
        log.error(f"Volatility tier watch: candle fetch failed for {raw_symbol} ({scope}): {exc}")
        candles = None

    _tier_candle_cache[key] = (now, candles)
    return candles


def _detect_tier_moves(scopes: list, cfg: dict) -> list:
    """
    24/7 Market Analyse add-on #3. Pairs like BTC/ETH/SOL rarely move
    more than a couple % a day, so the general spike_pct_threshold
    (20%+) would basically never fire for them - a much smaller move on
    its own candle timeframe already matters for these, PROVIDED it's
    backed by real candle volume (so a thin-liquidity wick doesn't
    trigger a false alarm).
    """
    if not cfg.get("enabled", True):
        return []

    refresh_seconds = cfg.get("refresh_seconds", 60)
    tiers = cfg.get("tiers", [])

    events = []
    for tier in tiers:
        symbols = [s.upper() for s in tier.get("symbols", [])]
        pct_threshold = tier.get("pct_threshold", 3.0)
        candle_interval = tier.get("candle_interval", "30m")
        min_volume_usdt = tier.get("min_candle_volume_usdt", 0)

        for scope in scopes:
            try:
                tokens = get_token_list(scope)["tokens"]
            except Exception as exc:
                log.error(f"Volatility tier watch: token list fetch failed for {scope}: {exc}")
                continue

            for token in tokens:
                raw_symbol = token["rawSymbol"]
                if raw_symbol.upper() not in symbols:
                    continue

                candles = _get_tier_candles(scope, raw_symbol, candle_interval, refresh_seconds)
                if not candles:
                    continue

                latest = candles[-1]
                open_price = latest.get("open")
                close_price = latest.get("close")
                candle_volume = latest.get("volume")
                if not open_price or not close_price or candle_volume is None:
                    continue

                pct_change = (close_price - open_price) / open_price * 100
                candle_volume_usdt = candle_volume * close_price

                if abs(pct_change) >= pct_threshold and candle_volume_usdt >= min_volume_usdt:
                    events.append({
                        "exchange": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                        "lastPrice": token.get("lastPrice"), "pctChange": pct_change,
                        "candleInterval": candle_interval, "candleVolumeUsdt": candle_volume_usdt,
                        "direction": "up" if pct_change > 0 else "down",
                    })

    return events


async def tick(context) -> None:
    """The job_queue.run_repeating callback - one poll for one chat."""
    job = context.job
    chat_id = job.chat_id
    market = (job.data or {}).get("market")

    # Self-heal: if this chat's mode got turned off but the job somehow
    # survived (shouldn't normally happen - handlers cancel it on OFF -
    # but a stale job outliving a state change is cheap to guard against).
    if not state_store.is_mode_on(chat_id, MODE):
        job.schedule_removal()
        return

    scopes = MARKET_SCOPE_MAP.get(market)
    if not scopes:
        log.error(f"Volume spike watch: unknown market {market!r} for chat {chat_id}, stopping job")
        job.schedule_removal()
        return

    settings = context.bot_data.get("settings", {})
    cfg = settings.get("volume_spike_watch", {})
    cooldown_seconds = cfg.get("cooldown_seconds", 900)

    try:
        events = _detect_moves(scopes, cfg)
    except Exception as exc:
        log.error(f"Volume spike watch: tick failed for chat {chat_id}: {exc}")
        return

    now = time.time()
    for event in events:
        cooldown_key = (chat_id, event["rawSymbol"], event["direction"])
        last = _last_alert.get(cooldown_key, 0)
        if now - last < cooldown_seconds:
            continue

        try:
            text = format_volume_spike_alert(
                pair=event["symbol"],
                last_price=event["lastPrice"],
                pct_change=event["pctChange"],
                direction=event["direction"],
                market=market,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_alert[cooldown_key] = now
            state_store.log_alert(
                chat_id, event["exchange"], event["rawSymbol"], event["symbol"],
                event["direction"], event["pctChange"], event["lastPrice"],
            )
        except Exception as exc:
            log.error(f"Volume spike watch: failed to send alert to chat {chat_id}: {exc}")

    # --- volume burst check (real traded volume, independent of price) ---
    # Off by default - see volume_burst_enabled comment in settings.yaml
    # for why (superseded by the absolute_volume_watch check further
    # below, which needs a real dollar amount rather than just a
    # multiplier over a possibly-tiny baseline).
    volume_events = []
    if cfg.get("volume_burst_enabled", False):
        try:
            volume_events = _detect_volume_bursts(scopes, cfg)
        except Exception as exc:
            log.error(f"Volume burst watch: tick failed for chat {chat_id}: {exc}")

    for event in volume_events:
        cooldown_key = (chat_id, event["rawSymbol"], "volume")
        last = _last_alert.get(cooldown_key, 0)
        if now - last < cooldown_seconds:
            continue

        buy_pct = None
        try:
            flow = get_order_flow(event["rawSymbol"], event["exchange"], bucket_seconds=60)
            if flow:
                live = flow.get("live") or {}
                recent = live.get("last60s") or live
                buy_pct = recent.get("buyPct")
        except Exception as exc:
            log.error(f"Volume burst watch: order flow fetch failed for {event['rawSymbol']}: {exc}")

        try:
            text = format_volume_burst_alert(
                pair=event["symbol"], last_price=event["lastPrice"],
                interval_volume=event["intervalVolume"], baseline_volume=event["baselineVolume"],
                multiplier=event["multiplier"], market=market, buy_pct=buy_pct,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_alert[cooldown_key] = now
            state_store.log_alert(
                chat_id, event["exchange"], event["rawSymbol"], event["symbol"],
                "volume", event["multiplier"], event["lastPrice"],
            )
        except Exception as exc:
            log.error(f"Volume burst watch: failed to send alert to chat {chat_id}: {exc}")

    # --- absolute volume burst check (add-on #1: fixed USDT bar) ---
    abs_cfg = settings.get("absolute_volume_watch", {})
    abs_cooldown = abs_cfg.get("cooldown_seconds", 1800)
    main_cfg = settings.get("main_coin_watch", {})
    main_symbols = {s.upper() for s in main_cfg.get("symbols", [])}
    try:
        # BUG FIX: this call was missing main_symbols entirely, which
        # made _detect_absolute_volume_bursts() raise a TypeError on
        # EVERY tick (silently caught below) - the absolute-volume
        # ("Massive Volume Surge") alert has never actually been able
        # to fire until this fix.
        abs_events = _detect_absolute_volume_bursts(scopes, abs_cfg, main_symbols)
    except Exception as exc:
        log.error(f"Absolute volume watch: tick failed for chat {chat_id}: {exc}")
        abs_events = []

    for event in abs_events:
        cooldown_key = (chat_id, event["rawSymbol"], "abs_volume")
        last = _last_alert.get(cooldown_key, 0)
        if now - last < abs_cooldown:
            continue

        trend = {}
        try:
            trend = _fetch_multi_timeframe_trend(event["exchange"], event["rawSymbol"])
        except Exception as exc:
            log.error(f"Absolute volume watch: trend fetch failed for {event['rawSymbol']}: {exc}")

        try:
            text = format_absolute_volume_alert(
                pair=event["symbol"], last_price=event["lastPrice"],
                interval_volume=event["intervalVolume"], window_seconds=event["windowSeconds"],
                threshold_usdt=event["thresholdUsdt"], market=market,
                direction=event["direction"], price_window_pct_change=event["priceWindowPctChange"],
                change_24h=event.get("change24h"), is_main=event["isMain"], trend=trend,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_alert[cooldown_key] = now
            state_store.log_alert(
                chat_id, event["exchange"], event["rawSymbol"], event["symbol"],
                "abs_volume", event["intervalVolume"], event["lastPrice"],
            )
        except Exception as exc:
            log.error(f"Absolute volume watch: failed to send alert to chat {chat_id}: {exc}")

    # --- main coin / meme coin 24h% watch (add-on #4/#5) ---
    # BUG FIX: _detect_main_meme_coin_moves() was fully written but
    # never actually called from tick() - main coins (BTC/ETH/SOL/BNB,
    # 3% bar) and meme/alt coins (40% up / 30% down bar) never got
    # this check at all, regardless of settings.yaml being enabled.
    meme_cfg = settings.get("meme_coin_watch", {})
    main_meme_cooldown_main = main_cfg.get("cooldown_seconds", 1800)
    main_meme_cooldown_meme = meme_cfg.get("cooldown_seconds", 1800)
    try:
        main_meme_events = _detect_main_meme_coin_moves(scopes, main_cfg, meme_cfg)
    except Exception as exc:
        log.error(f"Main/meme coin watch: tick failed for chat {chat_id}: {exc}")
        main_meme_events = []

    for event in main_meme_events:
        cooldown_key = (chat_id, event["rawSymbol"], f"mainmeme_{event['direction']}")
        cooldown = main_meme_cooldown_main if event["tier"] == "main" else main_meme_cooldown_meme
        last = _last_alert.get(cooldown_key, 0)
        if now - last < cooldown:
            continue

        try:
            text = format_main_meme_move_alert(
                pair=event["symbol"], last_price=event["lastPrice"], pct_change=event["pctChange"],
                direction=event["direction"], tier=event["tier"], market=market,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_alert[cooldown_key] = now
            state_store.log_alert(
                chat_id, event["exchange"], event["rawSymbol"], event["symbol"],
                event["direction"], event["pctChange"], event["lastPrice"],
            )
        except Exception as exc:
            log.error(f"Main/meme coin watch: failed to send alert to chat {chat_id}: {exc}")

    # --- new/delisted token watch (add-on #6) ---
    listing_cfg = settings.get("token_listing_watch", {})
    if listing_cfg.get("enabled", True):
        # BUG FIX: poll_interval was referenced here but never defined in
        # tick()'s scope (it only exists as a local inside
        # _detect_volume_bursts()/_detect_moves()) - this raised a
        # NameError on every tick once add-on #6 was enabled. Reuse the
        # same volume_spike_watch poll interval those functions use.
        poll_interval = cfg.get("poll_interval_seconds", 15)
        listing_events = []
        for scope in scopes:
            try:
                listing_events += _detect_token_listing_changes(scope, poll_interval)
            except Exception as exc:
                log.error(f"Token listing watch: tick failed for chat {chat_id} (scope={scope}): {exc}")

        for event in listing_events:
            cooldown_key = (chat_id, event["rawSymbol"], f"listing_{event['action']}")
            last = _last_alert.get(cooldown_key, 0)
            if now - last < _LISTING_ALERT_DEDUP_SECONDS:
                continue

            try:
                text = format_token_listing_alert(
                    action=event["action"], pair=event["symbol"], market=market, details=event["details"],
                )
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                _last_alert[cooldown_key] = now
                state_store.log_alert(
                    chat_id, event["scope"], event["rawSymbol"], event["symbol"],
                    event["action"], 0.0, event["details"].get("lastPrice"),
                )
            except Exception as exc:
                log.error(f"Token listing watch: failed to send alert to chat {chat_id}: {exc}")

    # --- daily reset big-mover check (add-on #2) ---
    daily_cfg = settings.get("daily_mover_watch", {})
    try:
        daily_events = _detect_daily_movers(scopes, daily_cfg)
    except Exception as exc:
        log.error(f"Daily mover watch: tick failed for chat {chat_id}: {exc}")
        daily_events = []

    for event in daily_events:
        dedupe_key = (chat_id, event["exchange"], event["rawSymbol"])
        if _daily_mover_alerted.get(dedupe_key) == event["periodId"]:
            continue  # already told this chat about this pair for today's trading day

        try:
            text = format_daily_mover_alert(
                pair=event["symbol"], last_price=event["lastPrice"],
                pct_change=event["pctChange"], market=market,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _daily_mover_alerted[dedupe_key] = event["periodId"]
            state_store.log_alert(
                chat_id, event["exchange"], event["rawSymbol"], event["symbol"],
                event["direction"], event["pctChange"], event["lastPrice"],
            )
        except Exception as exc:
            log.error(f"Daily mover watch: failed to send alert to chat {chat_id}: {exc}")

    # --- volatility-tier check (add-on #3: BTC/ETH/SOL etc, candle-confirmed) ---
    tier_cfg = settings.get("volatility_tier_watch", {})
    tier_cooldown = tier_cfg.get("cooldown_seconds", 1800)
    try:
        tier_events = _detect_tier_moves(scopes, tier_cfg)
    except Exception as exc:
        log.error(f"Volatility tier watch: tick failed for chat {chat_id}: {exc}")
        tier_events = []

    for event in tier_events:
        cooldown_key = (chat_id, event["rawSymbol"], f"tier_{event['direction']}")
        last = _last_alert.get(cooldown_key, 0)
        if now - last < tier_cooldown:
            continue

        try:
            text = format_tier_move_alert(
                pair=event["symbol"], last_price=event["lastPrice"], pct_change=event["pctChange"],
                candle_interval=event["candleInterval"], candle_volume_usdt=event["candleVolumeUsdt"],
                market=market,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_alert[cooldown_key] = now
            state_store.log_alert(
                chat_id, event["exchange"], event["rawSymbol"], event["symbol"],
                event["direction"], event["pctChange"], event["lastPrice"],
            )
        except Exception as exc:
            log.error(f"Volatility tier watch: failed to send alert to chat {chat_id}: {exc}")