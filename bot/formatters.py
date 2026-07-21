"""
bot/formatters.py

Turns raw signal-scanner / watcher output into the Markdown messages
the bot actually sends. Kept separate from handlers/jobs so wording
can be tweaked in one place without touching any scanning or
scheduling logic.
"""


def format_volume_spike_alert(pair: str, last_price: float, pct_change: float, direction: str, market: str) -> str:
    """
    Phase 2.1 - pushed by jobs/volume_spike_watcher.py whenever a pair
    suddenly moves past its threshold (BTC uses its own tighter one -
    see config/settings.yaml's volume_spike_watch section for both).
    `direction` is "up" or "down".
    """
    arrow = "🔺" if direction == "up" else "🔻"
    return (
        f"{arrow} *Sudden Move Detected* ({market.title()})\n\n"
        f"Pair: `{pair}`\n"
        f"Last Price: `{last_price}`\n"
        f"Move: *{pct_change:+.2f}%* {direction.upper()}"
    )


def _trade_plan_block(result: dict) -> str:
    plan = result.get("tradePlan") or {}
    return (
        f"Entry: `{plan.get('entry')}`\n"
        f"SL: `{plan.get('stopLoss')}`\n"
        f"TP1: `{plan.get('tp1')}`\n"
        f"TP2: `{plan.get('tp2')}`\n"
        f"TP3: `{plan.get('tp3')}`"
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
        f"🔥 *Strong Signal* — {verdict}\n\n"
        f"Pair: `{pair}`\n"
        f"Confidence: *{confidence:.0f}/100*\n\n"
        f"{_trade_plan_block(result)}"
    )


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
            f"📡 *Signal Scan #{i}* — {verdict}\n"
            f"Pair: `{pair}`\n"
            f"Confidence: *{confidence:.0f}/100*\n\n"
            f"{_trade_plan_block(result)}"
        )
    return "\n\n".join(blocks)