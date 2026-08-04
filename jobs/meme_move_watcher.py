"""
jobs/meme_move_watcher.py

"Meme/Alt Coin Move" add-on to "Find 24/7 Strong Signal" - rides the
SAME ON/OFF toggle as jobs/strong_signal_watcher.py (scheduled
alongside it in bot/handlers/strong_signal.py's start_watching(), no
separate button). Four alert types, all driven off the same trailing
cumulative-%-move idea already used by the pump/reversal add-on
(bot/state_store.py's pump_price_history/get_peak_cumulative_pct), plus one
independent 4H spike check:

1. UP checkpoints  - first alert once cumulative move >= up_first_pct
   (60% default), then again every further +up_step_pct (80/100/120/...).
2. DOWN checkpoints - mirror of the above, first at -down_first_pct
   (-40% default), then every further -down_step_pct (-50/-60/-70/...).
3. Pump-then-drop reversal - a tracked UP move that pulls back
   reversal_pullback_pct (20% default) off its peak since being
   flagged.
4. Dump-then-bounce reversal - a tracked DOWN move that bounces
   reversal_pullback_pct off its trough since being flagged.
5. (small separate add-on) 4H window check - fires independently of
   all of the above, in TWO ways, over a trailing
   four_h_window_hours (4h default) window:
     a. Volume: that window's traded USDT volume passes
        four_h_volume_threshold_usdt (200M default) - tagged "up" or
        "down" by whether price rose or fell over the same window.
     b. Price move: that window's price move itself passes
        four_h_price_move_pct (65% default), either direction.
   Uses the SAME cheap trick as jobs/volume_spike_watcher.py's
   Absolute Volume Watch: track each tick's (24h ticker volume,
   price) and diff against the closest sample ~4h old, instead of
   fetching actual 4h candles per pair per tick - the latter would
   mean hundreds of extra Bitget candle calls every tick across the
   whole market, exactly the kind of load that trips Bitget's rate
   limit (see engine/bitget_api.py's fix for that). This does mean
   the "volume" here is a rolling-window delta of 24h cumulative
   volume, not one single candle's own volume bucket - functionally
   the same number, just computed without an extra API call.

State for #1-#4 lives in state_store's meme_move_state table (module
docstring there has the full field-by-field rationale) - shared across
every chat watching a given scope, same as pump_price_history/
overextended_pairs, since the market itself is the same for everyone.
State for #5 is process-local/in-memory only (module-level dict below)
- losing it on a restart just means the rolling window rebuilds over
the next ~4h, an acceptable cost for a "heads up" alert.

Per-chat push cooldowns (so a chat already watching doesn't get the
same pair+event spammed every tick while conditions keep holding) are
also in-memory, same pattern as strong_signal_watcher.py's _last_push/
_last_pump_push.
"""
import asyncio
import logging
import time

from engine.bitget_api import get_token_list
from engine.signal_scanner import MARKET_SCOPE_MAP
from bot import state_store
from bot.formatters import (
    format_meme_move_checkpoint_alert,
    format_meme_move_reversal_alert,
    format_meme_move_4h_volume_alert,
    format_meme_move_4h_price_alert,
)
from bot.scan_executor import SCAN_EXECUTOR

log = logging.getLogger("crypto-telegram-bot")

MODE = "strong_signal"  # rides the same toggle - see module docstring

# --- shared, market-level check cache (same one-check-per-market
# pattern as strong_signal_watcher.py's _pump_check_cache) ---
_check_cache: dict = {}
_check_locks: dict = {}

# --- per-chat push cooldowns (in-memory, resets on restart - see
# module docstring) ---
_last_checkpoint_push: dict = {}   # (chat_id, rawSymbol, checkpoint_pct) -> ts
_last_reversal_push: dict = {}     # (chat_id, rawSymbol, reversal_type) -> ts
_last_4h_volume_push: dict = {}    # (chat_id, rawSymbol) -> ts
_last_4h_price_push: dict = {}     # (chat_id, rawSymbol) -> ts

# --- 4H rolling (ticker 24h volume, price) history, in-memory only
# (see #5 above) - same shape as volume_spike_watcher.py's
# _abs_volume_history, just tracked separately since this module
# needs its own window length. ---
_window_history: dict = {}         # (scope, rawSymbol) -> [(ts, vol24h, price), ...]


def _get_check_lock(market: str) -> asyncio.Lock:
    lock = _check_locks.get(market)
    if lock is None:
        lock = asyncio.Lock()
        _check_locks[market] = lock
    return lock


def _trim_window_history(history: list, now: float, max_age: float) -> None:
    cutoff = now - max_age
    i = 0
    while i < len(history) and history[i][0] < cutoff:
        i += 1
    if i:
        del history[:i]


def _find_closest_with_ts(history: list, now: float, target_age: float):
    """Same 'closest sample to target_age' idea as volume_spike_watcher.py's Absolute Volume Watch - a tick isn't guaranteed to land exactly target_age seconds after any given sample. Returns (ts, vol24h, price) of the closest match, or None if history isn't warmed up yet."""
    candidates = [
        (abs((now - ts) - target_age), ts, vol, price)
        for ts, vol, price in history
        if now - ts >= target_age * 0.5
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, ts, vol, price = candidates[0]
    return ts, vol, price


def _run_checkpoint_and_reversal_check(scopes: list, cfg: dict) -> dict:
    """
    For every pair in `scopes`: records today's price, computes the
    trailing cumulative % move, then updates/creates its
    meme_move_state row and decides whether a checkpoint and/or
    reversal event fires this pass. Returns
    {"checkpoints": [...], "reversals": [...]} - each entry only
    returned once (state is updated immediately, same
    resolve-on-return idea as pump reversal).
    """
    window_days = cfg.get("window_days", 5)
    up_first = cfg.get("up_first_pct", 60.0)
    up_step = cfg.get("up_step_pct", 20.0)
    down_first = cfg.get("down_first_pct", 40.0)
    down_step = cfg.get("down_step_pct", 10.0)
    reversal_pct = cfg.get("reversal_pullback_pct", 20.0)

    checkpoints, reversals = [], []

    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Meme move watch: token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            price = token.get("lastPrice")
            if not price:
                continue
            symbol = token["symbol"]

            state_store.record_daily_price(scope, raw_symbol, price)
            # Peak-aware, not plain get_cumulative_pct - see
            # state_store.get_peak_cumulative_pct's docstring (same fix
            # as jobs/strong_signal_watcher.py's overextended-flagging
            # check: a same-day spike-and-partial-revert would otherwise
            # silently never cross either checkpoint threshold at all).
            peak_result = state_store.get_peak_cumulative_pct(scope, raw_symbol, window_days)
            if peak_result is None:
                continue
            cumulative_pct, extreme_price_reached = peak_result

            existing = state_store.get_meme_move_state(scope, raw_symbol)

            if cumulative_pct >= up_first:
                # target_checkpoint = the highest up_first + N*up_step
                # step at/below the current move - jumping several
                # steps at once (e.g. a fresh listing that opens
                # straight at +150%) only announces the one it's
                # actually AT right now, not every skipped step.
                steps_past = int((cumulative_pct - up_first) / up_step) if up_step > 0 else 0
                target_checkpoint = up_first + steps_past * up_step

                fresh_direction = existing is None or existing["direction"] != "up"
                # Seed from the TRUE peak reached (extreme_price_reached),
                # not just the live current price - the peak may already
                # be in the past by the time this tick runs.
                peak_price = extreme_price_reached if fresh_direction else max(existing["extremePrice"], extreme_price_reached)
                new_peak = fresh_direction or peak_price > existing["extremePrice"]
                last_announced = None if fresh_direction else existing["lastAnnouncedPct"]
                reversal_announced = False if new_peak else existing["reversalAnnounced"]

                if last_announced is None or target_checkpoint > last_announced:
                    checkpoints.append({
                        "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol,
                        "direction": "up", "checkpointPct": target_checkpoint,
                        "cumulativePct": cumulative_pct, "price": price,
                    })
                    last_announced = target_checkpoint

                pullback_pct = (peak_price - price) / peak_price * 100 if peak_price > 0 else 0
                if pullback_pct >= reversal_pct and not reversal_announced:
                    reversals.append({
                        "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol,
                        "reversalType": "pump_then_drop", "cumulativePct": cumulative_pct,
                        "extremePrice": peak_price, "currentPrice": price, "movePct": pullback_pct,
                    })
                    reversal_announced = True

                state_store.upsert_meme_move_state(
                    scope, raw_symbol, symbol, "up", last_announced, peak_price, reversal_announced,
                )

            elif cumulative_pct <= -down_first:
                steps_past = int((-cumulative_pct - down_first) / down_step) if down_step > 0 else 0
                target_checkpoint = -(down_first + steps_past * down_step)

                fresh_direction = existing is None or existing["direction"] != "down"
                trough_price = extreme_price_reached if fresh_direction else min(existing["extremePrice"], extreme_price_reached)
                new_trough = fresh_direction or trough_price < existing["extremePrice"]
                last_announced = None if fresh_direction else existing["lastAnnouncedPct"]
                reversal_announced = False if new_trough else existing["reversalAnnounced"]

                if last_announced is None or target_checkpoint < last_announced:
                    checkpoints.append({
                        "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol,
                        "direction": "down", "checkpointPct": target_checkpoint,
                        "cumulativePct": cumulative_pct, "price": price,
                    })
                    last_announced = target_checkpoint

                bounce_pct = (price - trough_price) / trough_price * 100 if trough_price > 0 else 0
                if bounce_pct >= reversal_pct and not reversal_announced:
                    reversals.append({
                        "scope": scope, "rawSymbol": raw_symbol, "symbol": symbol,
                        "reversalType": "dump_then_bounce", "cumulativePct": cumulative_pct,
                        "extremePrice": trough_price, "currentPrice": price, "movePct": bounce_pct,
                    })
                    reversal_announced = True

                state_store.upsert_meme_move_state(
                    scope, raw_symbol, symbol, "down", last_announced, trough_price, reversal_announced,
                )

            elif existing is not None:
                # Back inside the neutral zone (between -down_first and
                # up_first) - forget this pair so its NEXT pump or dump
                # starts fresh from the first checkpoint again.
                state_store.clear_meme_move_state(scope, raw_symbol)

    return {"checkpoints": checkpoints, "reversals": reversals}


def _run_4h_window_check(scopes: list, cfg: dict) -> dict:
    """
    Independent of the checkpoint/reversal function above - samples
    each pair's current (24h ticker volume, price) into the rolling
    history, diffs against the closest sample ~four_h_window_hours old,
    and returns two kinds of events from that same window:
      "volumeEvents" - that window's traded USDT volume >=
        four_h_volume_threshold_usdt (200M default), tagged up/down by
        whether price rose or fell over the window.
      "priceEvents"  - that window's price move itself >=
        four_h_price_move_pct (65% default), either direction.
    No extra Bitget candle calls - see module docstring.
    """
    window_hours = cfg.get("four_h_window_hours", 4.0)
    window_seconds = window_hours * 3600
    volume_threshold = cfg.get("four_h_volume_threshold_usdt", 200_000_000)
    price_move_threshold = cfg.get("four_h_price_move_pct", 65.0)
    now = time.time()

    volume_events, price_events = [], []

    for scope in scopes:
        try:
            tokens = get_token_list(scope)["tokens"]
        except Exception as exc:
            log.error(f"Meme move watch (4h window): token list fetch failed for {scope}: {exc}")
            continue

        for token in tokens:
            raw_symbol = token["rawSymbol"]
            vol24h = token.get("usdtVolume24h")
            price = token.get("lastPrice")
            if not vol24h or not price:
                continue

            key = (scope, raw_symbol)
            history = _window_history.setdefault(key, [])
            baseline = _find_closest_with_ts(history, now, window_seconds)
            history.append((now, vol24h, price))
            _trim_window_history(history, now, window_seconds * 1.2)

            if baseline is None:
                continue  # still warming up - not enough history for this pair yet

            baseline_ts, baseline_vol, baseline_price = baseline
            actual_window = now - baseline_ts
            if actual_window <= 0 or baseline_price <= 0:
                continue

            interval_volume = vol24h - baseline_vol
            price_pct_change = (price - baseline_price) / baseline_price * 100
            direction = "up" if price_pct_change >= 0 else "down"

            if interval_volume > 0:
                # Scale to the ACTUAL elapsed window, same reasoning as
                # volume_spike_watcher.py's Absolute Volume Watch - keeps
                # "X+ within 4h" correct even if a tick lands late.
                scaled_threshold = volume_threshold * (actual_window / window_seconds)
                if interval_volume >= scaled_threshold:
                    volume_events.append({
                        "scope": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                        "intervalVolume": interval_volume, "direction": direction,
                        "priceBefore": baseline_price, "priceNow": price,
                        "windowHours": actual_window / 3600,
                    })

            if abs(price_pct_change) >= price_move_threshold:
                price_events.append({
                    "scope": scope, "rawSymbol": raw_symbol, "symbol": token["symbol"],
                    "movePct": price_pct_change, "priceBefore": baseline_price, "priceNow": price,
                    "windowHours": actual_window / 3600,
                })

    return {"volumeEvents": volume_events, "priceEvents": price_events}


async def _get_or_run_check(market: str, scopes: list, cfg: dict) -> dict:
    """Same shared-cache-per-market pattern as strong_signal_watcher.py's pump check."""
    max_age = cfg.get("check_interval_seconds", 60)
    cached = _check_cache.get(market)
    now = time.time()
    if cached and (now - cached["ts"]) < max_age:
        return cached["result"]

    lock = _get_check_lock(market)
    async with lock:
        cached = _check_cache.get(market)
        now = time.time()
        if cached and (now - cached["ts"]) < max_age:
            return cached["result"]

        loop = asyncio.get_running_loop()
        checkpoint_result = await loop.run_in_executor(
            SCAN_EXECUTOR, _run_checkpoint_and_reversal_check, scopes, cfg,
        )
        window_result = await loop.run_in_executor(SCAN_EXECUTOR, _run_4h_window_check, scopes, cfg)
        result = {**checkpoint_result, **window_result}
        _check_cache[market] = {"result": result, "ts": time.time()}
        return result


async def tick(context) -> None:
    """The job_queue.run_repeating callback - one push-check for one chat."""
    job = context.job
    chat_id = job.chat_id
    market = (job.data or {}).get("market")

    if not state_store.is_mode_on(chat_id, MODE):
        job.schedule_removal()
        return

    if market not in MARKET_SCOPE_MAP:
        log.error(f"Meme move watch: unknown market {market!r} for chat {chat_id}, stopping job")
        job.schedule_removal()
        return

    settings = context.bot_data.get("settings", {})
    cfg = settings.get("meme_move_watch", {})
    cooldown_seconds = cfg.get("cooldown_seconds", 3600)
    four_h_volume_cooldown = cfg.get("four_h_volume_cooldown_seconds", 3600)
    four_h_price_cooldown = cfg.get("four_h_price_cooldown_seconds", 3600)
    scopes = MARKET_SCOPE_MAP[market]

    try:
        result = await _get_or_run_check(market, scopes, cfg)
    except Exception as exc:
        log.error(f"Meme move watch: check failed for chat {chat_id} (market={market}): {exc}")
        return

    now = time.time()

    for event in result.get("checkpoints", []):
        cooldown_key = (chat_id, event["rawSymbol"], event["checkpointPct"])
        if now - _last_checkpoint_push.get(cooldown_key, 0) < cooldown_seconds:
            continue
        try:
            text = format_meme_move_checkpoint_alert(
                pair=event["symbol"], market=market, direction=event["direction"],
                checkpoint_pct=event["checkpointPct"], cumulative_pct=event["cumulativePct"],
                price=event["price"], window_days=cfg.get("window_days", 5),
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_checkpoint_push[cooldown_key] = now
            state_store.log_signal(
                chat_id, "watcher", event["scope"], event["symbol"],
                "UP" if event["direction"] == "up" else "DOWN", event["cumulativePct"], message_text=text,
            )
        except Exception as exc:
            log.error(f"Meme move watch: failed to send checkpoint push to chat {chat_id}: {exc}")

    for event in result.get("reversals", []):
        cooldown_key = (chat_id, event["rawSymbol"], event["reversalType"])
        if now - _last_reversal_push.get(cooldown_key, 0) < cooldown_seconds:
            continue
        try:
            text = format_meme_move_reversal_alert(
                pair=event["symbol"], market=market, reversal_type=event["reversalType"],
                cumulative_pct=event["cumulativePct"], extreme_price=event["extremePrice"],
                current_price=event["currentPrice"], move_pct=event["movePct"],
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_reversal_push[cooldown_key] = now
            state_store.log_signal(
                chat_id, "watcher", event["scope"], event["symbol"], event["reversalType"],
                event["movePct"], message_text=text,
            )
        except Exception as exc:
            log.error(f"Meme move watch: failed to send reversal push to chat {chat_id}: {exc}")

    for event in result.get("volumeEvents", []):
        cooldown_key = (chat_id, event["rawSymbol"])
        if now - _last_4h_volume_push.get(cooldown_key, 0) < four_h_volume_cooldown:
            continue
        try:
            text = format_meme_move_4h_volume_alert(
                pair=event["symbol"], market=market, direction=event["direction"],
                interval_volume=event["intervalVolume"], price_before=event["priceBefore"],
                price_now=event["priceNow"], hours=event["windowHours"],
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_4h_volume_push[cooldown_key] = now
            state_store.log_signal(
                chat_id, "watcher", event["scope"], event["symbol"], "4H_VOLUME",
                event["intervalVolume"], message_text=text,
            )
        except Exception as exc:
            log.error(f"Meme move watch: failed to send 4h volume push to chat {chat_id}: {exc}")

    for event in result.get("priceEvents", []):
        cooldown_key = (chat_id, event["rawSymbol"])
        if now - _last_4h_price_push.get(cooldown_key, 0) < four_h_price_cooldown:
            continue
        try:
            text = format_meme_move_4h_price_alert(
                pair=event["symbol"], market=market, move_pct=event["movePct"],
                price_before=event["priceBefore"], price_now=event["priceNow"], hours=event["windowHours"],
            )
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_4h_price_push[cooldown_key] = now
            state_store.log_signal(
                chat_id, "watcher", event["scope"], event["symbol"], "4H_MOVE",
                event["movePct"], message_text=text,
            )
        except Exception as exc:
            log.error(f"Meme move watch: failed to send 4h price push to chat {chat_id}: {exc}")
        except Exception as exc:
            log.error(f"Meme move watch: failed to send 4h spike push to chat {chat_id}: {exc}")