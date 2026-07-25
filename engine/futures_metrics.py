# futures_metrics.py
# Futures-only context that neither the candle-based indicators
# (engine/indicators/) nor the trading concepts (engine/trading_concepts/)
# can see: the funding rate and open interest Bitget already returns
# alongside every futures ticker (see engine/bitget_api.py's
# fetch_bitget_futures_tickers). Both are live, once analyze_one_pair
# is called - not per-timeframe, since neither has a "1h version" vs
# a "1d version"; a pair has ONE current funding rate and ONE current
# open interest, just like it has one live order-flow tape - so these
# fold into the already-combined multi-timeframe score the exact same
# way engine.signal_scanner.fold_in_order_flow does, not into
# signal_engine's per-timeframe vote collection.
#
# Spot pairs have neither concept (no funding, no open interest) - both
# functions below are simply skipped for spot (callers pass None).

import time

from engine.signal_engine import WEIGHTS

# Funding rate this extreme (in EITHER direction) is read as "one side
# is now crowded and paying heavily to stay in the trade" - a classic
# contrarian/mean-reversion tell, not a trend-following one: very
# positive funding = longs are crowded and paying shorts (squeeze/
# unwind risk to the downside); very negative = shorts are crowded
# (squeeze risk to the upside). Below this magnitude, funding is normal
# background noise and gets no vote at all.
FUNDING_RATE_EXTREME_THRESHOLD = 0.0005  # 0.05% per interval (Bitget's usual 8h funding cadence)

# (raw_symbol, exchange) -> (timestamp, openInterest, price) - the last
# sample seen for this pair, so open interest can be read as a CHANGE
# (rising/falling) rather than a bare level, which has no inherent
# direction on its own. Unbounded in count of pairs but each entry is
# tiny (3 numbers), and naturally self-limits to "pairs actually
# scanned recently" - never explicitly trimmed since there's nothing
# here worth expiring on a timer (a stale sample just means the next
# comparison spans a longer, still-valid gap).
_oi_history: dict[tuple, tuple[float, float, float]] = {}

# How much the open-interest-vs-price comparison must move to count as
# a real change worth an opinion, vs just noise between two samples.
OI_CHANGE_THRESHOLD_PCT = 2.0
PRICE_CHANGE_THRESHOLD_PCT = 0.5


def fold_in_funding_rate(combined_score: float, combined_confidence: float, funding_rate: float | None):
    """
    Contrarian nudge from an extreme funding rate - same blending
    pattern as engine.signal_scanner.fold_in_order_flow (weighted
    average against a fixed "whole 6-timeframe blend" base weight, plus
    a small confidence nudge for agreement/disagreement).

    Returns (new_score, new_confidence, folded_in: bool, info_text).
    folded_in is False (score/confidence unchanged) when funding_rate
    is None (spot pair) or within normal range (nothing extreme to
    react to) - info_text is still returned either way for display.
    """
    if funding_rate is None:
        return combined_score, combined_confidence, False, "No funding rate (spot pair)"

    info_text = f"Funding rate {funding_rate * 100:+.4f}%"
    if abs(funding_rate) < FUNDING_RATE_EXTREME_THRESHOLD:
        return combined_score, combined_confidence, False, f"{info_text} (normal range)"

    # Positive funding (longs paying shorts) => contrarian BEARISH tilt.
    # Negative funding (shorts paying longs) => contrarian BULLISH tilt.
    contrarian_direction_score = -100.0 if funding_rate > 0 else 100.0
    funding_weight = WEIGHTS.get("fundingRate", 0.8)
    base_weight = 6.0  # same convention as fold_in_order_flow - whole timeframe blend vs this one vote

    new_score = (combined_score * base_weight + contrarian_direction_score * funding_weight) / (base_weight + funding_weight)
    agrees = (contrarian_direction_score > 0) == (combined_score > 0)
    new_confidence = max(0, min(100, combined_confidence + (5 if agrees else -10)))

    squeeze_side = "longs crowded, squeeze risk down" if funding_rate > 0 else "shorts crowded, squeeze risk up"
    return round(new_score, 1), round(new_confidence, 1), True, f"{info_text} ({squeeze_side})"


def fold_in_open_interest(combined_score: float, combined_confidence: float, raw_symbol: str, exchange: str,
                           open_interest: float | None, last_price: float | None):
    """
    Reads open interest as a CHANGE since the last time this exact pair
    was scanned (rising/falling), cross-checked against the price move
    over that same span - not a one-off snapshot, since a bare OI
    number has no direction of its own:
      - OI up + price up   -> fresh money entering with the trend (bullish confirmation)
      - OI up + price down -> fresh money entering against the trend (bearish confirmation)
      - OI down             -> positions closing (de-leveraging) - direction is
                               ambiguous (could be either side taking profit/cutting
                               losses), so this deliberately casts NO vote rather
                               than guessing.

    Returns (new_score, new_confidence, folded_in: bool, info_text).
    The very first time a pair is seen there's nothing to compare
    against yet, so this only starts contributing from the second scan
    of that pair onward (info_text says so explicitly rather than
    silently doing nothing).
    """
    if open_interest is None or last_price is None:
        return combined_score, combined_confidence, False, "No open interest data (spot pair)"

    key = (raw_symbol, exchange)
    now = time.time()
    prior = _oi_history.get(key)
    _oi_history[key] = (now, open_interest, last_price)

    if prior is None:
        return combined_score, combined_confidence, False, "Open interest: building history (first scan of this pair)"

    _, prior_oi, prior_price = prior
    if prior_oi <= 0 or prior_price <= 0:
        return combined_score, combined_confidence, False, "Open interest: insufficient prior data"

    oi_change_pct = (open_interest - prior_oi) / prior_oi * 100
    price_change_pct = (last_price - prior_price) / prior_price * 100
    info_text = f"Open interest {oi_change_pct:+.1f}% since last scan (price {price_change_pct:+.2f}%)"

    if oi_change_pct <= OI_CHANGE_THRESHOLD_PCT:
        # Falling or flat OI - no fresh conviction to read either way.
        return combined_score, combined_confidence, False, info_text
    if abs(price_change_pct) < PRICE_CHANGE_THRESHOLD_PCT:
        # OI is rising but price barely moved - fresh positions opening
        # on both sides roughly canceling out; not a clean read yet.
        return combined_score, combined_confidence, False, info_text

    direction_score = 100.0 if price_change_pct > 0 else -100.0
    oi_weight = WEIGHTS.get("openInterest", 0.7)
    base_weight = 6.0

    new_score = (combined_score * base_weight + direction_score * oi_weight) / (base_weight + oi_weight)
    agrees = (direction_score > 0) == (combined_score > 0)
    new_confidence = max(0, min(100, combined_confidence + (5 if agrees else -10)))

    read = "fresh money confirming the move" if agrees else "fresh money building AGAINST the current technical read"
    return round(new_score, 1), round(new_confidence, 1), True, f"{info_text} - {read}"