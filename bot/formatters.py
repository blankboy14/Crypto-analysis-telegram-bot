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
        return f"{v:.2f}"
    elif v >= 1:
        return f"{v:.4f}"
    elif v >= 0.01:
        return f"{v:.6f}"
    else:
        return f"{v:.8f}"


def _send_time_line() -> str:
    """
    'Send Time: 14:32:07 UTC | Date: 24-07-2026' - stamped fresh at the
    moment each signal message is actually formatted/sent, so the user
    can tell exactly when a signal went out (useful once several
    signals for the same or different pairs have piled up in a chat).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return f"Send Time: `{now.strftime('%H:%M:%S')} UTC` | Date: `{now.strftime('%d-%m-%Y')}`"


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


def _fmt_usdt(value: float) -> str:
    """Compact $-prefixed T/B/M/K rounding, same scale style used for volume elsewhere in this file."""
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000_000_000:
        return f"{sign}${v / 1_000_000_000_000:.2f}T"
    if v >= 1_000_000_000:
        return f"{sign}${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{sign}${v / 1_000:.1f}K"
    return f"{sign}${v:.0f}"


def format_absolute_volume_alert(pair: str, last_price: float, interval_volume: float,
                                  window_seconds: float, threshold_usdt: float, market: str) -> str:
    """
    24/7 Market Analyse add-on #1 - a pair's traded volume just crossed
    a fixed absolute USDT bar within a rolling window (e.g. 60M+ within
    30 minutes), regardless of whether that's unusual for this
    particular pair or not.
    """
    window_minutes = window_seconds / 60
    return (
        f"🚨 *Massive Volume Surge* ({market.title()})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair: `{pair}`\n"
        f"Last Price: `{_fmt_price(last_price)}`\n"
        f"Traded in last ~{window_minutes:.0f}m: *{_fmt_usdt(interval_volume)}* "
        f"(threshold: {_fmt_usdt(threshold_usdt)})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_A huge amount of money just moved through this pair in a short "
        f"window - worth checking what's driving it._"
    )


def format_daily_mover_alert(pair: str, last_price: float, pct_change: float, market: str) -> str:
    """
    24/7 Market Analyse add-on #2 - a pair has moved past
    move_threshold_pct away from its price at today's reset time.
    """
    arrow = "🔺" if pct_change > 0 else "🔻"
    return (
        f"📅 *Daily Big Mover* ({market.title()})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair: `{pair}`\n"
        f"Last Price: `{_fmt_price(last_price)}`\n"
        f"Since today's reset: {arrow} *{pct_change:+.2f}%*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_This pair has moved a lot since the start of today's trading "
        f"window._"
    )


def format_signal_outcome_update(outcome: dict, new_status: str, new_highest_tp_hit: int,
                                  new_current_stop: float | None = None) -> str:
    """
    jobs/signal_outcome_tracker.py's notification when a tracked
    signal's price actually crosses a level - closes the loop on a
    signal that was sent earlier, automatically.

    `new_current_stop`, when set, means the stop was just trailed
    (breakeven after TP1 / up to TP1 after TP2) - called out explicitly
    so the user can see the protection actually happened, not just
    infer it from the outcome later.
    """
    pair = outcome["symbol"]
    verdict = outcome["verdict"]

    if new_status == "sl_hit":
        if new_highest_tp_hit > 0:
            return (
                f"🔁 *Stop Hit (after TP{new_highest_tp_hit})* — `{pair}` ({verdict})\n"
                f"Reached TP{new_highest_tp_hit} earlier, then reversed and hit the (trailed) stop.\n"
                f"_This closed as a scratch/small gain from the TP{new_highest_tp_hit} move, not a full loss - "
                f"the stop had already been moved up to protect it._"
            )
        return (
            f"🛑 *Stop Loss Hit* — `{pair}` ({verdict})\n"
            f"_This signal didn't work out - price hit the stop loss without reaching any target._"
        )

    tp_n = new_status.replace("tp", "").replace("_hit", "")
    if new_status == "tp3_hit":
        return (
            f"🎯 *Final Target Reached (TP3)* — `{pair}` ({verdict})\n"
            f"_Full target hit - the complete trade plan played out as intended._"
        )
    stop_note = ""
    if new_current_stop is not None:
        if new_highest_tp_hit == 1:
            stop_note = f"\n_Stop moved to breakeven (`{_fmt_price(new_current_stop)}`) - this can no longer close as a full loss._"
        elif new_highest_tp_hit == 2:
            stop_note = f"\n_Stop moved up to TP1 (`{_fmt_price(new_current_stop)}`) - at least the TP1 gain is now locked in._"
    return (
        f"✅ *TP{tp_n} Hit* — `{pair}` ({verdict})\n"
        f"_Still tracking toward TP{int(tp_n) + 1} - will notify again if it moves further or reverses._"
        f"{stop_note}"
    )


def format_signal_outcomes_status(stats: dict) -> str:
    """
    "📊 Signal Outcomes" - the automated version of manually tallying
    "how many of my signals actually hit SL vs TP1/2/3". Every count
    here comes from jobs/signal_outcome_tracker.py actually checking
    real candle data against each signal's own Entry/SL/TP levels, not
    a guess.
    """
    lines = [
        "📊 *Signal Outcomes*",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    win_rate = stats.get("winRate")
    total_closed = stats.get("totalClosed", 0)
    if total_closed:
        lines.append(f"Win rate: *{win_rate:.0f}%* _(of {total_closed} closed signals)_")
    else:
        lines.append("_No closed signals yet - check back once some have run their course._")

    lines += [
        "",
        "✅ *Closed*",
        f"  • Full target (TP3): {stats.get('tp3_hit', 0)}",
        f"  • Reversed to SL after reaching TP2: {stats.get('sl_hit_after_tp2', 0)}",
        f"  • Reversed to SL after reaching TP1: {stats.get('sl_hit_after_tp1', 0)}",
        f"  • Clean stop loss (no target reached): {stats.get('sl_hit_clean', 0)}",
        "",
        "🕒 *Still Running*",
        f"  • Sitting past TP2, targeting TP3: {stats.get('tp2_hit', 0)}",
        f"  • Sitting past TP1, targeting TP2: {stats.get('tp1_hit', 0)}",
        f"  • Not yet reached TP1: {stats.get('pending', 0)}",
    ]

    return "\n".join(lines)


def format_tier_move_alert(pair: str, last_price: float, pct_change: float, candle_interval: str,
                            candle_volume_usdt: float, market: str) -> str:
    """
    24/7 Market Analyse add-on #3 - one of the tightly-watched "usually
    stable" pairs (BTC/ETH/SOL/...) just made an unusually large move
    for itself, confirmed by real candle volume so it isn't just a
    thin-liquidity wick.
    """
    arrow = "🔺" if pct_change > 0 else "🔻"
    return (
        f"⚡ *Unusual Move — Major Pair* ({market.title()})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair: `{pair}`\n"
        f"Last Price: `{_fmt_price(last_price)}`\n"
        f"{candle_interval} candle move: {arrow} *{pct_change:+.2f}%*\n"
        f"{candle_interval} candle volume: *{_fmt_usdt(candle_volume_usdt)}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_This pair usually moves in small steps - a move this size, "
        f"backed by real volume, is worth a closer look._"
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


def format_money_management_block(mm: dict | None) -> str:
    """
    Phase - Money Management add-on. `mm` is
    engine.risk_manager.compute_money_management()'s return value
    (None if no wallet balance has been saved yet for this chat, or
    the trade plan didn't have both an entry and a stop-loss).
    """
    if not mm:
        return (
            "💰 *Money Management*: not set — tap *Wallet Balance* and "
            "type your balance to get a position size + leverage suggestion here."
        )
    cap_note = (
        "\n_⚠️ Position capped by your max-leverage setting — actual risk "
        "came out below your target %, not above it._" if mm.get("capped") else ""
    )
    return (
        f"💰 *Money Management* _(wallet: `{mm['balance']:,.2f} USDT`)_\n"
        f"Suggested Leverage: *{mm['leverage']}x*\n"
        f"Position Size: `{mm['positionNotional']:,.2f} USDT`\n"
        f"Margin Needed: `{mm['marginRequired']:,.2f} USDT`\n"
        f"Risk if SL hits: `{mm['actualRiskAmount']:,.2f} USDT` (*{mm['actualRiskPct']:.2f}%*"
        f" of wallet, target {mm['targetRiskPct']:.1f}%)"
        f"{cap_note}"
    )


def format_wallet_balance_ask() -> str:
    """Sent when the user presses the 'Wallet Balance' button."""
    return (
        "💰 *Wallet Balance*\n"
        "Type the USDT balance you want position sizes calculated against "
        "(e.g. `500` or `1250.50`).\n\n"
        "_This is a number you tell the bot, not a live account balance - "
        "the bot has no exchange trading access and never places, changes, "
        "or closes any order for you. It only uses this figure to size a "
        "suggested position + leverage next to each signal (Search Signal, "
        "Find Strong Signal, Single Pair Analyse), scaled so a stop-loss "
        "hit costs a fixed % of this balance. Not financial advice - "
        "always confirm any size/leverage yourself before trading._"
    )


def format_wallet_balance_saved(balance: float) -> str:
    return (
        f"✅ Wallet balance set to `{balance:,.2f} USDT`.\n"
        f"Every new signal from now on will include a Money Management "
        f"suggestion sized against it. Press *Wallet Balance* again any "
        f"time to update it."
    )


def format_wallet_balance_bad_number() -> str:
    return (
        "That doesn't look like a valid balance. Please type a positive "
        "number, e.g. `500` or `1250.50`."
    )


def _trade_plan_block(result: dict, wallet_balance: float | None = None, mm_cfg: dict | None = None) -> str:
    """
    `wallet_balance`/`mm_cfg` are only passed by call sites that want
    the Money Management block appended (see engine.risk_manager) -
    every call site in this file now passes them through from
    bot/state_store.get_wallet_balance() and settings.yaml's
    money_management section, so it always shows. Kept optional here
    (rather than required) so this stays easy to call from anywhere
    that genuinely doesn't have a chat/balance context.
    """
    from engine.risk_manager import compute_money_management

    plan = result.get("tradePlan") or {}
    lines = [
        f"Entry: `{_fmt_price(plan.get('entry'))}`",
        f"SL: `{_fmt_price(plan.get('stopLoss'))}`",
        f"TP1: `{_fmt_price(plan.get('tp1'))}`",
        f"TP2: `{_fmt_price(plan.get('tp2'))}`",
        f"TP3: `{_fmt_price(plan.get('tp3'))}`",
    ]
    if mm_cfg is not None:
        mm = compute_money_management(
            wallet_balance, plan.get("entry"), plan.get("stopLoss"),
            result.get("symbol"), mm_cfg,
        )
        lines.append("")
        lines.append(format_money_management_block(mm))
    return "\n".join(lines)


def format_strong_signal(result: dict, serial: int | None = None,
                          wallet_balance: float | None = None, mm_cfg: dict | None = None) -> str:
    """
    Phase 2.2 - ONE high-confidence result from
    engine.signal_scanner.scan_market_above_confidence(), sent by
    jobs/strong_signal_watcher.py.

    As of the "single signal, full analysis" rework: the watcher now
    sends at most one of these per tick (its own best-ranked candidate),
    instead of looping through every qualifying pair and firing them
    back-to-back - so this format carries the full breakdown that used
    to only live in Single Pair Analyse (format_single_pair_report),
    not just a one-line verdict. `serial` is a per-chat running count
    (state_store.next_signal_serial) purely so the user can track "how
    many signals have I actually gotten", not a ranking of any kind.

    Deliberately calmer/plainer than the old version: no siren emoji,
    no dense divider walls - reads like a written analysis a person
    would hand you, not an alarm.
    """
    pair = result.get("symbol", "?")
    verdict = result.get("verdict", "?")
    confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
    change24h = result.get("change24h")
    change_str = f"{change24h:+.2f}%" if isinstance(change24h, (int, float)) else "N/A"
    serial_str = f"#{serial}" if serial is not None else ""

    lines = [
        f"*Trade Signal {serial_str}*".strip(),
        f"`{pair}`  —  24H: *{change_str}*",
        "",
        "*Indicator Performance*",
        _bullet_votes(result.get("indicatorVotes"), result.get("indicatorInfo", "N/A")),
        "",
        "*Concept Performance*",
        _bullet_votes(result.get("conceptVotes"), result.get("conceptInfo", "N/A")),
        "",
        "*Order Flow*",
        f"  • Live tape: {result.get('orderFlowInfo', 'N/A')}",
    ]

    funding_info = result.get("fundingRateInfo")
    oi_info = result.get("openInterestInfo")
    if funding_info and "spot pair" not in funding_info:
        lines.append(f"  • Funding rate: {funding_info}")
    if oi_info:
        lines.append(f"  • Open interest: {oi_info}")

    lines += [
        "",
        f"*Executed Trade:* {_verdict_emoji(verdict)} {verdict}",
        f"*Reason:* {result.get('explanation', 'N/A')}",
        f"*Confidence:* {confidence:.0f}/100",
        _send_time_line(),
        "",
        f"{_trade_plan_block(result, wallet_balance, mm_cfg)}",
    ]

    return "\n".join(lines)


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
            f"{_send_time_line()}\n"
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


def _truncate_info(text: str, max_len: int = 300) -> str:
    """
    Caps a comma-joined info string (indicatorInfo/conceptInfo) at a
    safe length for the bulk "Full Analysis" per-pair block - unlike
    Single Pair Analyse's one-pair-at-a-time bulleted view, this one
    has to keep several pairs' worth of these strings together under
    Telegram's message-size limit, so an unusually vote-heavy pair
    can't single-handedly blow out a whole batch.
    """
    if not text or len(text) <= max_len:
        return text
    parts = text.split(", ")
    kept, total = [], 0
    for p in parts:
        if total + len(p) + 2 > max_len:
            break
        kept.append(p)
        total += len(p) + 2
    remaining = len(parts) - len(kept)
    if remaining <= 0:
        return text[:max_len].rstrip() + "..."
    return ", ".join(kept) + f", +{remaining} more"


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
        f"Indicator information: {_truncate_info(result.get('indicatorInfo', 'N/A'))}",
        f"Concept information: {_truncate_info(result.get('conceptInfo', 'N/A'))}",
        f"Order flow information: {result.get('orderFlowInfo', 'N/A')}",
    ]
    funding_info = result.get("fundingRateInfo")
    oi_info = result.get("openInterestInfo")
    if funding_info and "spot pair" not in funding_info:
        lines.append(f"Funding rate: {funding_info}")
        if oi_info:
            lines.append(f"Open interest: {oi_info}")
    lines.append(f"Executed trade: {'Yes' if tradeable else 'No'}")
    if tradeable:
        lines.append(f"Trading: {verdict}")
    elif result.get("reason"):
        lines.append(f"Reason: {result['reason']}")
        lines.append(f"Confidence level: {confidence:.0f}/100")
    return "\n".join(lines)


def format_pair_detail_batch(blocks: list) -> str:
    """Joins several format_pair_detail_block() outputs into one message."""
    return "\n\n".join(blocks)


def format_signal_scan_block(index: int, result: dict, wallet_balance: float | None = None,
                              mm_cfg: dict | None = None) -> str:
    """
    ONE "Signal Scan #N" block - factored out of format_final_signal_scan
    so the handler can log the exact text of each individual result
    (for the Search Signal Status button) while still sending them all
    joined together as one message, same as before.
    """
    confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
    verdict = result.get("verdict", "?")
    pair = result.get("symbol", "?")
    return (
        f"📡 *Signal Scan #{index}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_verdict_emoji(verdict)} *{verdict}* — `{pair}`\n"
        f"Confidence: *{confidence:.0f}/100*\n"
        f"{_send_time_line()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_trade_plan_block(result, wallet_balance, mm_cfg)}"
    )


def format_final_signal_scan(top_picks: list, wallet_balance: float | None = None, mm_cfg: dict | None = None) -> str:
    """
    Final "Signal Scan #1 / #2 / #3" block for Search Signal, in EXACT
    descending confidence order (highest confidence = #1) - distinct
    from engine.signal_scanner's own topPicks ordering, which ranks by
    rankScore (magnitude x confidence), not confidence alone. Callers
    should pass results already re-sorted by combinedConfidence desc.
    """
    if not top_picks:
        return "🔎 Scan complete — no tradeable setup found right now. Try again later."

    blocks = [format_signal_scan_block(i, result, wallet_balance, mm_cfg) for i, result in enumerate(top_picks, start=1)]
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


def _bullet_votes(votes: list, joined_fallback: str) -> str:
    """
    Renders a list of {"note", "direction"} vote dicts as one bullet
    per line ("  • note — bullish/bearish"). Falls back to the old
    comma-joined one-liner if no list is available (e.g. a result
    built before indicatorVotes/conceptVotes existed) - still readable,
    just not broken into bullets.
    """
    if not votes:
        return f"  {joined_fallback}" if joined_fallback and joined_fallback != "No clear signal" else "  No clear signal"
    lines = []
    for v in votes:
        direction_label = "bullish 🟢" if v.get("direction", 0) > 0 else "bearish 🔴"
        lines.append(f"  • {v.get('note', '?')} — {direction_label}")
    return "\n".join(lines)


def format_single_pair_report(result: dict, wallet_balance: float | None = None, mm_cfg: dict | None = None) -> str:
    """
    Full report for one pair from engine.signal_scanner.analyze_one_pair()
    - the same pipeline every scan uses, so this is a trustworthy
    standalone read on a pair, not a lighter/separate analysis. Always
    shows a verdict either way: a trade plan if tradeable, or the exact
    reason it isn't (mirrors format_pair_detail_block's "why not"
    honesty) rather than a bare "no" with nothing to go on.

    `wallet_balance`/`mm_cfg` are passed through to _trade_plan_block
    exactly like format_strong_signal/format_final_signal_scan do, so
    Single Pair Analyse also gets a Money Management suggestion.
    """
    pair = result.get("symbol", "?")
    scope_label = SCOPE_LABELS.get(result.get("exchange"), result.get("exchange", "?"))
    change24h = result.get("change24h")
    change_arrow = ""
    if isinstance(change24h, (int, float)):
        change_arrow = "🔺" if change24h > 0 else ("🔻" if change24h < 0 else "▪️")
    change_str = f"{change_arrow} {change24h:+.2f}%" if isinstance(change24h, (int, float)) else "N/A"
    tradeable = result.get("tradeable", False)

    lines = [
        f"🎯 *Single Pair Analyse* — `{pair}` ({scope_label})",
        "━━━━━━━━━━━━━━━━━━━━",
        f"24H Change: {change_str}",
        "",
        "📊 *Indicators*",
        _bullet_votes(result.get("indicatorVotes"), result.get("indicatorInfo", "N/A")),
        "",
        "🧩 *Concepts*",
        _bullet_votes(result.get("conceptVotes"), result.get("conceptInfo", "N/A")),
        "",
        "📈 *Order Flow*",
        f"  {result.get('orderFlowInfo', 'N/A')}",
    ]

    funding_info = result.get("fundingRateInfo")
    oi_info = result.get("openInterestInfo")
    if funding_info and "spot pair" not in funding_info:
        lines += ["", "💹 *Futures Metrics*", f"  • {funding_info}"]
        if oi_info:
            lines.append(f"  • {oi_info}")

    if tradeable:
        confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
        verdict = result.get("verdict", "?")
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{_verdict_emoji(verdict)} *Executed Trade: {verdict}*",
            f"Confidence: *{confidence:.0f}/100*",
            f"{_send_time_line()}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{_trade_plan_block(result, wallet_balance, mm_cfg)}",
        ]
    else:
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"❌ *Not currently tradeable*\n_{result.get('reason', 'N/A')}_",
        ]

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
    """
    "Find 24/7 Strong Signal — Status". The last-signals section here
    is scoped to ONLY signals the 24/7 watcher itself pushed
    (never Search Signal or Single Pair Analyse - those get their own
    "Search Signal — Status" button instead), and shows each one's
    full original message (Entry/SL/TP/Send Time and all), not a
    one-line summary.

    `status["minConfidenceToPush"]`/`status["scanIntervalSeconds"]` are
    optional (set by the handler from settings.yaml) - when present,
    they're used to spell out WHY a scan can complete successfully
    with zero signals pushed, which is the single most confusing thing
    about this report otherwise (a "check" is a full market scan, NOT
    a signal - it can run clean and still turn up nothing worth
    sending).
    """
    header = "🔥 *Find 24/7 Strong Signal — Status*"
    on_line = f"Currently: ✅ *ON* ({MARKET_LABELS.get(status['market'], status['market'])})" if status["isOn"] else "Currently: 🛑 *OFF*"
    lines = [header, "━━━━━━━━━━━━━━━━━━━━", on_line]
    if status["isOn"]:
        lines.append(f"Running for: {_duration_since(status['since'])}")
        interval = status.get("scanIntervalSeconds")
        if interval:
            lines.append(f"Scans the whole market every {int(interval // 60)}m")

    w = status["watcherScans"]
    s = status["searchScans"]
    watcher_runs = w["success"] + w["failed"]
    search_runs = s["success"] + s["failed"]
    min_conf = status.get("minConfidenceToPush")

    lines += [
        "",
        "📋 *Scan Activity* _(a scan run ≠ a signal - see below)_",
        f"  • 24/7 watcher: {watcher_runs} full market scan(s) run "
        f"(✅ {w['success']} completed, ❌ {w['failed']} failed)",
        f"  • Search Signal (manual button): {search_runs} run(s) "
        f"(✅ {s['success']} completed, ❌ {s['failed']} failed)",
    ]
    if min_conf is not None:
        lines.append(
            f"  _A completed scan only sends a message here if it finds a setup scoring "
            f"{min_conf:.0f}+/100 confidence - otherwise it finishes with nothing to push, "
            f"which is normal, not a malfunction._"
        )

    lines += [
        "",
        f"📨 *Signals Actually Sent* by the 24/7 watcher — Spot: *{status['spotSignalCount']}*, "
        f"Future: *{status['futureSignalCount']}*",
    ]

    last_signals = status.get("lastSignals") or []
    if last_signals:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("📜 *Last Signals Sent by 24/7 Watcher*")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        blocks = [_status_signal_block(sig) for sig in last_signals]
        lines.append("\n\n".join(blocks))
    else:
        lines.append("")
        lines.append("_No signals sent by the 24/7 watcher yet - it's still scanning "
                      "and hasn't found a setup that cleared the confidence bar above._")

    return "\n".join(lines)


def format_search_signal_status(status: dict) -> str:
    """
    "Search Signal — Status" - same report shape as
    format_strong_signal_status() above, scoped to ONLY one-shot Search
    Signal runs. No on/off line since Search Signal isn't a 24/7 toggle.
    """
    lines = [
        "🔎 *Search Signal — Status*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Total runs (manual button presses): *{status['totalRuns']}* "
        f"(✅ {status['successRuns']} completed, ❌ {status['failedRuns']} failed)",
        "",
        f"Signals found — Spot: *{status['spotSignalCount']}*, Future: *{status['futureSignalCount']}*",
    ]

    last_signals = status.get("lastSignals") or []
    if last_signals:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("📜 *Last Search Signals*")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        blocks = [_status_signal_block(sig) for sig in last_signals]
        lines.append("\n\n".join(blocks))
    else:
        lines.append("")
        lines.append("_No Search Signal runs with a tradeable result yet._")

    return "\n".join(lines)


def _status_signal_block(sig: dict) -> str:
    """
    A single entry in a Status button's signal list - the exact
    original message if it was logged with one, otherwise (older rows
    logged before message_text existed) a plain one-line fallback.
    """
    if sig.get("messageText"):
        return sig["messageText"]
    scope_label = SCOPE_LABELS.get(sig["scope"], sig["scope"])
    conf = sig.get("confidence")
    conf_str = f"{conf:.0f}/100" if isinstance(conf, (int, float)) else "N/A"
    return f"{_verdict_emoji(sig['verdict'])} *{sig['verdict']}* — `{sig['symbol']}` ({scope_label}, {conf_str}) — {_ago(sig['ts'])}"