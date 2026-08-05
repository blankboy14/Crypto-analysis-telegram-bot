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
    'Send Time: 20:32:07 BDT | 14:32:07 UTC | Date: 24-07-2026' -
    stamped fresh at the moment each signal message is actually
    formatted/sent. Shows BOTH BDT (UTC+6, no DST) and UTC together so
    it lines up with whatever timezone the person's own chart is set
    to without them having to convert it themselves.
    """
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    now_bdt = now_utc + timedelta(hours=6)
    return (
        f"Send Time: `{now_bdt.strftime('%H:%M:%S')} BDT` | `{now_utc.strftime('%H:%M:%S')} UTC` "
        f"| Date: `{now_utc.strftime('%d-%m-%Y')}`"
    )


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
                                  window_seconds: float, threshold_usdt: float, market: str,
                                  direction: str = "up", price_window_pct_change: float | None = None,
                                  change_24h: float | None = None, is_main: bool = False,
                                  trend: dict | None = None) -> str:
    """
    24/7 Market Analyse add-on #1 - a pair's traded volume just crossed
    a fixed absolute USDT bar within a rolling window (e.g. 60M+ within
    30 minutes), regardless of whether that's unusual for this
    particular pair or not.

    `direction`/`price_window_pct_change` say whether that money pushed
    price UP or DOWN over the window - "a lot of money moved" alone
    doesn't say which way. `change_24h` and `trend` (30m/1h/4h/1D %
    moves, each from that timeframe's own latest candle) give the
    fuller picture of what's actually been happening on this pair,
    not just this one window.
    """
    window_minutes = window_seconds / 60
    direction_label = "🟢 UP" if direction == "up" else "🔴 DOWN"
    tier_label = "Main Coin" if is_main else "Meme/Alt Coin"

    def _pct_str(pct):
        if pct is None:
            return "N/A"
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.2f}%"

    lines = [
        f"🚨 *Massive Volume Surge* — {tier_label} ({market.title()})",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"Direction: *{direction_label}*  |  Last Price: `{_fmt_price(last_price)}`",
        f"Move over this window: *{_pct_str(price_window_pct_change)}*  |  24h Change: *{_pct_str(change_24h)}*",
        f"Traded in last ~{window_minutes:.0f}m: *{_fmt_usdt(interval_volume)}* "
        f"(threshold: {_fmt_usdt(threshold_usdt)})",
    ]

    if trend:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("*Trend across timeframes:*")
        for label, key in (("30m", "30m"), ("1h", "1h"), ("4h", "4h"), ("1D", "1d")):
            pct = trend.get(key)
            if pct is None:
                continue
            arrow = "🟢▲" if pct >= 0 else "🔴▼"
            lines.append(f"  {label}: {arrow} {_pct_str(pct)}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        "_A huge amount of money just moved through this pair in a short "
        "window - worth checking what's driving it._"
    )
    return "\n".join(lines)


def format_main_meme_move_alert(pair: str, last_price: float, pct_change: float,
                                 direction: str, tier: str, market: str) -> str:
    """
    24/7 Market Analyse add-on #4/#5 - a MAIN coin (BTC/ETH/SOL/BNB,
    tight symmetric bar - main_coin_watch in settings.yaml) or a
    MEME/alt coin (much wider, asymmetric bar - meme_coin_watch) just
    crossed its 24h% move threshold, read straight off the exchange's
    own rolling 24h% ticker field. `tier` ("main"/"meme") picks a
    visually distinct header so the two are unmistakable at a glance -
    a 3% BTC move and a 45% meme-coin move are very different events
    even though both trip this same code path.
    """
    direction_label = "🟢 UP" if direction == "up" else "🔴 DOWN"
    sign = "+" if pct_change >= 0 else ""
    if tier == "main":
        header = f"🏛 *Main Coin Move* ({market.title()})"
        note = "_A major coin rarely moves this much this fast - worth a look._"
    else:
        header = f"🐸 *Meme/Alt Coin Move* ({market.title()})"
        note = "_Large, fast move on this pair - can reverse just as fast, trade with caution._"
    return (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair: `{pair}`\n"
        f"Direction: *{direction_label}*  |  Last Price: `{_fmt_price(last_price)}`\n"
        f"24h Change: *{sign}{pct_change:.2f}%*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{note}"
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


def _format_ms_touch_time(ms: int) -> str:
    """Same BDT+UTC+Date format as _format_iso_send_time(), from a candle time in milliseconds - the exact moment a level was actually touched, not just when the tracker happened to notice it."""
    from datetime import datetime, timezone
    return _format_iso_send_time(datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat())


def format_live_trade_preview(plan: dict) -> str:
    """
    Compact addition under a Search Signal result: what this trade
    would ACTUALLY look like if activated right now with the full
    saved Wallet Balance, at this pair's real max leverage - not the
    detailed risk-based Money Management block, just the numbers that
    matter for a quick glance (leverage, margin, $ at each level).
    """
    if plan.get("belowMinSize"):
        need = plan.get("minUsdtNeeded")
        need_str = f"at least `{need:.2f} USDT`" if need else "more balance"
        return (
            f"⚠️ *If Activated:* your balance is too small for this pair even at {plan['leverage']}x max leverage - "
            f"need {need_str}."
        )

    def _line(label: str, usdt, pct) -> str | None:
        if usdt is None:
            return None
        sign = "+" if usdt >= 0 else ""
        return f"{label}: `{sign}{usdt:.2f} USDT` ({sign}{pct:.1f}%)"

    lines = [
        "*If Activated (Live Balance):*",
        f"Leverage: `{plan['leverage']}x`  |  Margin: `{plan['balance']:.2f} USDT`",
        _line("SL", plan.get("slUsdt"), plan.get("slPct")),
        _line("TP1", plan.get("tp1Usdt"), plan.get("tp1Pct")),
        _line("TP2", plan.get("tp2Usdt"), plan.get("tp2Pct")),
        _line("TP3", plan.get("tp3Usdt"), plan.get("tp3Pct")),
    ]
    return "\n".join(line for line in lines if line is not None)


def _trade_event_header(outcome: dict, touch_time_ms: int | None = None) -> str:
    """
    Shared header for every live tracking notification: Trade ID, the
    signal's ORIGINAL Send Time, and (when known) the exact Touch Time
    the level was actually crossed at - which can be well before the
    tracker got around to noticing it, so it's kept distinct from
    "when this message arrived".
    """
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"Trade ID   : `{outcome['tradeId']}`",
        f"Send Time  : {_format_iso_send_time(outcome['openedAt'])}",
    ]
    if touch_time_ms is not None:
        lines.append(f"Touch Time : {_format_ms_touch_time(touch_time_ms)}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _balance_result_block(balance_result: dict) -> str:
    sign = "+" if balance_result["usdt"] >= 0 else ""
    kind = "Realized" if balance_result["realized"] else "Floating"
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"💰 {kind} P/L: `{sign}{balance_result['usdt']:.2f} USDT` ({sign}{balance_result['pct']:.1f}%)",
    ]
    if balance_result.get("newBalance") is not None:
        lines.append(f"Wallet Balance now: `{balance_result['newBalance']:.2f} USDT`")
    return "\n".join(lines)


def format_signal_outcome_update(outcome: dict, new_status: str, new_highest_tp_hit: int,
                                  new_current_stop: float | None = None, is_catchup: bool = False,
                                  touch_time_ms: int | None = None, balance_result: dict | None = None) -> str:
    """
    jobs/signal_outcome_tracker.py's notification when a tracked
    signal's price actually crosses a level - closes the loop on a
    signal that was sent earlier, automatically.

    `is_catchup=True` means this level was already crossed BEFORE (or
    shortly after) the trade was activated, and this tick is only just
    discovering it - e.g. price already ran to TP2 and back to SL in
    the time between the signal being sent and the person actually
    pressing "Active a Trade". Worded as "had already" instead of
    "just", so it doesn't read as something happening live right now.
    `touch_time_ms` is the exact candle time the crossing happened at,
    shown as its own "Touch Time" line - especially useful alongside
    is_catchup, where it can be well before "now".

    `new_current_stop`, when set, means the stop was just trailed
    (breakeven after TP1 / up to TP1 after TP2) - called out explicitly
    so the user can see the protection actually happened, not just
    infer it from the outcome later.

    `balance_result`, only present for a "List with Balance" trade:
    {"usdt": float, "pct": float, "realized": bool, "newBalance": float|None}.
    realized=True only at a terminal close (SL or TP3) - that's the
    only point actual Wallet Balance changes; a TP1/TP2 touch on an
    open position only shows realized=False "floating" P/L, no balance
    change yet.
    """
    pair = outcome["symbol"]
    verdict = outcome["verdict"]
    header = _trade_event_header(outcome, touch_time_ms)
    already = "had already" if is_catchup else "just"
    balance_suffix = f"\n{_balance_result_block(balance_result)}" if balance_result else ""

    if new_status == "sl_hit":
        if new_highest_tp_hit > 0:
            return (
                f"📉 *Down (after TP{new_highest_tp_hit})* — `{pair}` ({verdict})\n"
                f"{header}\n"
                f"Price {already} reached TP{new_highest_tp_hit} earlier, then reversed and hit the (trailed) stop.\n"
                f"_This closed as a scratch/small gain from the TP{new_highest_tp_hit} move, not a full loss - "
                f"the stop had already been moved up to protect it._"
                f"{balance_suffix}"
            )
        return (
            f"🛑 *Close (Stop Loss Hit)* — `{pair}` ({verdict})\n"
            f"{header}\n"
            f"_This signal didn't work out - price {already} hit the stop loss without reaching any target._"
            f"{balance_suffix}"
        )

    tp_n = new_status.replace("tp", "").replace("_hit", "")
    if new_status == "tp3_hit":
        return (
            f"🎯 *Closed (Full Target — TP3)* — `{pair}` ({verdict})\n"
            f"{header}\n"
            f"_Full target {already} hit - the complete trade plan played out as intended._"
            f"{balance_suffix}"
        )
    stop_note = ""
    if new_current_stop is not None:
        if new_highest_tp_hit == 1:
            stop_note = f"\n_Stop moved to breakeven (`{_fmt_price(new_current_stop)}`) - this can no longer close as a full loss._"
        elif new_highest_tp_hit == 2:
            stop_note = f"\n_Stop moved up to TP1 (`{_fmt_price(new_current_stop)}`) - at least the TP1 gain is now locked in._"
    return (
        f"✅ *Touch TP-{tp_n}* — `{pair}` ({verdict})\n"
        f"{header}\n"
        f"Price {already} reached TP{tp_n}. "
        f"_Still tracking toward TP{int(tp_n) + 1} - will notify again if it moves further or reverses._"
        f"{stop_note}"
        f"{balance_suffix}"
    )


def format_entry_arrived_update(outcome: dict, is_catchup: bool = False, touch_time_ms: int | None = None) -> str:
    """
    jobs/signal_outcome_tracker.py's notification the moment price
    first touches an activated trade's entry level - the point real
    SL/TP tracking begins. `is_catchup=True` means entry was already
    reached before (or shortly after) activation, only just discovered
    this tick - worded "had already" rather than "just". `touch_time_ms`
    is the exact candle time entry was actually reached at.
    """
    pair = outcome["symbol"]
    verdict = outcome["verdict"]
    already = "had already" if is_catchup else "just"
    return (
        f"🟡 *Touch Entry* — `{pair}` ({verdict})\n"
        f"{_trade_event_header(outcome, touch_time_ms)}\n"
        f"_Price {already} reached entry (`{_fmt_price(outcome['entry'])}`) - now tracking SL/TP1/TP2/TP3 from here._"
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


def format_token_listing_alert(action: str, pair: str, market: str, details: dict | None) -> str:
    """
    24/7 Market Analyse add-on #6 - a pair just appeared on or
    disappeared from Bitget's own token list.

    `action`: "added" or "removed".
    `details`: current ticker fields (lastPrice/change24h/usdtVolume24h)
    for a NEW listing, or the LAST KNOWN snapshot of those same fields
    for a delisting - either way, whatever's known at the moment this
    fires. None means no usable price data either way (shows just the
    pair name + event).
    """
    if action == "added":
        header = f"🆕 *New Token Listed* ({market.title()})"
        detail_label = "Current Details"
        note = "_Just appeared on Bitget - no trading history yet, so treat any signal on it with extra caution._"
    else:
        header = f"🗑 *Token Delisted* ({market.title()})"
        detail_label = "Last Known Details"
        note = "_No longer tradeable on Bitget - any open position or signal on this pair needs to be closed out manually._"

    lines = [
        header,
        _send_time_line(),
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
    ]

    if details:
        lines.append(f"*{detail_label}:*")
        if details.get("lastPrice") is not None:
            lines.append(f"  Price: `{_fmt_price(details['lastPrice'])}`")
        if details.get("change24h") is not None:
            sign = "+" if details["change24h"] >= 0 else ""
            lines.append(f"  24h Change: {sign}{details['change24h']:.2f}%")
        if details.get("usdtVolume24h") is not None:
            lines.append(f"  24h Volume: {_fmt_usdt(details['usdtVolume24h'])}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(note)
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


def format_early_momentum_alert(pair: str, market: str, last_price: float, pct_change: float,
                                 direction: str, lookback_seconds: float) -> str:
    """
    "Find 24/7 Strong Signal" add-on: Early Momentum Watch (see
    jobs/strong_signal_watcher.py's early_watch_tick()). A pair the
    LAST full scan rated as unremarkable (below weak_confidence_ceiling)
    has moved sharply since then - a cheap, fast, ticker-only heads-up,
    deliberately NOT styled like a full Strong Signal (no confidence
    score, no trade plan) since this hasn't gone through the full
    indicator pipeline yet - it's a "go take a closer look" nudge, not
    a signal to act on directly.
    """
    direction_label = "🟢 UP" if direction == "up" else "🔴 DOWN"
    sign = "+" if pct_change >= 0 else ""
    lookback_minutes = lookback_seconds / 60
    return (
        f"👀 *Early Momentum Watch* ({market.title()})\n"
        f"{_send_time_line()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair: `{pair}`\n"
        f"Direction: *{direction_label}*  |  Last Price: `{_fmt_price(last_price)}`\n"
        f"Move in last ~{lookback_minutes:.0f}m: *{sign}{pct_change:.2f}%*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_This pair scored low on the last full scan but is moving fast now - "
        f"worth a Search Signal or Single Pair Analyse look. Not a full trade signal yet._"
    )


def format_pump_reversal_alert(pair: str, market: str, cumulative_pct: float, peak_price: float,
                                current_price: float, drop_pct: float, sell_pct: float | None,
                                entry: float | None = None, stop_loss: float | None = None,
                                tp1: float | None = None, tp2: float | None = None,
                                tp3: float | None = None, trade_id: str | None = None) -> str:
    """
    Phase 2.2 add-on - pushed by jobs/strong_signal_watcher.py for a
    pair that was flagged as overextended (a large multi-day cumulative
    pump) and has now started reversing with real sell pressure.
    Deliberately biased to read as a SELL call, not a neutral FYI - per
    the explicit request that extreme pumps reversing should be
    treated as high-probability SELL setups.

    `entry`/`stop_loss`/`tp1`/`tp2`/`tp3`/`trade_id`, when given, print
    the same Trade ID + trade-plan block every other signal type
    already has - a pump-reversal signal has always had strong
    evidence behind it (cumulative pump % + real order-flow
    confirmation) but never actually said what to DO about it in
    concrete price terms until now.
    """
    flow_line = f"Order flow: *{sell_pct:.1f}%* sell-side" if sell_pct is not None else "Order flow: not available right now"
    trade_id_line = f"Trade ID : `{trade_id}`\n" if trade_id else ""

    lines = [
        f"🔻 *Strong Signal — SELL* _(Pump Reversal)_ ({market.title()})",
        f"{trade_id_line}"
        f"{_send_time_line()}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"Cumulative pump: *+{cumulative_pct:.0f}%* before this reversal",
        f"Peak: `{_fmt_price(peak_price)}` → Now: `{_fmt_price(current_price)}` (*-{drop_pct:.1f}%* off peak)",
        flow_line,
    ]

    if entry is not None and stop_loss is not None:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"Entry: `{_fmt_price(entry)}`")
        lines.append(f"SL: `{_fmt_price(stop_loss)}`")
        if tp1 is not None:
            lines.append(f"TP1: `{_fmt_price(tp1)}`")
        if tp2 is not None:
            lines.append(f"TP2: `{_fmt_price(tp2)}`")
        if tp3 is not None:
            lines.append(f"TP3: `{_fmt_price(tp3)}`")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        "_This pair pumped hard and is now showing real reversal pressure - "
        "extended parabolic moves like this tend to give back a large chunk of "
        "the move fast. Bias: SELL._"
    )
    return "\n".join(lines)


def format_meme_move_checkpoint_alert(pair: str, market: str, direction: str, checkpoint_pct: float,
                                       cumulative_pct: float, price: float, window_days: int) -> str:
    """
    jobs/meme_move_watcher.py - fires once a pair's trailing cumulative
    move crosses a new checkpoint level in one direction: 60% then
    every +20% on the way up (60/80/100/...), or -40% then every
    -10% further down (-40/-50/-60/...) - see that module's docstring
    for the exact stepping rule. Same "one clean push per checkpoint"
    idea as strong signals, just for a raw meme/alt coin move instead
    of a full indicator-confidence setup.
    """
    is_up = direction == "up"
    header_icon = "🚀" if is_up else "🔻"
    header_label = "MEME MOVE — PUMPING" if is_up else "MEME MOVE — DUMPING"
    bar_icon = "🟩" if is_up else "🟥"
    sign = "+" if is_up else ""

    lines = [
        f"{header_icon} *{header_label}* ({market.title()})",
        f"{_send_time_line()}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"{bar_icon} Checkpoint reached: *{sign}{checkpoint_pct:.0f}%*",
        f"Cumulative move ({window_days}d): *{sign}{cumulative_pct:.1f}%*",
        f"Price now: `{_fmt_price(price)}`",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if is_up:
        lines.append("_Still climbing - you'll get pinged again at every further +20% step, and separately if it snaps back hard off the top._")
    else:
        lines.append("_Still falling - you'll get pinged again at every further -10% step, and separately if it suddenly bounces off the bottom._")
    return "\n".join(lines)


def format_meme_move_reversal_alert(pair: str, market: str, reversal_type: str, cumulative_pct: float,
                                     extreme_price: float, current_price: float, move_pct: float) -> str:
    """
    jobs/meme_move_watcher.py - fires when a tracked meme/alt coin move
    snaps back the other way: reversal_type "pump_then_drop" (was up,
    now pulling back off its peak) or "dump_then_bounce" (was down,
    now bouncing off its trough). Always states the peak/trough it's
    measured against AND the live "before -> now" % so the size of the
    snap-back is unambiguous at a glance, per the explicit request for
    this one.
    """
    is_pump_drop = reversal_type == "pump_then_drop"
    header_icon = "⚠️🔻" if is_pump_drop else "⚠️🚀"
    header_label = "MEME MOVE — PUMP REVERSING DOWN" if is_pump_drop else "MEME MOVE — DUMP REVERSING UP"
    extreme_label = "Peak" if is_pump_drop else "Trough"
    move_label = "off peak" if is_pump_drop else "off trough"
    move_sign = "-" if is_pump_drop else "+"

    lines = [
        f"{header_icon} *{header_label}* ({market.title()})",
        f"{_send_time_line()}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"Cumulative move before this: *{'+' if is_pump_drop else ''}{cumulative_pct:.1f}%*",
        f"{extreme_label}: `{_fmt_price(extreme_price)}` → Now: `{_fmt_price(current_price)}` "
        f"(*{move_sign}{move_pct:.1f}%* {move_label})",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if is_pump_drop:
        lines.append("_A big move up is now giving some of it back fast - worth watching for further downside._")
    else:
        lines.append("_A big move down is now bouncing back fast - worth watching for further upside._")
    return "\n".join(lines)


def format_meme_move_4h_volume_alert(pair: str, market: str, direction: str, interval_volume: float,
                                      price_before: float, price_now: float, hours: float) -> str:
    """
    jobs/meme_move_watcher.py - fires when a pair's traded USDT volume
    over the trailing ~4h window itself passes a raw threshold (200M
    default), independent of the multi-day cumulative checkpoint/
    reversal tracking above. Tagged "up"/"down" by whether price rose
    or fell over that same window, since a huge amount of volume means
    something different depending on which way it pushed price.
    """
    is_up = direction == "up"
    icon = "🟢📊" if is_up else "🔴📊"
    return "\n".join([
        f"{icon} *MEME MOVE — {hours:.0f}H VOLUME SPIKE* ({market.title()})",
        f"{_send_time_line()}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"{hours:.0f}h traded volume: *${interval_volume:,.0f}*  ({'📈 UP' if is_up else '📉 DOWN'})",
        f"~{hours:.0f}h ago: `{_fmt_price(price_before)}` → Now: `{_fmt_price(price_now)}`",
        "━━━━━━━━━━━━━━━━━━━━",
        "_An unusually large amount of money moving through this pair in a single 4h window - worth a look._",
    ])


def format_meme_move_4h_price_alert(pair: str, market: str, move_pct: float, price_before: float,
                                     price_now: float, hours: float) -> str:
    """
    jobs/meme_move_watcher.py - the separate, simpler 4H price-move
    check: fires once a pair's price move over the trailing ~4h window
    itself passes a raw % threshold (65% default), in EITHER direction,
    independent of the multi-day cumulative checkpoint/reversal
    tracking above.
    """
    is_up = move_pct >= 0
    icon = "⚡🚀" if is_up else "⚡🔻"
    sign = "+" if is_up else ""
    return "\n".join([
        f"{icon} *MEME MOVE — {hours:.0f}H PRICE SPIKE* ({market.title()})",
        f"{_send_time_line()}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"{hours:.0f}h move: *{sign}{move_pct:.1f}%*",
        f"~{hours:.0f}h ago: `{_fmt_price(price_before)}` → Now: `{_fmt_price(price_now)}`",
        "━━━━━━━━━━━━━━━━━━━━",
        "_An unusually fast move for a single 4h window - typical of thin/low-liquidity meme coins. Trade size accordingly._",
    ])


def format_rsi_extreme_alert(pair: str, market: str, direction: str, level: float, rsi_value: float,
                              timeframe: str) -> str:
    """
    jobs/rsi_extreme_watcher.py - fires the instant RSI crosses into
    extreme territory (>=80, then every further +10 step: 80/90/100)
    or (<=25, then every further -5 step: 25/20/15), and again at each
    later checkpoint on the same pair. Explicitly calls out that the
    pair has ALSO been added to the High Alert Pair scan pool (per the
    request that this stay unambiguous - "no confusion" about what
    happens next), since that's the whole point of flagging it this
    early: it can turn any time from here.
    """
    is_high = direction == "high"
    icon = "🔺🚨" if is_high else "🔻🚨"
    header = "RSI EXTREME — OVERBOUGHT" if is_high else "RSI EXTREME — OVERSOLD"
    watch_for = "a SELL reversal" if is_high else "a BUY reversal"
    return "\n".join([
        f"{icon} *{header}* ({market.title()})",
        f"{_send_time_line()}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"RSI ({timeframe}) checkpoint: *{level:.0f}*  (now: *{rsi_value:.1f}*)",
        "━━━━━━━━━━━━━━━━━━━━",
        f"_Added to 🎯 High Alert Pair - the full indicator engine is now watching this pair specifically for {watch_for}. "
        f"You'll get pinged again at every further checkpoint ({'110/120/...' if is_high else '10/5/...'}) while it stays this extreme._",
    ])


def format_rsi_retest_alert(pair: str, market: str, direction: str, level: float, rsi_value: float,
                             timeframe: str) -> str:
    """
    jobs/rsi_extreme_watcher.py - fires ONCE for a pair that was
    genuinely RSI-extreme (>=85 or <=16, i.e. it already has a
    rsi_alert_state row) and has now pulled back through the classic
    70 (overbought side) or 30 (oversold side) line. Only ever fires
    for pairs that were extreme first - a pair casually crossing
    70/30 with no prior extreme reading never reaches this formatter
    at all, which is what keeps this side from spamming.
    """
    is_high = direction == "high"
    icon = "🔁🔺" if is_high else "🔁🔻"
    header = "RSI RETEST — 70 LINE" if is_high else "RSI RETEST — 30 LINE"
    context_line = (
        "Pulled back from overbought and just retested the *70* line from above."
        if is_high else
        "Bounced from oversold and just retested the *30* line from below."
    )
    return "\n".join([
        f"{icon} *{header}* ({market.title()})",
        f"{_send_time_line()}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair: `{pair}`",
        f"RSI ({timeframe}): *{rsi_value:.1f}*  (retest level: *{level:.0f}*)",
        "━━━━━━━━━━━━━━━━━━━━",
        f"_{context_line} This pair was flagged 🚨 RSI EXTREME first, so this is a confirmed retest - not a random 70/30 cross._",
    ])


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
                          wallet_balance: float | None = None, mm_cfg: dict | None = None,
                          trade_id: str | None = None, badge: str | None = None) -> str:
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

    `badge`, when given, is an extra line printed ABOVE the normal
    "Trade Signal #N" header - used by jobs/high_alert_watcher.py to
    mark a push as coming from the overextended-pair SELL scan
    specifically, while reusing this exact same full breakdown (every
    other field below is identical either way).

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

    lines = []
    if badge:
        lines.append(badge)
    lines += [
        f"*Trade Signal {serial_str}*".strip(),
        f"`{pair}`  —  24H: *{change_str}*",
    ]
    if trade_id:
        lines.append(f"Trade ID : `{trade_id}`")
    lines += [
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
                              mm_cfg: dict | None = None, trade_id: str | None = None,
                              live_preview: dict | None = None) -> str:
    """
    ONE "Signal Scan #N" block - factored out of format_final_signal_scan
    so the handler can log the exact text of each individual result
    (for the Search Signal Status button) while still sending them all
    joined together as one message, same as before.

    `trade_id` prints right under the header, right where the person
    needs it to note down/copy before it scrolls off - this is the
    number they'll later type into "Active a Trade" to start tracking
    this exact signal.

    `live_preview`, when given (engine.risk_manager.compute_live_trade_plan's
    output - FUTURES pairs only), appends a compact "what would
    actually happen if activated right now" block using the pair's
    real max leverage and the full saved Wallet Balance - separate
    from the risk-based Money Management block above it.
    """
    confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)
    verdict = result.get("verdict", "?")
    pair = result.get("symbol", "?")
    trade_id_line = f"Trade ID : `{trade_id}`\n" if trade_id else ""
    live_preview_block = f"\n━━━━━━━━━━━━━━━━━━━━\n{format_live_trade_preview(live_preview)}" if live_preview else ""
    return (
        f"📡 *Signal Scan #{index}*\n"
        f"{trade_id_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_verdict_emoji(verdict)} *{verdict}* — `{pair}`\n"
        f"Confidence: *{confidence:.0f}/100*\n"
        f"{_send_time_line()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_trade_plan_block(result, wallet_balance, mm_cfg)}"
        f"{live_preview_block}"
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


def format_full_analysis_block(index: int, result: dict, wallet_balance: float | None = None,
                                mm_cfg: dict | None = None) -> str:
    """
    "Full Analysis" mode's per-pair report - sent as its OWN message,
    one pair at a time, as each pair finishes scanning (not batched
    several-per-message) so nothing has to wait for a batch to fill up
    or get silently bundled with others. Same underlying data and
    visual language as format_single_pair_report (bulleted
    Indicator/Concept Performance, Futures Metrics when relevant, a
    full trade plan when tradeable) with a "#N" serial number added so
    a long Full Analysis run stays easy to follow pair by pair.
    """
    pair = result.get("symbol", "?")
    scope_label = SCOPE_LABELS.get(result.get("exchange"), result.get("exchange", "?"))
    change24h = result.get("change24h")
    change_arrow = ""
    if isinstance(change24h, (int, float)):
        change_arrow = "🔺" if change24h > 0 else ("🔻" if change24h < 0 else "▪️")
    change_str = f"{change_arrow} {change24h:+.2f}%" if isinstance(change24h, (int, float)) else "N/A"
    tradeable = result.get("tradeable", False)
    confidence = result.get("multiTimeframe", {}).get("combinedConfidence", 0)

    lines = [
        f"📄 *Full Analysis #{index}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pair name: `{pair}` ({scope_label})",
        f"24H %: {change_str}",
        "",
        "📊 *Indicator Performance*",
        _bullet_votes(result.get("indicatorVotes"), result.get("indicatorInfo", "N/A")),
        "",
        "🧩 *Concept Performance*",
        _bullet_votes(result.get("conceptVotes"), result.get("conceptInfo", "N/A")),
        "",
        "📈 *Order Flow Information*",
        f"  {result.get('orderFlowInfo', 'N/A')}",
    ]

    funding_info = result.get("fundingRateInfo")
    oi_info = result.get("openInterestInfo")
    if funding_info and "spot pair" not in funding_info:
        lines += ["", "💹 *Futures Metrics*", f"  • {funding_info}"]
        if oi_info:
            lines.append(f"  • {oi_info}")

    lines += ["", "━━━━━━━━━━━━━━━━━━━━"]
    if tradeable:
        verdict = result.get("verdict", "?")
        lines += [
            f"{_verdict_emoji(verdict)} *Executed trade: Yes*",
            f"Trading: *{verdict}*",
            f"Confidence level: *{confidence:.0f}/100*",
            f"{_send_time_line()}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{_trade_plan_block(result, wallet_balance, mm_cfg)}",
        ]
    else:
        lines += [
            "❌ *Executed trade: No*",
            f"Reason: _{result.get('reason', 'N/A')}_",
            f"Confidence level: *{confidence:.0f}/100*",
        ]

    return "\n".join(lines)


def format_single_pair_report(result: dict, wallet_balance: float | None = None, mm_cfg: dict | None = None,
                               trade_id: str | None = None) -> str:
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
        ]
        if trade_id:
            lines.append(f"Trade ID : `{trade_id}`")
        lines += [
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
    min_conf = status.get("minConfidenceToPush")

    lines += [
        "",
        "📋 *Scan Activity*",
        f"  • Full market scans run: *{w['success'] + w['failed']}* "
        f"(✅ {w['success']} completed, ❌ {w['failed']} failed)",
    ]
    if min_conf is not None:
        lines.append(
            f"  _A scan only sends a signal here if it finds a setup scoring "
            f"{min_conf:.0f}+/100 confidence - otherwise it finishes clean with nothing "
            f"to push, which is normal, not a malfunction._"
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


_HIGH_ALERT_SCOPE_LABELS = {"bitget-spot": "Spot", "bitget-futures": "Future"}


def format_high_alert_pairs(pool: dict) -> str:
    """
    "🚨 High Alert Pairs" button - see bot/handlers/high_alert_pairs.py
    module docstring for exactly what's in `pool` (scope -> list of
    candidate dicts, each already tagged with its source/verdict).
    """
    header = "🚨 *High Alert Pairs*"
    total = sum(len(v) for v in pool.values())

    if total == 0:
        return (
            f"{header}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Pool is empty right now - no pair is currently overextended "
            "(80%+ cumulative pump) or sitting at an RSI extreme. This "
            "updates live as strong_signal_watcher and rsi_extreme_watcher "
            "tick, no action needed._"
        )

    lines = [
        header,
        "━━━━━━━━━━━━━━━━━━━━",
        f"_{total} pair(s) currently in the pool - these are what the next "
        f"High Alert scan will check against the full engine._",
    ]

    for scope, candidates in pool.items():
        if not candidates:
            continue
        scope_label = _HIGH_ALERT_SCOPE_LABELS.get(scope, scope)
        lines.append("")
        lines.append(f"*{scope_label}* — {len(candidates)} pair(s)")
        for c in candidates:
            verdict_arrow = "🔻 SELL-watch" if c["expectedVerdict"] == "SELL" else "🔺 BUY-watch"
            if c["source"] == "pump":
                detail = f"Pump +{c['cumulativePct']:.1f}% cumulative — flagged {_ago(c['flaggedAt'])}"
            else:
                detail = f"RSI extreme on {c['timeframe']} — flagged {_ago(c['flaggedAt'])}"
            lines.append(f"  • `{c['symbol']}` — {verdict_arrow}\n    {detail}")

    lines.append("")
    lines.append(
        "_A pair here isn't a signal yet - it only gets pushed once the "
        "full engine confirms it and clears High Alert's own confidence bar._"
    )
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

# =========================================================================
# --- Trade Information/Active (Trade ID system) ---
# Replaces the old "Signal Outcomes" button. See bot/handlers/
# trade_information.py for the full flow. Every signal now carries a
# Trade ID; nothing is tracked until the person activates that ID here.
# =========================================================================

def _format_iso_send_time(iso: str) -> str:
    """Same visual format as _send_time_line() (BDT + UTC together), but for a STORED timestamp (when the signal was originally sent) instead of 'now'."""
    from datetime import datetime, timedelta
    dt_utc = datetime.fromisoformat(iso)
    dt_bdt = dt_utc + timedelta(hours=6)
    return (
        f"`{dt_bdt.strftime('%H:%M:%S')} BDT` | `{dt_utc.strftime('%H:%M:%S')} UTC` "
        f"| Date: `{dt_utc.strftime('%d-%m-%Y')}`"
    )


def format_trade_id_prompt() -> str:
    return "✅ *Active a Trade*\n\nSend the Trade ID you want to activate (the number shown on the signal itself)."


def format_trade_activated(trade: dict) -> str:
    """'ID : X Found' confirmation right after activation - reprints the original signal block (no Money Management, no repeated Trade ID line since the header already states it)."""
    confidence = trade.get("confidence")
    confidence_str = f"{confidence:.0f}/100" if confidence is not None else "N/A"
    scan_label = trade.get("scanLabel") or "Signal"
    pair = trade["symbol"]
    verdict = trade["verdict"]
    return (
        f"ID : `{trade['tradeId']}` Found\n\n"
        f"📡 *{scan_label}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_verdict_emoji(verdict)} *{verdict}* — `{pair}`\n"
        f"Confidence: *{confidence_str}*\n"
        f"Send Time: {_format_iso_send_time(trade['openedAt'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry: `{_fmt_price(trade.get('entry'))}`\n"
        f"SL: `{_fmt_price(trade.get('stopLoss'))}`\n"
        f"TP1: `{_fmt_price(trade.get('tp1'))}`\n"
        f"TP2: `{_fmt_price(trade.get('tp2'))}`\n"
        f"TP3: `{_fmt_price(trade.get('tp3'))}`"
    )


def format_active_balance_prompt() -> str:
    return (
        "This trade is now being tracked (Entry/SL/TP notifications will come automatically).\n\n"
        "💰 Want to also *Active Balance* for it? That locks your current Wallet Balance as margin "
        "for this trade right now, at this pair's real max leverage, and tracks live P/L like a real trade."
    )


def format_active_balance_confirmed(trade: dict, plan: dict, new_balance: float) -> str:
    return (
        f"✅ *Active Balance set successful*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair          : `{trade['symbol']}` ({trade['verdict']})\n"
        f"Trade ID      : `{trade['tradeId']}`\n"
        f"Leverage      : `{plan['leverage']}x`\n"
        f"Margin Locked : `{plan['balance']:.2f} USDT`\n"
        f"Position Size : `{plan['positionNotional']:.2f} USDT`  (`{plan['quantity']:.6f}` units)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Wallet Balance now: *`{new_balance:.2f} USDT`*\n\n"
        f"_TP/SL notifications for this trade will now include real P/L on this position._"
    )


def format_active_balance_error(reason: str) -> str:
    return f"⚠️ Couldn't *Active Balance*: {reason}\n\nThis trade is still being tracked normally, just without the balance/leverage numbers."


def format_active_balance_spot_unsupported() -> str:
    return format_active_balance_error("this is a Spot pair - leverage-based Active Balance is Futures-only for now.")


def format_trade_already_active(trade: dict) -> str:
    return f"ID : `{trade['tradeId']}` — already active, already being tracked for `{trade['symbol']}`."


def format_trade_id_not_found(trade_id: str) -> str:
    return f"ID : `{trade_id}` — not found. Double-check the number and try again."


def format_trade_id_bad_input() -> str:
    return "That doesn't look like a Trade ID. Please send just the number, e.g. `984323987`."


def format_trade_information_summary(stats: dict) -> str:
    return (
        f"📈 *Trade Information*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Active Trade — *{stats['totalActive']}*\n"
        f"Touch Entry — *{stats['touchEntry']}*\n"
        f"Touch SL — *{stats['touchSl']}*\n"
        f"Touch TP-1 — *{stats['touchTp1']}*\n"
        f"Touch TP-2 — *{stats['touchTp2']}*\n"
        f"Touch TP-3 — *{stats['touchTp3']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Tp-1 revers Sl — *{stats['tp1RefersSl']}*\n"
        f"Tp-2 revers Sl — *{stats['tp2RefersSl']}*\n"
        f"Tp-3 revers Sl — *{stats['tp3RefersSl']}*"
    )


def _trade_overall_status_label(trade: dict) -> str:
    if trade["entryStatus"] != "arrived":
        return "⏳ Waiting Entry"
    if trade["status"] == "sl_hit":
        if trade["highestTpHit"] > 0:
            return f"📉 Down (after TP{trade['highestTpHit']})"
        return "🛑 Closed (Stop Loss)"
    if trade["status"] == "tp3_hit":
        return "🎯 Closed (Full Target)"
    return "🟢 Active — In Trade"


def _level_line(label: str, price, touched: bool) -> str | None:
    if price is None:
        return None
    mark = "✅ Touched" if touched else "⏳ Not yet"
    return f"{label}: `{_fmt_price(price)}` — {mark}"


def format_trade_detail_block(trade: dict, live_price: float | None = None) -> str:
    """
    One trade's full status card - used by both 'See Last 12 Trade'
    (joined several together) and 'See Active Trade By ID' (one alone).
    Per-level lines show whether that level's been touched; the
    "pair health" line at the bottom is the trade's overall status
    (Waiting Entry / Active / Down / Closed).

    `live_price`, when given for a "List with Balance" trade whose
    entry has already arrived, adds a live/floating P/L line - the
    "like a real trade, show current profit if market goes up" view.
    """
    confidence = trade.get("confidence")
    confidence_str = f"{confidence:.0f}/100" if confidence is not None else "N/A"
    scan_label = trade.get("scanLabel") or "Signal"
    pair = trade["symbol"]
    verdict = trade["verdict"]
    entry_arrived = trade["entryStatus"] == "arrived"
    highest = trade["highestTpHit"]
    sl_touched = trade["status"] == "sl_hit"

    lines = [
        f"📡 *{scan_label}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{_verdict_emoji(verdict)} *{verdict}* — `{pair}`",
        f"Confidence: *{confidence_str}*",
        f"Send Time: {_format_iso_send_time(trade['openedAt'])}",
        "━━━━━━━━━━━━━━━━━━━━",
        _level_line("Entry", trade.get("entry"), entry_arrived),
        _level_line("SL", trade.get("stopLoss"), sl_touched),
        _level_line("TP1", trade.get("tp1"), highest >= 1),
        _level_line("TP2", trade.get("tp2"), highest >= 2),
        _level_line("TP3", trade.get("tp3"), highest >= 3),
        "--------------------------------------------------------------",
        f"Pair health: {_trade_overall_status_label(trade)}",
        f"Trade ID : `{trade['tradeId']}`",
    ]

    if trade.get("balanceMode") == "list_with_balance":
        lines.append(f"Leverage: `{trade['leverageUsed']}x`  |  Margin Locked: `{trade['marginLocked']:.2f} USDT`")
        if entry_arrived and live_price is not None:
            from engine.risk_manager import pnl_at_price
            usdt, pct = pnl_at_price(trade["positionNotional"], trade["entry"], trade["marginLocked"], verdict, live_price)
            if usdt is not None:
                sign = "+" if usdt >= 0 else ""
                lines.append(f"Live P/L (at `{_fmt_price(live_price)}`): `{sign}{usdt:.2f} USDT` ({sign}{pct:.1f}%)")

    return "\n".join(line for line in lines if line is not None)


def format_last_trades_list(trades: list) -> str:
    if not trades:
        return "No activated trades yet - use *Active a Trade* first."
    blocks = [format_trade_detail_block(t) for t in trades]
    return "\n\n".join(blocks)


def format_trade_by_id_prompt() -> str:
    return "🔍 *See Active Trade By ID*\n\nSend the Trade ID you want to check."


def format_trade_by_id_not_found(trade_id: str) -> str:
    return (
        f"ID : `{trade_id}` — not found among your last 12 activated trades. "
        f"It may not have been activated, or it's already rolled off the list."
    )


def format_remove_trade_prompt() -> str:
    return "🗑 *Remove Trade*\n\nSend the Trade ID you want to remove."


def format_trade_removed(trade_id: str) -> str:
    return f"ID : `{trade_id}` — removed. It's stopped being tracked and won't appear in Trade Information anymore."


def format_trade_remove_not_found(trade_id: str) -> str:
    return f"ID : `{trade_id}` — not found among your last 12 activated trades, nothing removed."
