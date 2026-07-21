# signal_engine.py
# Phase 4.2 wrap-up: the Order Flow page shows raw trade-print buy/sell
# volume, but on its own that's only one piece of the picture. This
# module blends the already-computed Technical Indicators (4.1),
# World Best Trading Concepts (4.2), and the live Order Flow reading
# into a SINGLE overall verdict - the "is this pair actually a buy or
# a sell right now" answer the Order Flow page's new Signal panel
# shows next to the chart.
#
# This is a transparent, rule-based vote (NOT a black box) - every
# indicator/concept that has an opinion casts a weighted vote of +1
# (bullish), -1 (bearish) or 0 (no opinion / not enough data yet).
# Votes are summed and normalized into a -100..+100 score. This keeps
# every number on screen traceable back to a specific indicator
# reading, which matters far more for real trading decisions than a
# more "clever" opaque model would.
#
# IMPORTANT HONESTY NOTE: this score is a rules-based technical-vote
# aggregate over the last 1 window of candles - it is not a probability
# of profit and is not a guarantee of anything. It should be read the
# same way a trader reads any single-timeframe technical read: as one
# input among several, always paired with the trader's own risk
# management (position size, stop loss).

# (label, weight) - weight reflects how much this module's opinion
# should count. SuperTrend/ADX/CHoCH and the live order-flow delta are
# weighted highest since they're the most direct "which side actually
# controls the tape right now" reads; oscillators like Stochastic RSI/
# CCI/MFI are supporting/confirming signals, not primary drivers.
WEIGHTS = {
    "orderFlow": 1.3,
    "superTrend": 1.5,
    "trendStructure": 1.3,
    "changeOfCharacter": 1.1,
    "adx": 1.2,
    "breakOfStructure": 1.0,
    "rsi": 1.0,
    "macd": 1.0,
    "liquiditySweeps": 0.9,
    "bollinger": 0.8,
    "stochRsi": 0.7,
    "mfi": 0.7,
    "cci": 0.7,
    "vwap": 0.6,
}

BUY_THRESHOLD = 20
SELL_THRESHOLD = -20


def _vote(key, direction, note, votes):
    """direction: +1 bullish, -1 bearish, 0 neutral/no-opinion (not recorded)."""
    if direction == 0 or direction is None:
        return
    votes.append({"key": key, "weight": WEIGHTS.get(key, 1.0), "direction": direction, "note": note})


def _collect_votes(indicators, concepts, order_flow_live):
    votes = []

    # --- Order flow (live tape buy% vs sell%) ---
    if order_flow_live:
        buy_pct = order_flow_live.get("buyPct")
        if buy_pct is not None:
            direction = max(-1.0, min(1.0, (buy_pct - 50) / 25))  # scaled, saturates at +/-25% off 50/50
            if abs(buy_pct - 50) >= 2:
                _vote("orderFlow", direction, f"Live tape {buy_pct:.1f}% buy volume", votes)

    # --- Indicators (4.1) ---
    rsi = indicators.get("rsi")
    if isinstance(rsi, (int, float)):
        if rsi < 30:
            _vote("rsi", 1, f"RSI {rsi:.1f} (oversold)", votes)
        elif rsi > 70:
            _vote("rsi", -1, f"RSI {rsi:.1f} (overbought)", votes)

    stoch = indicators.get("stochRsi")
    if isinstance(stoch, (int, float)):
        if stoch < 20:
            _vote("stochRsi", 1, f"Stoch RSI {stoch:.1f} (oversold)", votes)
        elif stoch > 80:
            _vote("stochRsi", -1, f"Stoch RSI {stoch:.1f} (overbought)", votes)

    macd = indicators.get("macd")
    if isinstance(macd, dict) and macd.get("histogram") is not None:
        hist = macd["histogram"]
        _vote("macd", 1 if hist > 0 else (-1 if hist < 0 else 0), f"MACD histogram {hist:+.4f}", votes)

    mfi = indicators.get("mfi")
    if isinstance(mfi, (int, float)):
        if mfi < 20:
            _vote("mfi", 1, f"MFI {mfi:.1f} (oversold)", votes)
        elif mfi > 80:
            _vote("mfi", -1, f"MFI {mfi:.1f} (overbought)", votes)

    cci = indicators.get("cci")
    if isinstance(cci, (int, float)):
        if cci < -100:
            _vote("cci", 1, f"CCI {cci:.1f} (oversold)", votes)
        elif cci > 100:
            _vote("cci", -1, f"CCI {cci:.1f} (overbought)", votes)

    adx = indicators.get("adx")
    if isinstance(adx, dict) and adx.get("adx") is not None and adx["adx"] >= 20:
        if adx["plus_di"] > adx["minus_di"]:
            _vote("adx", 1, f"ADX {adx['adx']:.1f}, +DI > -DI (trending up)", votes)
        elif adx["minus_di"] > adx["plus_di"]:
            _vote("adx", -1, f"ADX {adx['adx']:.1f}, -DI > +DI (trending down)", votes)

    supertrend = indicators.get("superTrend")
    if isinstance(supertrend, dict) and supertrend.get("trend"):
        _vote("superTrend", 1 if supertrend["trend"] == "up" else -1,
              f"SuperTrend flipped {supertrend['trend']}", votes)

    bollinger = indicators.get("bollinger")
    last_close = indicators.get("_lastClose")
    if isinstance(bollinger, dict) and last_close is not None:
        if last_close <= bollinger.get("lower", float("-inf")):
            _vote("bollinger", 1, "Price at/below lower Bollinger Band", votes)
        elif last_close >= bollinger.get("upper", float("inf")):
            _vote("bollinger", -1, "Price at/above upper Bollinger Band", votes)

    vwap = indicators.get("vwap")
    if isinstance(vwap, (int, float)) and last_close is not None:
        _vote("vwap", 1 if last_close > vwap else (-1 if last_close < vwap else 0),
              f"Price {'above' if last_close > vwap else 'below'} VWAP", votes)

    # --- Trading concepts (4.2) ---
    trend_structure = concepts.get("trendStructure")
    if isinstance(trend_structure, dict) and trend_structure.get("trend") in ("uptrend", "downtrend"):
        _vote("trendStructure", 1 if trend_structure["trend"] == "uptrend" else -1,
              f"Market structure: {trend_structure['trend']} (strength {trend_structure.get('strength', 0)})", votes)

    choch = concepts.get("changeOfCharacter")
    if isinstance(choch, dict) and choch.get("changed"):
        _vote("changeOfCharacter", 1 if choch.get("to") == "uptrend" else -1,
              f"CHoCH: {choch.get('from')} -> {choch.get('to')}", votes)

    bos = concepts.get("breakOfStructure")
    if isinstance(bos, dict) and bos.get("broke"):
        _vote("breakOfStructure", 1 if bos.get("direction") == "bullish" else -1,
              f"Break of structure ({bos.get('direction')}) at {bos.get('level')}", votes)

    sweeps = concepts.get("liquiditySweeps")
    if isinstance(sweeps, dict):
        buy_side = sweeps.get("buySideSweeps") or []
        sell_side = sweeps.get("sellSideSweeps") or []
        latest_buy_ts = buy_side[0]["time"] if buy_side else -1
        latest_sell_ts = sell_side[0]["time"] if sell_side else -1
        # A sweep of highs (buy-side liquidity taken) that then closes
        # back below = stop-hunt of breakout buyers -> often bearish
        # continuation. Mirror for a sweep of lows.
        if latest_buy_ts > 0 or latest_sell_ts > 0:
            if latest_buy_ts > latest_sell_ts:
                _vote("liquiditySweeps", -1, f"Buy-side liquidity swept at {buy_side[0]['level']}", votes)
            else:
                _vote("liquiditySweeps", 1, f"Sell-side liquidity swept at {sell_side[0]['level']}", votes)

    return votes


def compute_overall_signal(indicators, concepts, order_flow_live, last_close=None):
    """
    Blends indicators + trading concepts + live order flow into one
    verdict. `indicators` and `concepts` are the dicts returned by
    compute_all_indicators()/compute_all_concepts(); `order_flow_live`
    is the `live` block from get_order_flow() (may be None if the
    order-flow tape hasn't loaded yet - the verdict just runs on
    fewer votes in that case, not fail).

    Returns:
      {
        verdict: "BUY" | "SELL" | "NEUTRAL",
        score: -100..100,
        confidence: 0..100,
        bullishSignals: [str],
        bearishSignals: [str],
        voteCount: int,
      }
    """
    indicators = dict(indicators or {})
    indicators["_lastClose"] = last_close
    votes = _collect_votes(indicators, concepts or {}, order_flow_live or {})

    total_weight = sum(v["weight"] for v in votes)
    if total_weight == 0:
        return {
            "verdict": "NEUTRAL",
            "score": 0,
            "confidence": 0,
            "bullishSignals": [],
            "bearishSignals": [],
            "voteCount": 0,
        }

    raw_score = sum(v["weight"] * v["direction"] for v in votes) / total_weight * 100
    score = max(-100, min(100, round(raw_score, 1)))

    if score >= BUY_THRESHOLD:
        verdict = "BUY"
    elif score <= SELL_THRESHOLD:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    bullish = [v["note"] for v in votes if v["direction"] > 0]
    bearish = [v["note"] for v in votes if v["direction"] < 0]

    # Confidence = how lopsided the vote is (agreement), scaled by how
    # many modules actually had an opinion (a verdict from 2 votes is
    # less trustworthy than the same score from 10 votes).
    participation = min(1.0, len(votes) / 8)
    confidence = round(min(100, abs(score) * (0.6 + 0.4 * participation)))

    return {
        "verdict": verdict,
        "score": score,
        "confidence": confidence,
        "bullishSignals": bullish,
        "bearishSignals": bearish,
        "voteCount": len(votes),
    }


def build_chart_markers(concepts):
    """
    Turns a handful of the most recent structural events (BOS, CHoCH,
    liquidity sweeps) into chart marker descriptors the frontend can
    hand straight to LightweightCharts' series.setMarkers(). Kept
    deliberately sparse (last event per type, not every historical
    one) so the chart stays readable instead of turning into a wall
    of arrows.
    """
    markers = []

    bos = (concepts or {}).get("breakOfStructure")
    if isinstance(bos, dict) and bos.get("broke") and bos.get("time"):
        markers.append({
            "time": bos["time"], "position": "aboveBar" if bos["direction"] == "bullish" else "belowBar",
            "color": "#26a69a" if bos["direction"] == "bullish" else "#ef5350",
            "shape": "arrowUp" if bos["direction"] == "bullish" else "arrowDown",
            "text": f"BOS {bos['direction']}",
        })

    choch = (concepts or {}).get("changeOfCharacter")
    if isinstance(choch, dict) and choch.get("changed") and choch.get("time"):
        bullish = choch.get("to") == "uptrend"
        markers.append({
            "time": choch["time"], "position": "belowBar" if bullish else "aboveBar",
            "color": "#42a5f5", "shape": "circle",
            "text": f"CHoCH -> {choch.get('to')}",
        })

    sweeps = (concepts or {}).get("liquiditySweeps")
    if isinstance(sweeps, dict):
        buy_side = sweeps.get("buySideSweeps") or []
        sell_side = sweeps.get("sellSideSweeps") or []
        if buy_side:
            markers.append({
                "time": buy_side[0]["time"], "position": "aboveBar",
                "color": "#ffb300", "shape": "circle", "text": "Liquidity swept (highs)",
            })
        if sell_side:
            markers.append({
                "time": sell_side[0]["time"], "position": "belowBar",
                "color": "#ffb300", "shape": "circle", "text": "Liquidity swept (lows)",
            })

    markers.sort(key=lambda m: m["time"])
    return markers