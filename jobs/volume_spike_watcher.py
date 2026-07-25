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
_abs_volume_history: dict[tuple, list[tuple[float, float]]] = {}
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
    candidates = [(abs((now - ts) - target_age), ts, val) for ts, val in history if now - ts >= target_age * 0.5]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, ts, val = candidates[0]
    return ts, val


def _detect_absolute_volume_bursts(scopes: list, cfg: dict) -> list:
    """
    24/7 Market Analyse add-on #1. Unlike _detect_volume_bursts() above
    (which flags a pair moving far past ITS OWN recent baseline), this
    flags a pair the moment its traded volume crosses a fixed absolute
    USDT amount within a rolling window - e.g. a pair that normally
    does ~100k/interval suddenly trading 60M+ within 30 minutes gets
    caught here even on its very first sample, with no "warm-up"
    baseline needed for the pair itself.
    """
    if not cfg.get("enabled", True):
        return []

    now = time.time()
    window_seconds = cfg.get("window_seconds", 1800)
    abs_threshold = cfg.get("absolute_threshold_usdt", 60_000_000)

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
            if not vol24h:
                continue

            key = (scope, raw_symbol)
            history = _abs_volume_history.setdefault(key, [])
            baseline = _find_closest_with_ts(history, now, window_seconds)
            history.append((now, vol24h))
            _trim(history, now, _ABS_VOLUME_HISTORY_MAX_AGE_SECONDS)

            if baseline is None:
                continue  # still warming up - not enough history for this pair yet

            baseline_ts, baseline_vol = baseline
            actual_window = now - baseline_ts
            if actual_window <= 0:
                continue

            interval_volume = vol24h - baseline_vol
            if interval_volume <= 0:
                continue

            # Scale the threshold to the ACTUAL elapsed comparison
            # window rather than the configured one - this is what
            # keeps "X+ within Y minutes" correct even if a tick lands
            # late/irregular or window_seconds itself gets edited.
            scaled_threshold = abs_threshold * (actual_window / window_seconds)

            if interval_volume >= scaled_threshold:
                events.append({
                    "exchange": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                    "lastPrice": token.get("lastPrice"), "intervalVolume": interval_volume,
                    "windowSeconds": actual_window, "thresholdUsdt": scaled_threshold,
                })

    return events


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
    try:
        abs_events = _detect_absolute_volume_bursts(scopes, abs_cfg)
    except Exception as exc:
        log.error(f"Absolute volume watch: tick failed for chat {chat_id}: {exc}")
        abs_events = []

    for event in abs_events:
        cooldown_key = (chat_id, event["rawSymbol"], "abs_volume")
        last = _last_alert.get(cooldown_key, 0)
        if now - last < abs_cooldown:
            continue

        try:
            text = format_absolute_volume_alert(
                pair=event["symbol"], last_price=event["lastPrice"],
                interval_volume=event["intervalVolume"], window_seconds=event["windowSeconds"],
                threshold_usdt=event["thresholdUsdt"], market=market,
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_alert[cooldown_key] = now
            state_store.log_alert(
                chat_id, event["exchange"], event["rawSymbol"], event["symbol"],
                "abs_volume", event["intervalVolume"], event["lastPrice"],
            )
        except Exception as exc:
            log.error(f"Absolute volume watch: failed to send alert to chat {chat_id}: {exc}")

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