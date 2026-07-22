"""
bot/formatters.py

Turns raw signal-scanner / watcher output into the Markdown messages
the bot actually sends. Kept separate from handlers/jobs so wording
can be tweaked in one place without touching any scanning or
scheduling logic.
"""


def _fmt_price(value) -> str:
    """
    Adaptive decimal precision so Entry/SL/TP never shows a raw,
    ugly float like `472.91125357` or `3.53477583` - the right number
    of decimals depends on the pair's own price scale (a $472 pair
    doesn't need 8 decimals; a $0.003 pair needs more than 2 or every
    level rounds to the same number). Trims trailing zeros so a clean
    value like 391.30 shows as 391.3, not 391.30000.
    """
    if value is None:
        return "N/A"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "N/A"

    if v >= 100:
        s = f"{v:,.2f}"
    elif v >= 1:
        s = f"{v:.4f}"
    elif v >= 0.01:
        s = f"{v:.6f}"
    else:
        s = f"{v:.8f}"

    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _verdict_emoji(verdict: str) -> str:
    v = (verdict or "").upper()
    if "BUY" in v:
        return "🟢"
    if "SELL" in v:
        return "🔴"
    return "⚪"


def format_volume_spike_alert(pair: str, last_price: float, pct_change: float, direction: str, market: str) -> str:
    """
    Phase 2.1 - pushed by jobs/volume_spike_watcher.py whenever a pair
    suddenly moves past its threshold (BTC uses its own tighter one -
    see config/settings.yaml's volume_spike_watch section for both).
    `direction` is "up" or "down".
    """
    arrow = "🔺" if direction == "up" else "🔻"
    return (
        f"{arrow} *Sudden Move Detected*\n"
        f"_{market.title()} Market_\n"
        f"\n"
        f"Pair: `{pair}`\n"
        f"Last Price: `{_fmt_price(last_price)}`\n"
        f"Move: *{pct_change:+.2f}%* {direction.upper()}"
    )


def format_volume_burst_alert(pair: str, last_price: float, interval_volume: float,
                               baseline_volume: float, multiplier: float, market: str,
                               buy_pct: float | None) -> str:
    """
    Phase 2.1 add-on - pushed by jobs/volume_spike_watcher.py when a
    pair's TRADED VOLUME (not price) suddenly jumps well above its own
    recent baseline - e.g. a pair that's been quiet suddenly sees a
    burst of real trading activity, which often shows up before or
    alongside a price move rather than after it. `buy_pct` is the live
    order-flow tape's buy-side percentage at the moment of detection,
    if it was available (None if the order-flow fetch failed).
    """
    if buy_pct is None:
        flow_line = "Order flow: not available right now"
    else:
        bias = "buy-side" if buy_pct >= 50 else "sell-side"
        flow_line = f"Order flow: *{buy_pct:.1f}%* {bias}"
    return (
        f"📊 *Volume Burst Detected* ({market.title()})\n\n"
        f"Pair: `{pair}`\n"
        f"Last Price: `{_fmt_price(last_price)}`\n"
        f"Traded volume this window: *${interval_volume:,.0f}* "
        f"(~{multiplier:.1f}x its recent baseline of ${baseline_volume:,.0f})\n"
        f"{flow_line}\n\n"
        f"_This pair was trading quietly and just saw a real burst of volume - "
        f"worth a closer look._"
    )


def format_pump_reversal_alert(pair: str, market: str, cumulative_pct: float, peak_price: float,
                                current_price: float, drop_pct: float, sell_pct: float | None) -> str:
    """
    Phase 2.2 add-on - pushed by jobs/strong_signal_watcher.py for a
    pair that was flagged as overextended (a large multi-day cumulative
    pump - see engine/pump_tracker.py) and has now started reversing
    with real sell pressure. Deliberately biased to read as a SELL
    call, not a neutral FYI - per the explicit request that extreme
    pumps reversing should be treated as high-probability SELL setups.
    """
    flow_line = f"Order flow: *{sell_pct:.1f}%* sell-side" if sell_pct is not None else "Order flow: not available right now"
    return (
        f"🔻 *Strong Signal — SELL (Pump Reversal)* ({market.title()})\n\n"
        f"Pair: `{pair}`\n"
        f"Cumulative pump: *+{cumulative_pct:.0f}%* before this reversal\n"
        f"Peak: `{_fmt_price(peak_price)}` → Now: `{_fmt_price(current_price)}` (*-{drop_pct:.1f}%* off peak)\n"
        f"{flow_line}\n\n"
        f"_This pair pumped hard and is now showing real reversal pressure - "
        f"extended parabolic moves like this tend to give back a large chunk of "
        f"the move fast. Bias: SELL._"
    )


def _trade_plan_block(result: dict) -> str:
    plan = result.get("tradePlan") or {}
    return (
        f"Entry: `{_fmt_price(plan.get('entry'))}`\n"
        f"SL: `{_fmt_price(plan.get('stopLoss'))}`\n"
        f"TP1: `{_fmt_price(plan.get('tp1'))}`\n"
        f"TP2: `{_fmt_price(plan.get('tp2'))}`\n"
        f"TP3: `{_fmt_price(plan.get('tp3'))}`"
    )


def format_strong_signal(result: dict) -> str:
    """
    Phase 2.2 - one high-confidence (80%+ by default) result from
    engine.signal_scanner.scan_market_above_confidence(), sent by
    jobs/strong_signal_watcher.py the moment its scan finds it.
    """
    confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
    verdict = result.get("verdict", "?")
    pair = result.get("symbol", "?")
    return (
        f"🔥 *Strong Signal*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_verdict_emoji(verdict)} *{verdict}* — `{pair}`\n"
        f"Confidence: *{confidence:.0f}/100*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_trade_plan_block(result)}"
    )


MARKET_DETAILS_LABELS = {
    "all": "All Pairs",
    "higher": "Higher Movers (highest % first)",
    "lower": "Lower Movers (lowest % first)",
    "number": "Top by Volume",
}


def format_market_details_header(market: str, detail_type: str, shown: int, total: int) -> str:
    """
    First message of a Market Details listing (bot/handlers/market_details.py)
    - market/type label plus a count, so the batches of pair lines that
    follow (format_market_details_batch) make sense even if this chat
    only scrolls back to the header.
    """
    market_label = MARKET_LABELS.get(market, market)
    type_label = MARKET_DETAILS_LABELS.get(detail_type, detail_type)
    if shown < total:
        count_line = f"Showing *{shown}* of *{total}* pairs"
    else:
        count_line = f"*{total}* pairs"
    return f"📋 *Market Details — {market_label} — {type_label}*\n\n{count_line}"


def format_market_details_line(index: int, symbol: str, price: float, change_24h: float | None,
                                volume_24h: float | None, scope_tag: str | None = None) -> str:
    """
    One row: # index, pair name, current price, current 24h%, 24h
    traded volume - exactly the fields asked for. `scope_tag` (e.g.
    "Spot"/"Future") is only passed when market="both", since the same
    symbol can legitimately appear on both and needs disambiguating.
    """
    name = f"{symbol} ({scope_tag})" if scope_tag else symbol
    change_str = f"{change_24h:+.2f}%" if isinstance(change_24h, (int, float)) else "N/A"
    if isinstance(volume_24h, (int, float)):
        if volume_24h >= 1_000_000_000_000:
            vol_str = f"${volume_24h / 1_000_000_000_000:.2f}T"
        elif volume_24h >= 1_000_000_000:
            vol_str = f"${volume_24h / 1_000_000_000:.2f}B"
        elif volume_24h >= 1_000_000:
            vol_str = f"${volume_24h / 1_000_000:.2f}M"
        elif volume_24h >= 1_000:
            vol_str = f"${volume_24h / 1_000:.1f}K"
        else:
            vol_str = f"${volume_24h:.0f}"
    else:
        vol_str = "N/A"
    return f"#{index}  `{name}`  —  `{price}`  |  {change_str}  |  Vol: {vol_str}"


def format_market_details_batch(lines: list) -> str:
    """Joins several format_market_details_line() rows into one message (same batching idea as format_pair_detail_batch)."""
    return "\n".join(lines)


def format_market_details_ask_number(market: str) -> str:
    market_label = MARKET_LABELS.get(market, market)
    return (
        f"🔢 {market_label} selected. How many top pairs (by 24h traded volume) do you want to see? "
        f"Just type a number, e.g. `50` or `100`."
    )


def format_market_details_bad_number() -> str:
    return "That doesn't look like a valid number. Please try Market Details again and type a whole number, e.g. `50`."


def format_market_details_empty(market: str, detail_type: str) -> str:
    market_label = MARKET_LABELS.get(market, market)
    type_label = MARKET_DETAILS_LABELS.get(detail_type, detail_type)
    return f"📋 *Market Details — {market_label} — {type_label}*\n\nNo pairs matched right now."


def format_market_details_fetch_error() -> str:
    return "⚠️ Couldn't reach the exchange to fetch pair details right now. Please try Market Details again shortly."


def format_search_signal_results(top_picks: list) -> str:
    """
    Phase 2.3 - up to 3 results from
    engine.signal_scanner.scan_market()'s topPicks, formatted as the
    "Signal Scan #1 / #2 / #3" list the spec calls for.
    """
    if not top_picks:
        return "🔎 Scan complete — no tradeable setup found right now. Try again later."

    blocks = []
    for i, result in enumerate(top_picks, start=1):
        confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
        verdict = result.get("verdict", "?")
        pair = result.get("symbol", "?")
        blocks.append(
            f"📡 *Signal Scan #{i}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{_verdict_emoji(verdict)} *{verdict}* — `{pair}`\n"
            f"Confidence: *{confidence:.0f}/100*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{_trade_plan_block(result)}"
        )
    return "\n\n".join(blocks)


# =========================================================================
# --- Phase 2.3 upgrade: total-pair count, analysis-depth prompt,
#     10/25/50/75/100% progress, per-pair detail, and the final
#     confidence-sorted Signal Scan #1-3 block. ---
# =========================================================================

MARKET_LABELS = {"spot": "Spot", "future": "Future", "both": "Spot + Future"}


def format_total_pairs_message(market: str, total: int, per_scope: dict) -> str:
    label = MARKET_LABELS.get(market, market)
    if market == "both":
        spot_n = per_scope.get("bitget-spot", 0)
        fut_n = per_scope.get("bitget-futures", 0)
        return (
            f"📊 *Total pairs in {label} market:* {total}\n"
            f"   • Spot: {spot_n}\n"
            f"   • Future: {fut_n}"
        )
    return f"📊 *Total pairs in {label} market:* {total}"


def format_progress_message(completed: int, total: int, pct: int) -> str:
    return f"⏳ Complete analyse scan pair: *{pct}%* ({completed}/{total})"


def format_pair_detail_block(index: int, result: dict) -> str:
    """
    Exact per-pair block requested for "Full Analysis" mode - one
    block per pair as it finishes, batched a handful at a time by the
    handler rather than one Telegram message per pair (which would hit
    Telegram's flood limits across a multi-hundred-pair scan).
    """
    pair = result.get("symbol", "?")
    change24h = result.get("change24h")
    change_str = f"{change24h:+.2f}%" if isinstance(change24h, (int, float)) else "N/A"
    tradeable = result.get("tradeable", False)
    verdict = result.get("verdict") if tradeable else None
    confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0) if tradeable else 0

    lines = [
        f"*#{index}*",
        f"Pair name: `{pair}`",
        f"24H %: {change_str}",
        f"Indicator information: {result.get('indicatorInfo', 'N/A')}",
        f"Concept information: {result.get('conceptInfo', 'N/A')}",
        f"Order flow information: {result.get('orderFlowInfo', 'N/A')}",
        f"Executed trade: {'Yes' if tradeable else 'No'}",
    ]
    if tradeable:
        lines.append(f"Trading: {verdict}")
        lines.append(f"Confidence level: {confidence:.0f}/100")
    return "\n".join(lines)


def format_pair_detail_batch(blocks: list) -> str:
    """Joins several format_pair_detail_block() outputs into one message."""
    return "\n\n".join(blocks)


def format_final_signal_scan(top_picks: list) -> str:
    """
    Final "Signal Scan #1 / #2 / #3" block for Search Signal, in EXACT
    descending confidence order (highest confidence = #1) - distinct
    from engine.signal_scanner's own topPicks ordering, which ranks by
    rankScore (magnitude x confidence), not confidence alone. Callers
    should pass results already re-sorted by combinedConfidence desc.
    """
    if not top_picks:
        return "🔎 Scan complete — no tradeable setup found right now. Try again later."

    blocks = []
    for i, result in enumerate(top_picks, start=1):
        confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
        verdict = result.get("verdict", "?")
        pair = result.get("symbol", "?")
        blocks.append(
            f"📡 *Signal Scan #{i}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{_verdict_emoji(verdict)} *{verdict}* — `{pair}`\n"
            f"Confidence: *{confidence:.0f}/100*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{_trade_plan_block(result)}"
        )
    return "\n\n".join(blocks)


# =========================================================================
# --- Single Pair Analyse ---
# =========================================================================

SCOPE_LABELS = {"bitget-spot": "Spot", "bitget-futures": "Future"}


def format_single_pair_not_found(typed: str, checked_markets: str) -> str:
    return (
        f"❌ Couldn't find a pair matching `{typed}` in {checked_markets}.\n"
        f"Double-check the spelling - e.g. `BTC/USDT` or `BTCUSDT`."
    )


def format_single_pair_report(result: dict) -> str:
    """
    Full report for one pair from engine.signal_scanner.analyze_one_pair()
    - the same pipeline every scan uses, so this is a trustworthy
    standalone read on a pair, not a lighter/separate analysis. Always
    shows a verdict either way: a trade plan if tradeable, or the exact
    reason it isn't (mirrors format_pair_detail_block's "why not"
    honesty) rather than a bare "no" with nothing to go on.
    """
    pair = result.get("symbol", "?")
    scope_label = SCOPE_LABELS.get(result.get("exchange"), result.get("exchange", "?"))
    change24h = result.get("change24h")
    change_str = f"{change24h:+.2f}%" if isinstance(change24h, (int, float)) else "N/A"
    tradeable = result.get("tradeable", False)

    lines = [
        f"🎯 *Single Pair Analyse* — `{pair}` ({scope_label})",
        f"24H %: {change_str}",
        f"Indicator information: {result.get('indicatorInfo', 'N/A')}",
        f"Concept information: {result.get('conceptInfo', 'N/A')}",
        f"Order flow information: {result.get('orderFlowInfo', 'N/A')}",
    ]

    if tradeable:
        confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
        verdict = result.get("verdict", "?")
        lines += [
            "",
            f"✅ *Executed Trade: {verdict}*",
            f"Confidence: *{confidence:.0f}/100*",
            f"{_trade_plan_block(result)}",
        ]
    else:
        lines += ["", f"❌ Not currently tradeable — {result.get('reason', 'N/A')}"]

    return "\n".join(lines)


# =========================================================================
# --- Status buttons ---
# =========================================================================


def _ago(iso_ts: str | None) -> str:
    """'2h 15m ago' style relative time from an ISO timestamp, or 'N/A'."""
    if not iso_ts:
        return "N/A"
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "N/A"
    delta = datetime.now(timezone.utc) - then
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h ago"
    if hours:
        return f"{hours}h {minutes}m ago"
    if minutes:
        return f"{minutes}m ago"
    return "just now"


def _duration_since(iso_ts: str | None) -> str:
    """'2h 15m' style elapsed duration from an ISO timestamp, or 'N/A'."""
    text = _ago(iso_ts)
    return text[:-4] if text.endswith(" ago") else text


def format_market_analyse_status(status: dict) -> str:
    if not status["isOn"]:
        return "📊 *24/7 Market Analyse — Status*\n\nCurrently: 🔕 *OFF*"

    label = MARKET_LABELS.get(status["market"], status["market"])
    lines = [
        "📊 *24/7 Market Analyse — Status*",
        "",
        f"Currently: ✅ *ON* ({label})",
        f"Running for: {_duration_since(status['since'])}",
        "",
    ]

    last = status.get("lastAlert")
    if last:
        arrow = "🔺" if last["direction"] == "up" else "🔻"
        lines.append(
            f"Last alert (any market): {arrow} `{last['symbol']}` {last['pctChange']:+.2f}% — {_ago(last['ts'])}"
        )
    else:
        lines.append("Last alert (any market): none yet")

    lines.append("")
    lines.append(f"*Spot* — {status['spotAlertCount']} alert(s) sent")
    last_spot = status.get("lastSpotAlert")
    if last_spot:
        arrow = "🔺" if last_spot["direction"] == "up" else "🔻"
        lines.append(f"  Last: {arrow} `{last_spot['symbol']}` {last_spot['pctChange']:+.2f}% — {_ago(last_spot['ts'])}")

    lines.append(f"*Future* — {status['futureAlertCount']} alert(s) sent")
    last_future = status.get("lastFutureAlert")
    if last_future:
        arrow = "🔺" if last_future["direction"] == "up" else "🔻"
        lines.append(f"  Last: {arrow} `{last_future['symbol']}` {last_future['pctChange']:+.2f}% — {_ago(last_future['ts'])}")

    return "\n".join(lines)


def format_strong_signal_status(status: dict) -> str:
    header = "🔥 *Find 24/7 Strong Signal — Status*"
    on_line = f"Currently: ✅ *ON* ({MARKET_LABELS.get(status['market'], status['market'])})" if status["isOn"] else "Currently: 🛑 *OFF*"
    lines = [header, "", on_line]
    if status["isOn"]:
        lines.append(f"Running for: {_duration_since(status['since'])}")

    w = status["watcherScans"]
    s = status["searchScans"]
    total_uses = w["success"] + w["failed"] + s["success"] + s["failed"]
    total_success = w["success"] + s["success"]
    total_failed = w["failed"] + s["failed"]

    lines += [
        "",
        f"Total used: *{total_uses}* (✅ {total_success} successful, ❌ {total_failed} failed)",
        f"  • 24/7 watcher checks: {w['success'] + w['failed']} (✅ {w['success']} / ❌ {w['failed']})",
        f"  • Search Signal runs: {s['success'] + s['failed']} (✅ {s['success']} / ❌ {s['failed']})",
        "",
        f"Signals found — Spot: *{status['spotSignalCount']}*, Future: *{status['futureSignalCount']}*",
    ]

    last_signals = status.get("lastSignals") or []
    if last_signals:
        lines.append("")
        lines.append("*Last 12 signals:*")
        for sig in last_signals:
            scope_label = SCOPE_LABELS.get(sig["scope"], sig["scope"])
            src_label = {"watcher": "24/7", "search": "Search", "single_pair": "Single Pair"}.get(sig["source"], sig["source"])
            conf = sig.get("confidence")
            conf_str = f"{conf:.0f}/100" if isinstance(conf, (int, float)) else "N/A"
            lines.append(f"  `{sig['symbol']}` {sig['verdict']} ({scope_label}, {conf_str}) — {src_label}, {_ago(sig['ts'])}")
    else:
        lines.append("\nNo signals generated yet.")

    return "\n".join(lines)