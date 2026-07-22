# signal_scanner.py
# Phase 4.3 - "Start Signal" full-market scanner. Runs the SAME
# indicator (4.1) + trading-concept (4.2) + order-flow + signal_engine
# pipeline already proven on the Order Flow page's Signal panel
# (/api/order-flow-signal) across EVERY pair in a chosen market
# (Spot or Futures), on EVERY one of the 6 required timeframes, then
# folds in live order-flow tape and matching news headlines, decides
# which pairs are genuinely tradeable right now, and ranks the winners
# into a small shortlist of concrete trade plans (Entry/SL/TP1-3, not
# a fixed R:R - built from real support/resistance + ATR).
#
# HONEST NOTE ON SCALE: a full scan is genuinely a multi-minute
# operation. Each pair needs 6 candle fetches (one per required
# timeframe) + 2 order-flow calls (trades, order book) = 8 Bitget API
# calls; across 700+ pairs that's thousands of calls. This runs as a
# background job (same job-id + polling pattern already used elsewhere
# in this project) with a worker pool, and the frontend shows live
# progress rather than blocking - there's no way to make a genuinely
# thorough 700-pair x 6-timeframe x order-flow scan instant, and
# pretending otherwise would mean quietly cutting corners on the depth
# of analysis that was explicitly asked for.

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from engine.exchange_adapter import EXCHANGE_ADAPTERS
from engine.bitget_api import get_token_list
from engine.order_flow import get_order_flow
from engine.news_service import get_news_feed
from engine.indicators import compute_all_indicators
from engine.trading_concepts.analysis import compute_all_concepts
from engine.trading_concepts.support_resistance import analyze_support_resistance
from engine.indicators.atr import compute_atr
from engine.signal_engine import compute_overall_signal, WEIGHTS, INDICATOR_VOTE_KEYS, CONCEPT_VOTE_KEYS

log = logging.getLogger("crypto-analyzer-http")

# Per the Phase 4.3 spec: "The analysis should only use these
# timeframes... No other timeframes should be analyzed." 4H is called
# out as highest priority, which is reflected in TIMEFRAME_WEIGHTS below
# (used to blend each timeframe's individual signal into one combined
# multi-timeframe score) and in which timeframe's candles are used for
# the structure-based SL/TP calculation (see PRIMARY_TIMEFRAME_ORDER).
SCAN_TIMEFRAMES = ["1w", "1d", "4h", "1h", "30m", "15m"]
TIMEFRAME_WEIGHTS = {"1w": 0.7, "1d": 1.0, "4h": 1.6, "1h": 1.2, "30m": 0.9, "15m": 0.7}
# Preference order for which timeframe's candles to build the actual
# trade plan (support/resistance levels, ATR) from - 4h first since
# it's the spec's highest-priority timeframe, falling back to the next
# one actually available for this pair if 4h's candle fetch happened to
# come back too thin.
PRIMARY_TIMEFRAME_ORDER = ["4h", "1d", "1h", "1w", "30m", "15m"]

CANDLE_LIMIT_PER_TIMEFRAME = 300
ORDER_FLOW_TRADE_DEPTH = 500  # stays within Bitget's quick (non-paginated) endpoint - 1 call, not several

# --- "Tradeable yes/no" gate thresholds - starting points, not tuned on
# real trading results. All named here so they're easy to find and
# adjust rather than buried as magic numbers through the scan logic. ---
MIN_USDT_VOLUME_24H = 500_000     # liquidity gate ("Highest Priority" check per the spec) - below this,
                                   # a technically perfect setup still isn't safely tradeable (slippage risk)
MIN_TIMEFRAMES_WITH_DATA = 3      # need at least half the 6 timeframes to have usable candle data at all
MIN_TIMEFRAME_AGREEMENT = 4       # of the timeframes that DID compute, at least this many must agree on
                                   # direction - stops a single outlier timeframe from driving the call
MIN_COMBINED_CONFIDENCE = 40      # signal_engine's 0-100 confidence, floor for "worth surfacing at all"
BUY_THRESHOLD = 20                # same threshold signal_engine.py uses for one-timeframe verdicts
SELL_THRESHOLD = -20

ATR_SL_BUFFER_MULT = 0.5   # stop sits this many ATRs beyond the nearest support/resistance, not exactly on it
ATR_EXTRAPOLATED_TARGET_MULT = 1.5  # when fewer than 3 real S/R levels exist, extrapolate further targets by this many ATRs


def compute_structure_trade_plan(candles, direction):
    """
    Builds Entry/SL/TP1-3 from REAL market structure (support/resistance
    clusters + ATR) instead of a fixed risk:reward ratio - per the
    explicit request that this should read like a human-made plan, not
    a mechanical 1:2/1:3 formula. The resulting risk:reward numbers are
    whatever the actual structure produces (sometimes 1:1.4, sometimes
    1:5) - that variability is intentional, not a bug.

    Returns None if there isn't enough structure to build a plan from
    yet (too little swing history) - callers should treat that pair as
    "not enough structure to trade" rather than falling back to a fake
    generic plan.
    """
    sr = analyze_support_resistance(candles)
    if not sr or not sr.get("support") or not sr.get("resistance"):
        return None

    current_price = candles[-1]["close"]
    atr = compute_atr(candles) or (current_price * 0.01)  # 1% fallback only if ATR itself has too little history

    supports = sr["support"]
    resistances = sr["resistance"]

    if direction == "BUY":
        stop_loss = supports[0]["price"] - atr * ATR_SL_BUFFER_MULT
        liquidity_zone = supports[0]["price"]
        targets = [r["price"] for r in resistances[:3]]
        while len(targets) < 3:
            base = targets[-1] if targets else current_price
            targets.append(base + atr * ATR_EXTRAPOLATED_TARGET_MULT)
    elif direction == "SELL":
        stop_loss = resistances[0]["price"] + atr * ATR_SL_BUFFER_MULT
        liquidity_zone = resistances[0]["price"]
        targets = [s["price"] for s in supports[:3]]
        while len(targets) < 3:
            base = targets[-1] if targets else current_price
            targets.append(base - atr * ATR_EXTRAPOLATED_TARGET_MULT)
    else:
        return None

    entry = current_price
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return None

    return {
        "entry": round(entry, 8),
        "stopLoss": round(stop_loss, 8),
        "tp1": round(targets[0], 8),
        "tp2": round(targets[1], 8),
        "tp3": round(targets[2], 8),
        "riskReward": [round(abs(t - entry) / risk, 2) for t in targets],
        "liquidityZone": round(liquidity_zone, 8),
        "atr": round(atr, 8),
    }


def analyze_pair_multi_timeframe(raw_symbol, exchange, enabled_indicators, enabled_concepts):
    """
    Fetches candles and runs indicators+concepts+signal_engine
    independently across every SCAN_TIMEFRAMES entry this exchange
    actually supports, then blends the per-timeframe scores into one
    weighted combined score (see TIMEFRAME_WEIGHTS - 4h counts most).

    Returns None if fewer than MIN_TIMEFRAMES_WITH_DATA timeframes had
    enough candle history to even compute a signal (e.g. a very
    recently-listed pair) - not enough basis for a multi-timeframe call.
    """
    adapter = EXCHANGE_ADAPTERS[exchange]
    per_tf = {}
    candles_by_tf = {}

    for tf in SCAN_TIMEFRAMES:
        if tf not in adapter["granularities"]:
            continue
        candles = adapter["fetch"](raw_symbol, tf, CANDLE_LIMIT_PER_TIMEFRAME)
        if not candles or len(candles) < 20:
            continue
        indicators = compute_all_indicators(candles, enabled=enabled_indicators)
        concepts = compute_all_concepts(candles, enabled=enabled_concepts)
        signal = compute_overall_signal(indicators, concepts, None, last_close=candles[-1]["close"])
        per_tf[tf] = {
            "score": signal["score"], "confidence": signal["confidence"],
            "verdict": signal["verdict"], "voteCount": signal["voteCount"],
            "votes": signal["votes"],
        }
        candles_by_tf[tf] = candles

    if len(per_tf) < MIN_TIMEFRAMES_WITH_DATA:
        return None

    total_weight = sum(TIMEFRAME_WEIGHTS[tf] for tf in per_tf)
    combined_score = sum(TIMEFRAME_WEIGHTS[tf] * per_tf[tf]["score"] for tf in per_tf) / total_weight
    combined_confidence = sum(TIMEFRAME_WEIGHTS[tf] * per_tf[tf]["confidence"] for tf in per_tf) / total_weight
    agreement = sum(
        1 for tf in per_tf
        if per_tf[tf]["score"] != 0 and (per_tf[tf]["score"] > 0) == (combined_score > 0)
    )

    primary_tf = next((tf for tf in PRIMARY_TIMEFRAME_ORDER if tf in candles_by_tf), None)

    return {
        "perTimeframe": per_tf,
        "combinedScore": round(combined_score, 1),
        "combinedConfidence": round(combined_confidence, 1),
        "agreementCount": agreement,
        "timeframesAnalyzed": len(per_tf),
        "primaryTimeframe": primary_tf,
        "primaryCandles": candles_by_tf.get(primary_tf) if primary_tf else None,
    }


def fold_in_order_flow(combined_score, combined_confidence, order_flow_live):
    """
    Blends the live trade-tape buy/sell split into the multi-timeframe
    score using the SAME weight already tuned for this in
    core/signal_engine.py (WEIGHTS["orderFlow"]) rather than inventing a
    new arbitrary number - keeps this consistent with how the Order Flow
    page's own Signal panel already treats live tape data.
    """
    if not order_flow_live or order_flow_live.get("buyPct") is None:
        return combined_score, combined_confidence, False

    buy_pct = order_flow_live["buyPct"]
    if abs(buy_pct - 50) < 2:
        return combined_score, combined_confidence, False  # too close to 50/50 to have an opinion

    flow_direction_score = max(-1.0, min(1.0, (buy_pct - 50) / 25)) * 100
    flow_weight = WEIGHTS.get("orderFlow", 1.3)
    base_weight = 6.0  # treats the whole 6-timeframe blend as roughly weight-6 versus this one order-flow vote

    new_score = (combined_score * base_weight + flow_direction_score * flow_weight) / (base_weight + flow_weight)
    # Confidence nudges up slightly when order flow AGREES with the
    # timeframe blend (extra confirmation), down slightly when it
    # disagrees (conflicting signal) - capped to a modest +/-10 either way
    # so one live tape reading can't single-handedly flip a verdict.
    agrees = (flow_direction_score > 0) == (combined_score > 0)
    new_confidence = max(0, min(100, combined_confidence + (5 if agrees else -10)))

    return round(new_score, 1), round(new_confidence, 1), True


def check_news_relevance(base_asset, news_items):
    """
    Simple substring match against each cached news item's already-
    classified currency_or_coin field (see news_service.py) - cheap,
    in-memory, no extra network calls per pair. Returns the single most
    recent matching item (or None), not every match, since the scanner
    output only needs "is there relevant news and what does it say",
    not a full duplicate news feed per pair.
    """
    if not base_asset:
        return None
    matches = [n for n in news_items if n.get("currency_or_coin", "").upper() == base_asset.upper()]
    if not matches:
        return None
    matches.sort(key=lambda n: n.get("published_at") or 0, reverse=True)
    top = matches[0]
    return {
        "headline": top["headline"], "sentiment": top.get("sentiment", "unknown"),
        "impact": top.get("impact", "unknown"), "source": top.get("source", ""),
        "publishedAt": top.get("published_at"),
    }


def guess_base_asset(raw_symbol):
    """'BTCUSDT' -> 'BTC'. Bitget's USDT-margined pairs are consistently
    <BASE>USDT, so stripping the fixed USDT suffix is reliable here -
    this project only tracks USDT-margined pairs."""
    return raw_symbol[:-4] if raw_symbol.upper().endswith("USDT") else raw_symbol


def build_explanation(mtf_result, order_flow_folded_in, news_item, verdict):
    """
    Template-built, honest explanation from the ACTUAL votes/data that
    drove the verdict - no ANTHROPIC_API_KEY required, and never
    presented as more than what it is (see the same honesty pattern
    used in news_service.py's keyword-fallback classifier). Every
    sentence here traces back to a real number already computed, not an
    invented narrative.
    """
    parts = []
    parts.append(
        f"{mtf_result['agreementCount']} of {mtf_result['timeframesAnalyzed']} analyzed timeframes agree on "
        f"{verdict.lower()}, combined score {mtf_result['combinedScore']:+.1f} "
        f"(confidence {mtf_result['combinedConfidence']:.0f}/100)."
    )
    primary = mtf_result.get("primaryTimeframe")
    if primary and primary in mtf_result["perTimeframe"]:
        pv = mtf_result["perTimeframe"][primary]
        parts.append(f"Highest-priority {primary} timeframe: {pv['verdict']} (score {pv['score']:+.1f}, {pv['voteCount']} indicators/concepts voting).")
    if order_flow_folded_in:
        parts.append("Live order-flow tape was factored in and adjusted the confidence.")
    if news_item:
        parts.append(f"Recent news ({news_item['sentiment']}, {news_item['impact']} impact): \"{news_item['headline']}\".")
    return " ".join(parts)


def summarize_votes(votes):
    """
    Splits a timeframe's raw vote list (signal["votes"]) into short,
    human-readable "indicator info" / "concept info" one-liners for the
    bot's Search Signal full-analysis mode - e.g.
    "RSI 28.4 (oversold) bullish, MACD histogram +0.0012 bullish".
    Returns (indicator_text, concept_text); either is "No clear signal"
    if that family had no opinion on this timeframe.
    """
    def _fmt(vs):
        if not vs:
            return "No clear signal"
        return ", ".join(f"{v['note']} ({'bullish' if v['direction'] > 0 else 'bearish'})" for v in vs)

    indicator_votes = [v for v in votes if v["key"] in INDICATOR_VOTE_KEYS]
    concept_votes = [v for v in votes if v["key"] in CONCEPT_VOTE_KEYS]
    return _fmt(indicator_votes), _fmt(concept_votes)


def analyze_one_pair(raw_symbol, display_symbol, exchange, usdt_volume_24h, enabled_indicators, enabled_concepts, news_items, change_24h=None):
    """
    Full per-pair pipeline: liquidity gate -> multi-timeframe signal ->
    order flow -> news -> tradeable decision -> (if tradeable)
    structure-based trade plan. Always returns a result dict (never
    raises) so one pair's failure can't take down the whole scan -
    failures are recorded in the result instead.
    """
    result = {
        "symbol": display_symbol, "rawSymbol": raw_symbol, "exchange": exchange,
        "usdtVolume24h": usdt_volume_24h, "change24h": change_24h,
        "tradeable": False, "reason": None,
        "indicatorInfo": "Not enough data", "conceptInfo": "Not enough data",
    }

    if usdt_volume_24h < MIN_USDT_VOLUME_24H:
        result["reason"] = f"24h volume ${usdt_volume_24h:,.0f} below liquidity floor (${MIN_USDT_VOLUME_24H:,.0f})"
        return result

    try:
        mtf = analyze_pair_multi_timeframe(raw_symbol, exchange, enabled_indicators, enabled_concepts)
    except Exception as exc:
        log.error(f"Signal scan: multi-timeframe analysis failed for {raw_symbol}: {exc}")
        result["reason"] = "Analysis failed"
        return result

    if mtf is None:
        result["reason"] = "Not enough candle history on enough timeframes yet"
        return result

    primary_tf = mtf.get("primaryTimeframe")
    primary_votes = mtf["perTimeframe"].get(primary_tf, {}).get("votes", []) if primary_tf else []
    result["indicatorInfo"], result["conceptInfo"] = summarize_votes(primary_votes)

    order_flow_live = None
    try:
        of = get_order_flow(raw_symbol, exchange, depth=ORDER_FLOW_TRADE_DEPTH)
        if of:
            order_flow_live = of.get("live")
    except Exception as exc:
        log.error(f"Signal scan: order flow failed for {raw_symbol}: {exc}")

    score, confidence, flow_folded_in = fold_in_order_flow(mtf["combinedScore"], mtf["combinedConfidence"], order_flow_live)

    base_asset = guess_base_asset(raw_symbol)
    news_item = check_news_relevance(base_asset, news_items)
    # A HIGH-impact news item pulling the opposite direction is treated
    # as a real risk factor - it doesn't override the technical verdict
    # outright (technicals already priced in a lot), but it meaningfully
    # dents confidence rather than being silently ignored.
    if news_item and news_item["impact"] == "high":
        news_bullish = news_item["sentiment"] == "bullish"
        tech_bullish = score > 0
        if news_bullish != tech_bullish and news_item["sentiment"] != "neutral":
            confidence = max(0, confidence - 15)

    result["multiTimeframe"] = {
        "perTimeframe": mtf["perTimeframe"], "combinedScore": score, "combinedConfidence": confidence,
        "agreementCount": mtf["agreementCount"], "timeframesAnalyzed": mtf["timeframesAnalyzed"],
        "primaryTimeframe": mtf["primaryTimeframe"], "orderFlowFoldedIn": flow_folded_in,
    }
    result["orderFlow"] = order_flow_live
    if order_flow_live and order_flow_live.get("buyPct") is not None:
        result["orderFlowInfo"] = f"{order_flow_live['buyPct']:.1f}% buy-side tape"
    else:
        result["orderFlowInfo"] = "No live tape data"
    result["newsImpact"] = news_item

    if mtf["agreementCount"] < MIN_TIMEFRAME_AGREEMENT:
        result["reason"] = f"Only {mtf['agreementCount']}/{mtf['timeframesAnalyzed']} timeframes agree (need {MIN_TIMEFRAME_AGREEMENT}+)"
        return result
    if confidence < MIN_COMBINED_CONFIDENCE:
        result["reason"] = f"Confidence {confidence:.0f} below floor ({MIN_COMBINED_CONFIDENCE})"
        return result
    if score >= BUY_THRESHOLD:
        verdict = "BUY"
    elif score <= SELL_THRESHOLD:
        verdict = "SELL"
    else:
        result["reason"] = f"Combined score {score:+.1f} too close to neutral"
        return result

    if not mtf["primaryCandles"]:
        result["reason"] = "No usable candles for trade-plan timeframe"
        return result

    trade_plan = compute_structure_trade_plan(mtf["primaryCandles"], verdict)
    if trade_plan is None:
        result["reason"] = "Not enough support/resistance structure to build a trade plan"
        return result

    result["tradeable"] = True
    result["verdict"] = verdict
    result["tradePlan"] = trade_plan
    result["explanation"] = build_explanation(mtf, flow_folded_in, news_item, verdict)
    # Ranking key: conviction (score magnitude) weighted by confidence -
    # used later to pick the top 1-3 out of everything marked tradeable.
    result["rankScore"] = round(abs(score) * (confidence / 100), 1)
    return result


def run_full_scan(job, scope, enabled_indicators, enabled_concepts, worker_count=8,
                   on_pair=None, on_progress=None):
    """
    Runs in a background thread (see http_server.py's
    /api/signal-scan/start) - scans every pair in `scope`
    ("bitget-spot" | "bitget-futures"), highest 24h% first down to the
    biggest losers, updating `job` in place as results stream in so the
    frontend's progress poll always has the latest partial state.

    `on_pair` (optional): called with each individual result dict the
    moment it's ready - used by the bot's Search Signal "Full Analysis"
    mode to stream per-pair detail messages as the scan runs, instead
    of waiting for everything to finish.
    `on_progress` (optional): called with (completed_count, total_count)
    after every single pair finishes - used to drive the bot's
    10/25/50/75/100% progress messages. Both callbacks run on THIS
    worker thread (not the caller's event loop) - callers that need to
    touch asyncio/Telegram from them are responsible for hopping back
    onto their own loop (e.g. via asyncio.run_coroutine_threadsafe).
    Any exception raised by either callback is caught and logged here
    so a bot-side formatting/sending bug can never abort the scan
    itself - the two concerns stay isolated from each other.
    """
    tokens = get_token_list(scope)["tokens"]
    # Highest 24h% first, down to the biggest losers - per the explicit
    # requested scan order. Purely affects the order pairs are processed
    # (and therefore the order they appear in the live progress feed),
    # not the final ranking (that's decided by rankScore after everything
    # finishes).
    tokens = sorted(tokens, key=lambda t: float(t.get("change24h") or 0), reverse=True)

    job["total"] = len(tokens)

    try:
        news_items = get_news_feed(limit=100)
    except Exception as exc:
        log.error(f"Signal scan: news feed fetch failed, continuing without news context: {exc}")
        news_items = []

    def work(token):
        if job["cancelled"]:
            return None
        return analyze_one_pair(
            token["rawSymbol"], token["symbol"], scope,
            token.get("usdtVolume24h", 0), enabled_indicators, enabled_concepts, news_items,
            change_24h=token.get("change24h"),
        )

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(work, t): t for t in tokens}
        for future in as_completed(futures):
            if job["cancelled"]:
                break
            token = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                log.error(f"Signal scan: worker failed for {token['rawSymbol']}: {exc}")
                result = {
                    "symbol": token["symbol"], "rawSymbol": token["rawSymbol"], "exchange": scope,
                    "tradeable": False, "reason": "Unexpected error during analysis",
                }
            if result is None:
                continue
            job["results"][result["rawSymbol"]] = result
            job["completed"] += 1

            if on_pair is not None:
                try:
                    on_pair(result)
                except Exception as exc:
                    log.error(f"Signal scan: on_pair callback failed for {result.get('rawSymbol')}: {exc}")
            if on_progress is not None:
                try:
                    on_progress(job["completed"], job["total"])
                except Exception as exc:
                    log.error(f"Signal scan: on_progress callback failed: {exc}")

    tradeable = [r for r in job["results"].values() if r.get("tradeable")]
    tradeable.sort(key=lambda r: r.get("rankScore", 0), reverse=True)
    job["topPicks"] = [r["rawSymbol"] for r in tradeable[:3]]

    job["status"] = "cancelled" if job["cancelled"] else "done"
    job["finishedAt"] = time.time()

# =========================================================================
# --- Telegram bot convenience wrappers (crypto-analyzer -> bot upgrade) ---
# The web UI drove run_full_scan() through a persisted `job` dict that a
# frontend polled for progress. The bot doesn't poll - handlers/jobs just
# want a plain function call that returns when the scan is done. These
# wrappers build a throwaway job dict internally, run the exact same
# scan logic above (unchanged), and hand back a simple result. They also
# add the "Spot / Future / Both" choice the bot menus expose (2.1/2.2/2.3
# in the plan), which the original web scanner didn't need since it only
# ever scanned one scope per call.
# =========================================================================

MARKET_SCOPE_MAP = {
    "spot": ["bitget-spot"],
    "future": ["bitget-futures"],
    "both": ["bitget-spot", "bitget-futures"],
}


def _new_job():
    return {
        "results": {}, "completed": 0, "total": 0,
        "cancelled": False, "status": "running", "topPicks": [],
    }


def count_pairs(market):
    """
    Quick "how many pairs would a scan of this market cover" count,
    with NO analysis run - just the token list length(s). Cheap even
    on a cold cache (one Bitget REST call per scope; get_token_list()
    caches the response afterwards so the run_full_scan() that
    typically follows moments later reuses it). Used by the bot's
    Search Signal flow to show "Total pairs: N" up front, before the
    user commits to Full Analysis vs Skip Analysis Detail.
    """
    scopes = MARKET_SCOPE_MAP.get(market)
    if not scopes:
        raise ValueError(f"Unknown market choice: {market!r} (expected spot/future/both)")
    per_scope = {scope: len(get_token_list(scope)["tokens"]) for scope in scopes}
    return {"perScope": per_scope, "total": sum(per_scope.values())}


def scan_market(market, enabled_indicators, enabled_concepts, worker_count=8,
                 on_pair=None, on_progress=None, on_total=None):
    """
    market: "spot" | "future" | "both" - as chosen via the bot's
    Spot/Future/Both prompt (market_select.py).

    Runs run_full_scan() unchanged for each underlying scope, merges the
    results, and re-ranks so "both" gives one combined top-3 instead of
    two separate ones. Returns a plain dict (no job/polling object):

        {
            "tradeable": [ <result dicts, sorted best-first by rankScore>, ... ],
            "topPicks": [ <up to 3 result dicts> ],
            "scanned": <int, total pairs scanned>,
        }

    Each result dict's "multiTimeframe"->"combinedConfidence" is the
    0-100 confidence level the bot messages show (Phase 2.2/2.3).

    `on_total(total_count)`: called once, before any pair is analyzed,
    with the combined pair count across every scope in `market` (so
    "both" reports one grand total, not two separate ones).
    `on_pair(result)` / `on_progress(completed, total)`: same contract
    as run_full_scan()'s callbacks, but `completed`/`total` here are
    running totals across ALL scopes in `market`, so a "both" scan's
    progress reads as one continuous 0-100% instead of restarting at
    a new 0% when it moves from spot to futures.
    """
    scopes = MARKET_SCOPE_MAP.get(market)
    if not scopes:
        raise ValueError(f"Unknown market choice: {market!r} (expected spot/future/both)")

    merged_tradeable = []
    scanned = 0

    if on_total is not None or on_progress is not None:
        combined_total = sum(len(get_token_list(scope)["tokens"]) for scope in scopes)
        if on_total is not None:
            try:
                on_total(combined_total)
            except Exception as exc:
                log.error(f"Signal scan: on_total callback failed: {exc}")
    else:
        combined_total = 0

    completed_so_far = 0

    def _wrapped_progress(scope_completed, scope_total):
        if on_progress is None:
            return
        on_progress(completed_so_far + scope_completed, combined_total)

    for scope in scopes:
        job = _new_job()
        run_full_scan(
            job, scope, enabled_indicators, enabled_concepts, worker_count=worker_count,
            on_pair=on_pair, on_progress=_wrapped_progress,
        )
        scanned += job["completed"]
        completed_so_far += job["completed"]
        merged_tradeable.extend(r for r in job["results"].values() if r.get("tradeable"))

    merged_tradeable.sort(key=lambda r: r.get("rankScore", 0), reverse=True)

    return {
        "tradeable": merged_tradeable,
        "topPicks": merged_tradeable[:3],
        "scanned": scanned,
    }


def scan_market_above_confidence(market, min_confidence, enabled_indicators, enabled_concepts, worker_count=8):
    """
    Same as scan_market(), but for the 24/7 "Find Strong Signal" watcher
    (Phase 2.2): only pairs whose combinedConfidence is >= min_confidence
    (spec calls for an 80+ floor) are returned - the watcher pushes every
    one of these to active users, not just a top-3 shortlist, since the
    whole point of that mode is "don't miss a high-conviction setup".
    """
    scan = scan_market(market, enabled_indicators, enabled_concepts, worker_count=worker_count)
    strong = [
        r for r in scan["tradeable"]
        if r.get("multiTimeframe", {}).get("combinedConfidence", 0) >= min_confidence
    ]
    return {"strong": strong, "scanned": scan["scanned"]}