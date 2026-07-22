"""
bot/handlers/help.py

The ℹ️ Help button - a plain-text breakdown of what every main-menu
button actually does, in the same order they appear on the keyboard.
Numbers quoted here (thresholds, intervals, cooldowns) are pulled live
from config/settings.yaml rather than hardcoded, so this stays accurate
if those get tuned later instead of silently going stale.
"""
from telegram import Update
from telegram.ext import ContextTypes


def _build_help_text(settings: dict) -> str:
    vsw = settings.get("volume_spike_watch", {})
    ssw = settings.get("strong_signal_watch", {})
    ss = settings.get("search_signal", {})

    poll = vsw.get("poll_interval_seconds", 15)
    spike_pct = vsw.get("spike_pct_threshold", 20.0)
    btc_pct = vsw.get("btc_spike_pct_threshold", 5.0)
    vsw_cooldown = vsw.get("cooldown_seconds", 900) // 60

    scan_interval = ssw.get("scan_interval_seconds", 900) // 60
    min_conf = ssw.get("min_confidence_to_push", 80)
    ssw_cooldown = ssw.get("cooldown_seconds", 3600) // 60

    top_n = ss.get("top_n", 3)

    return (
        "ℹ️ *Help — What Every Button Does*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "📊 *24/7 Market Analyse*\n"
        "Turns on continuous background monitoring for every pair in "
        "the market you pick (Spot / Future / Both). Every "
        f"~{poll}s it checks each pair's price and pushes an alert the "
        f"moment one moves *{spike_pct:.0f}%+* in either direction "
        f"(BTC uses a tighter *{btc_pct:.0f}%+* threshold, since a "
        "20-40% BTC move essentially never happens). The same pair/"
        f"direction won't alert again for {vsw_cooldown} min after an "
        "alert, so you don't get spammed while a move is still playing "
        "out.\n\n"

        "🔕 *24/7 Off Market Analyse*\n"
        "Turns the above off for this chat.\n\n"

        "📈 *24/7 Market Analyse — Status*\n"
        "Shows whether it's currently on, which market it's watching, "
        "how long it's been running, and your most recent alerts.\n\n"

        "🔥 *Find 24/7 Strong Signal*\n"
        "Turns on a continuous background scanner over every pair in "
        "the market you pick. It re-scans the whole market roughly "
        f"every {scan_interval} min — full indicators, trading "
        "concepts, order flow, and news for every pair — and the "
        f"instant a setup's confidence reaches *{min_conf}+/100* it "
        "pushes you the full trade plan (Entry / SL / TP1-3) "
        "unprompted. The same pair+verdict won't push again for "
        f"{ssw_cooldown} min once you've been told about it.\n\n"

        "🛑 *Off 24/7 Find Signal*\n"
        "Turns the above off for this chat.\n\n"

        "📈 *Find 24/7 Strong Signal — Status*\n"
        "Shows on/off state, how many scans have run, how many "
        "signals have been found, and your last 12 signals.\n\n"

        "🔎 *Search Signal*\n"
        "A one-shot scan you trigger manually instead of waiting for "
        "the 24/7 scanner: pick Spot / Future / Both, optionally see "
        "full per-pair detail as it scans, and get back the top "
        f"{top_n} setups right now, ranked by confidence. This mode "
        "runs once and then switches itself off automatically - it "
        "doesn't keep running in the background.\n\n"

        "🎯 *Single Pair Analyse*\n"
        "Type any one pair (e.g. `BTC/USDT` or `BTCUSDT`) and get its "
        "full report immediately - indicators, concepts, order flow, "
        "and a trade plan if it's currently tradeable.\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "_Tip: you can have 24/7 Market Analyse AND Find Strong Signal "
        "both on at the same time - they watch for different things "
        "(sudden moves vs. high-confidence setups) and don't conflict._"
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data.get("settings", {})
    await update.message.reply_text(_build_help_text(settings), parse_mode="Markdown")