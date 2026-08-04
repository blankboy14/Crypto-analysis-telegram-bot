"""
jobs/high_alert_watcher.py

"High Alert Pair Analyse" add-on to "Find 24/7 Strong Signal" - rides
the SAME ON/OFF toggle, scheduled alongside strong_signal_watcher.py
and meme_move_watcher.py (see bot/handlers/strong_signal.py's
start_watching()). No separate button.

WHY THIS IS DIFFERENT FROM THE EXISTING PUMP-REVERSAL ADD-ON:
strong_signal_watcher.py's _run_pump_reversal_check only looks at
PRICE (peak vs current) plus live order-flow buy/sell split - cheap
enough to re-check the whole market every ~60s, but it never actually
asks the real indicator/concept engine (the same one every other
signal this bot sends is built on) what it thinks.

This add-on makes the opposite trade-off: instead of running that full
multi-timeframe pipeline across the WHOLE market (which is what makes
a normal full scan take minutes - see engine/signal_scanner.py), it
runs that exact same pipeline (analyze_one_pair) but ONLY against the
small pool of pairs state_store already has flagged as overextended
(state_store.get_overextended() - pump_threshold_pct%+ cumulative
pump, populated by the pump-reversal check above, which already runs
on the same toggle) - so it's affordable to run fairly often, while
still producing a real indicator-confirmed SELL call with a full
Entry/SL/TP trade plan, not just a price/order-flow heuristic.

Rationale (from direct product feedback, worth keeping on record
here): pairs that have pumped 80-100%+ tend to see buyers exhaust
around a certain point, and once that happens a real SELL setup can
form - because the move is already extreme, a confirmed SELL entry
caught early here has room to capture 30-50%+ downside fast. This is
meant to be the single highest-conviction SELL alert type the bot
sends - hence running the FULL engine on it rather than a shortcut.
"""
import asyncio
import logging
import time

from engine.signal_scanner import MARKET_SCOPE_MAP, analyze_one_pair
from engine.news_service import get_news_feed
from engine.bitget_api import get_token_list
from bot import state_store
from bot.formatters import format_strong_signal
from bot.scan_executor import SCAN_EXECUTOR

log = logging.getLogger("crypto-telegram-bot")

MODE = "strong_signal"  # rides the same toggle - see module docstring

_check_cache: dict = {}
_check_locks: dict = {}
_last_push: dict = {}   # (chat_id, rawSymbol) -> unix ts, per-chat cooldown


def _get_check_lock(market: str) -> asyncio.Lock:
    lock = _check_locks.get(market)
    if lock is None:
        lock = asyncio.Lock()
        _check_locks[market] = lock
    return lock


def _run_high_alert_check(scopes: list, cfg: dict, enabled_indicators) -> list:
    """
    For every currently-overextended pair (state_store.get_overextended)
    in `scopes`: runs the full multi-timeframe engine and keeps it only
    if that comes back tradeable, verdict SELL, and at/above
    min_confidence_to_push. Returns the qualifying result dicts
    (same shape format_strong_signal already expects).
    """
    min_confidence = cfg.get("min_confidence_to_push", 65.0)
    results = []

    for scope in scopes:
        # --- Build this scope's scan pool from BOTH sources, each
        # candidate tagged with the verdict it's actually being
        # screened for (a pump/RSI-overbought pair is only interesting
        # if the full engine agrees SELL; an RSI-oversold pair only if
        # it agrees BUY) - see module docstring. Keyed by rawSymbol so
        # a pair flagged by both sources at once is only analyzed once
        # (pump/RSI-high both want SELL anyway, so there's nothing to
        # merge there; the pathological case of pump-overextended AND
        # RSI-oversold at the same time is treated as SELL, since the
        # pump-overextended flag is the stronger/older signal of the two).
        candidates = {}
        for ov in state_store.get_overextended(scope):
            candidates[ov["rawSymbol"]] = {
                "symbol": ov["symbol"], "expectedVerdict": "SELL",
                "source": "pump", "cumulativePumpPct": ov["cumulativePct"],
            }
        for r in state_store.get_rsi_alert_pairs(scope):
            if r["rawSymbol"] in candidates:
                continue
            candidates[r["rawSymbol"]] = {
                "symbol": r["symbol"], "expectedVerdict": "SELL" if r["direction"] == "high" else "BUY",
                "source": "rsi", "cumulativePumpPct": None,
            }

        if not candidates:
            continue

        try:
            tokens_by_raw = {t["rawSymbol"]: t for t in get_token_list(scope)["tokens"]}
        except Exception as exc:
            log.error(f"High alert watch: token list fetch failed for {scope}: {exc}")
            continue

        try:
            news_items = get_news_feed(limit=100)
        except Exception as exc:
            log.error(f"High alert watch: news feed fetch failed, continuing without news context: {exc}")
            news_items = []

        for raw_symbol, candidate in candidates.items():
            token = tokens_by_raw.get(raw_symbol)
            if token is None:
                continue  # delisted/no longer on the ticker list

            try:
                result = analyze_one_pair(
                    raw_symbol, candidate["symbol"], scope,
                    token.get("usdtVolume24h", 0), enabled_indicators, None, news_items,
                    change_24h=token.get("change24h"),
                    funding_rate=token.get("fundingRate"), open_interest=token.get("openInterest"),
                )
            except Exception as exc:
                log.error(f"High alert watch: analysis failed for {raw_symbol} ({scope}): {exc}")
                continue

            if not result.get("tradeable") or result.get("verdict") != candidate["expectedVerdict"]:
                continue
            confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
            if confidence < min_confidence:
                continue

            result["exchange"] = scope
            result["cumulativePumpPct"] = candidate["cumulativePumpPct"]
            result["highAlertSource"] = candidate["source"]
            results.append(result)

    return results


async def _get_or_run_check(market: str, scopes: list, cfg: dict, enabled_indicators) -> list:
    """Same shared-cache-per-market pattern as the other watchers - only one of these fairly expensive checks in flight per market at a time."""
    max_age = cfg.get("check_interval_seconds", 300)
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
        result = await loop.run_in_executor(SCAN_EXECUTOR, _run_high_alert_check, scopes, cfg, enabled_indicators)
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
        log.error(f"High alert watch: unknown market {market!r} for chat {chat_id}, stopping job")
        job.schedule_removal()
        return

    settings = context.bot_data.get("settings", {})
    cfg = settings.get("high_alert_watch", {})
    cooldown_seconds = cfg.get("cooldown_seconds", 3600)
    enabled_indicators = state_store.get_enabled_indicators()
    scopes = MARKET_SCOPE_MAP[market]

    try:
        results = await _get_or_run_check(market, scopes, cfg, enabled_indicators)
    except Exception as exc:
        log.error(f"High alert watch: check failed for chat {chat_id} (market={market}): {exc}")
        return

    now = time.time()
    for result in results:
        cooldown_key = (chat_id, result.get("rawSymbol"))
        if now - _last_push.get(cooldown_key, 0) < cooldown_seconds:
            continue

        try:
            mm_cfg = settings.get("money_management", {})
            wallet_bal = state_store.get_wallet_balance(chat_id)
            serial = state_store.next_signal_serial(chat_id, "watcher")
            confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
            plan = result.get("tradePlan")
            trade_id = None
            if plan:
                trade_id = state_store.record_signal_outcome_tracking(
                    chat_id, "watcher", result.get("exchange", ""), result.get("rawSymbol", ""),
                    result.get("symbol", "?"), result.get("verdict", "?"),
                    plan["entry"], plan["stopLoss"], plan.get("tp1"), plan.get("tp2"), plan.get("tp3"),
                    confidence=confidence,
                    scan_label=f"High Alert #{serial}" if serial is not None else "High Alert",
                )
            if result.get("highAlertSource") == "rsi":
                badge = (
                    f"🎯 *High Alert — RSI Extreme Pair* "
                    f"_(flagged by the RSI Extreme watch before this confirmation)_"
                )
            else:
                badge = (
                    f"🎯 *High Alert — Overextended Pair* "
                    f"_(cumulative pump +{result.get('cumulativePumpPct', 0):.0f}% before this)_"
                )
            text = format_strong_signal(result, serial, wallet_bal, mm_cfg, trade_id=trade_id, badge=badge)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            _last_push[cooldown_key] = now
            state_store.log_signal(
                chat_id, "watcher", result.get("exchange", ""), result.get("symbol", "?"),
                result.get("verdict", "?"), confidence, message_text=text,
            )
        except Exception as exc:
            log.error(f"High alert watch: failed to send push to chat {chat_id}: {exc}")