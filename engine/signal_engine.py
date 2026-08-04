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
    "fundingRate": 0.8,   # not voted here - folded in separately by engine/futures_metrics.py, see that module
    "openInterest": 0.7,  # same as fundingRate above
    "superTrend": 1.5,
    "trendStructure": 1.3,
    "changeOfCharacter": 1.1,
    "adx": 1.2,
    "breakOfStructure": 1.0,
    "rsi": 1.0,
    "rsiDivergence": 1.2,
    "rsiMulti": 1.1,
    "macd": 1.0,
    "liquiditySweeps": 0.9,
    "bollinger": 0.8,
    "stochRsi": 0.7,
    "mfi": 0.7,
    "cci": 0.7,
    "vwap": 0.6,
    # --- added so every remaining computed indicator/concept actually
    # has a say in the final verdict, not just a display-only value ---
    "ema": 0.9,
    "sma": 0.7,
    "ichimoku": 1.0,
    "parabolicSar": 0.8,
    "obv": 0.7,
    "volumeProfile": 0.6,
    "pivotPoints": 0.5,
    "buySellVolume": 0.6,
    "deltaVolume": 0.5,
    "volumeSpikes": 0.6,
    "premiumDiscountZones": 0.6,
    "fibonacci": 0.5,
    "supportResistance": 0.6,
    "supplyDemand": 0.7,
    "orderBlocks": 0.7,
    "fairValueGaps": 0.5,
    "ict": 1.0,
    "wyckoff": 0.8,
    "elliottWave": 0.5,
    "priceAction": 0.7,
    "candlestickPatterns": 0.6,
    "institutionalOrderFlow": 0.8,
    "marketCycles": 0.7,
}
# Deliberately NOT independently voted, even though each is fully
# computed and used elsewhere - forcing a vote out of these would
# either double-count a signal already voted under a different key, or
# fabricate direction from something that has none:
#   atr                    - a volatility MAGNITUDE, not a direction;
#                            already used for stop-loss/target sizing
#                            in engine.signal_scanner.build_trade_plan.
#   rvol                   - "how much more than usual", not "which
#                            way" - applied as a confidence multiplier
#                            in compute_overall_signal below instead.
#   volumeAnalysis         - its own trend field describes VOLUME
#                            rising/falling, not price direction;
#                            folded into that same confidence
#                            multiplier alongside rvol.
#   marketStructure        - trendStructure already votes this EXACT
#                            same uptrend/downtrend/ranging read (it's
#                            literally built from
#                            analyze_market_structure's own output) -
#                            voting both would double-count one signal.
#   smc                    - a composite of trendStructure,
#                            breakOfStructure, changeOfCharacter,
#                            liquiditySweeps, and orderBlocks - every
#                            piece of it already votes individually
#                            under those keys; voting smc too would
#                            count each of those signals twice.
#   liquidityIdentification, sessionAnalysis
#                          - both map WHERE liquidity/session ranges
#                            sit, with no directional read of their
#                            own (ict.py already consumes the former
#                            for its drawOnLiquidity read, which does
#                            feed into ict's vote below).


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

    rsi_div = indicators.get("rsiDivergence")
    if isinstance(rsi_div, dict) and rsi_div.get("signal"):
        sig = rsi_div["signal"]
        label = {
            "bullish": "Bullish Divergence", "hiddenBullish": "Hidden Bullish Divergence",
            "bearish": "Bearish Divergence", "hiddenBearish": "Hidden Bearish Divergence",
        }.get(sig, sig)
        direction = 1 if sig in ("bullish", "hiddenBullish") else -1
        _vote("rsiDivergence", direction, f"RSI {label} (RSI {rsi_div.get('rsi')})", votes)

    rsi_multi = indicators.get("rsiMulti")
    if isinstance(rsi_multi, dict):
        readings = [v for v in rsi_multi.values() if v is not None]
        if readings:
            oversold_count = sum(1 for v in readings if v < 30)
            overbought_count = sum(1 for v in readings if v > 70)
            # Needs a MAJORITY of the configured periods agreeing, not
            # just one - RSI(14) alone already votes above; this is
            # specifically about several different lookback windows
            # (fast AND slow) confirming the same extreme at once,
            # which plain RSI(14) can't tell you on its own.
            if oversold_count >= len(readings) / 2 + 0.5:
                _vote("rsiMulti", oversold_count / len(readings),
                      f"RSI oversold across {oversold_count}/{len(readings)} periods {sorted(rsi_multi.keys())}", votes)
            elif overbought_count >= len(readings) / 2 + 0.5:
                _vote("rsiMulti", -overbought_count / len(readings),
                      f"RSI overbought across {overbought_count}/{len(readings)} periods {sorted(rsi_multi.keys())}", votes)

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

    ema = indicators.get("ema")
    if isinstance(ema, dict) and ema.get(9) is not None and ema.get(21) is not None:
        if ema[9] > ema[21]:
            _vote("ema", 1, f"EMA9 ({ema[9]:.6g}) > EMA21 ({ema[21]:.6g})", votes)
        elif ema[9] < ema[21]:
            _vote("ema", -1, f"EMA9 ({ema[9]:.6g}) < EMA21 ({ema[21]:.6g})", votes)

    sma50 = indicators.get("sma50")
    if isinstance(sma50, (int, float)) and last_close is not None:
        if last_close > sma50:
            _vote("sma", 1, f"Price above SMA50 ({sma50:.6g})", votes)
        elif last_close < sma50:
            _vote("sma", -1, f"Price below SMA50 ({sma50:.6g})", votes)

    ichimoku = indicators.get("ichimoku")
    if isinstance(ichimoku, dict) and ichimoku.get("cloud_bias") in ("bullish", "bearish"):
        _vote("ichimoku", 1 if ichimoku["cloud_bias"] == "bullish" else -1,
              f"Price {ichimoku['cloud_bias']} of the Ichimoku cloud", votes)

    psar = indicators.get("parabolicSar")
    if isinstance(psar, dict) and psar.get("trend"):
        _vote("parabolicSar", 1 if psar["trend"] == "up" else -1,
              f"Parabolic SAR flipped {psar['trend']}", votes)

    obv = indicators.get("obv")
    if isinstance(obv, dict) and obv.get("trend") in ("rising", "falling"):
        _vote("obv", 1 if obv["trend"] == "rising" else -1, f"OBV {obv['trend']}", votes)

    vp = indicators.get("volumeProfile")
    if isinstance(vp, dict) and vp.get("poc") is not None and last_close is not None:
        if last_close > vp["poc"]:
            _vote("volumeProfile", 1, f"Price above volume POC ({vp['poc']:.6g})", votes)
        elif last_close < vp["poc"]:
            _vote("volumeProfile", -1, f"Price below volume POC ({vp['poc']:.6g})", votes)

    pivots = indicators.get("pivotPoints")
    if isinstance(pivots, dict) and pivots.get("pp") is not None and last_close is not None:
        if last_close > pivots["pp"]:
            _vote("pivotPoints", 1, f"Price above pivot ({pivots['pp']:.6g})", votes)
        elif last_close < pivots["pp"]:
            _vote("pivotPoints", -1, f"Price below pivot ({pivots['pp']:.6g})", votes)

    bsv = indicators.get("buySellVolume")
    if isinstance(bsv, dict) and bsv.get("delta"):
        _vote("buySellVolume", 1 if bsv["delta"] > 0 else -1,
              f"Est. buy/sell volume delta {bsv['delta']:+.4g}", votes)

    dv = indicators.get("deltaVolume")
    if isinstance(dv, dict) and dv.get("delta"):
        _vote("deltaVolume", 1 if dv["delta"] > 0 else -1,
              f"Recent delta volume {dv['delta']:+.4g}", votes)

    spikes = indicators.get("volumeSpikes")
    last_open = indicators.get("_lastOpen")
    last_candle_time = indicators.get("_lastCandleTime")
    if spikes and last_candle_time is not None and last_close is not None and last_open is not None:
        if any(s["time"] == last_candle_time for s in spikes) and last_close != last_open:
            _vote("volumeSpikes", 1 if last_close > last_open else -1,
                  "Latest candle is a volume spike", votes)

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

    pdz = concepts.get("premiumDiscountZones")
    if isinstance(pdz, dict) and pdz.get("currentZone") in ("premium", "discount"):
        _vote("premiumDiscountZones", 1 if pdz["currentZone"] == "discount" else -1,
              f"Price in {pdz['currentZone']} zone (buy discount / sell premium)", votes)

    fib = concepts.get("fibonacci")
    if isinstance(fib, dict) and fib.get("retracements") and last_close is not None:
        level_618 = fib["retracements"].get(0.618)
        swing_high = (fib.get("swingHigh") or {}).get("price")
        swing_low = (fib.get("swingLow") or {}).get("price")
        if level_618 is not None and swing_high and swing_low and swing_high > swing_low:
            tolerance = (swing_high - swing_low) * 0.03
            if abs(last_close - level_618) <= tolerance:
                # direction="down" = price retraced UP off a low (a
                # rally being measured) - 618 here is a classic
                # rejection/resistance zone -> bearish. direction="up"
                # = price retraced DOWN off a high (a drop being
                # measured) - 618 here is a classic bounce/support
                # zone -> bullish.
                _vote("fibonacci", -1 if fib.get("direction") == "down" else 1,
                      "Price at the 0.618 Fibonacci golden pocket", votes)

    sr = concepts.get("supportResistance")
    if isinstance(sr, dict) and last_close:
        nearest_support = sr["support"][0]["price"] if sr.get("support") else None
        nearest_resistance = sr["resistance"][0]["price"] if sr.get("resistance") else None
        # Within 1% of a level counts as "reacting from it" - closer
        # than that is noise, further than that isn't in play yet.
        if nearest_support and abs(last_close - nearest_support) / last_close <= 0.01:
            _vote("supportResistance", 1, f"Price near support ({nearest_support:.6g})", votes)
        elif nearest_resistance and abs(last_close - nearest_resistance) / last_close <= 0.01:
            _vote("supportResistance", -1, f"Price near resistance ({nearest_resistance:.6g})", votes)

    sd = concepts.get("supplyDemand")
    if isinstance(sd, dict) and last_close is not None:
        in_demand = any(z["bottom"] <= last_close <= z["top"] for z in (sd.get("demand") or []))
        in_supply = any(z["bottom"] <= last_close <= z["top"] for z in (sd.get("supply") or []))
        if in_demand and not in_supply:
            _vote("supplyDemand", 1, "Price inside an unmitigated demand zone", votes)
        elif in_supply and not in_demand:
            _vote("supplyDemand", -1, "Price inside an unmitigated supply zone", votes)

    obs = concepts.get("orderBlocks")
    if isinstance(obs, dict) and last_close is not None:
        in_bull_ob = any(b["bottom"] <= last_close <= b["top"] for b in (obs.get("bullish") or []))
        in_bear_ob = any(b["bottom"] <= last_close <= b["top"] for b in (obs.get("bearish") or []))
        if in_bull_ob and not in_bear_ob:
            _vote("orderBlocks", 1, "Price inside an unmitigated bullish order block", votes)
        elif in_bear_ob and not in_bull_ob:
            _vote("orderBlocks", -1, "Price inside an unmitigated bearish order block", votes)

    fvgs = concepts.get("fairValueGaps")
    if isinstance(fvgs, dict) and last_close is not None:
        in_bull_fvg = any(g["bottom"] <= last_close <= g["top"] for g in (fvgs.get("bullish") or []))
        in_bear_fvg = any(g["bottom"] <= last_close <= g["top"] for g in (fvgs.get("bearish") or []))
        if in_bull_fvg and not in_bear_fvg:
            _vote("fairValueGaps", 1, "Price inside an open bullish FVG", votes)
        elif in_bear_fvg and not in_bull_fvg:
            _vote("fairValueGaps", -1, "Price inside an open bearish FVG", votes)

    ict = concepts.get("ict")
    if isinstance(ict, dict) and ict.get("bias") in ("bullish", "bearish"):
        _vote("ict", 1 if ict["bias"] == "bullish" else -1, f"ICT bias: {ict['bias']}", votes)

    wyckoff = concepts.get("wyckoff")
    if isinstance(wyckoff, dict) and wyckoff.get("phaseGuess") in ("markup", "markdown"):
        _vote("wyckoff", 1 if wyckoff["phaseGuess"] == "markup" else -1,
              f"Wyckoff phase: {wyckoff['phaseGuess']}", votes)

    ew = concepts.get("elliottWave")
    if isinstance(ew, dict) and ew.get("waveType") == "impulse" and ew.get("currentWave") == 5 and ew.get("swings"):
        swing_prices = [s["price"] for s in ew["swings"]]
        if len(swing_prices) >= 2 and swing_prices[0] != swing_prices[-1]:
            impulse_up = swing_prices[-1] > swing_prices[0]
            confidence_scale = (ew.get("confidence") or 50) / 100
            # Wave 5 is the impulse's LAST leg - classic Elliott theory
            # treats it as the exhaustion point, so this votes AGAINST
            # the impulse direction (anticipating the reversal), scaled
            # down by the read's own confidence - the single most
            # speculative vote in this whole function.
            _vote("elliottWave", (-1 if impulse_up else 1) * confidence_scale,
                  f"Elliott wave 5 of an {'up' if impulse_up else 'down'} impulse (possible exhaustion)", votes)

    pa = concepts.get("priceAction")
    if isinstance(pa, dict):
        if pa.get("momentum") == "strong_bullish":
            _vote("priceAction", 1, "Price action: strong bullish momentum", votes)
        elif pa.get("momentum") == "strong_bearish":
            _vote("priceAction", -1, "Price action: strong bearish momentum", votes)
        elif pa.get("rejection") == "upper":
            _vote("priceAction", -1, "Upper-wick rejection candle", votes)
        elif pa.get("rejection") == "lower":
            _vote("priceAction", 1, "Lower-wick rejection candle", votes)

    patterns = concepts.get("candlestickPatterns")
    if patterns:
        latest = max(patterns, key=lambda p: p["time"])
        if latest["type"] in ("bullish", "bearish"):
            _vote("candlestickPatterns", 1 if latest["type"] == "bullish" else -1,
                  f"Candlestick pattern: {latest['name']}", votes)

    iof = concepts.get("institutionalOrderFlow")
    if isinstance(iof, dict) and iof.get("confirmation") == "confirming" and iof.get("structure") in ("uptrend", "downtrend"):
        _vote("institutionalOrderFlow", 1 if iof["structure"] == "uptrend" else -1,
              f"Institutional order flow confirming the {iof['structure']}", votes)

    mc = concepts.get("marketCycles")
    if isinstance(mc, dict) and mc.get("cyclePhase"):
        phase_direction = {"markup": 1.0, "accumulation": 0.4, "distribution": -0.4, "markdown": -1.0}.get(mc["cyclePhase"])
        if phase_direction:
            _vote("marketCycles", phase_direction, f"Market cycle phase: {mc['cyclePhase']}", votes)

    return votes


def compute_overall_signal(indicators, concepts, order_flow_live, last_close=None,
                            last_open=None, last_candle_time=None):
    """
    Blends indicators + trading concepts + live order flow into one
    verdict. `indicators` and `concepts` are the dicts returned by
    compute_all_indicators()/compute_all_concepts(); `order_flow_live`
    is the `live` block from get_order_flow() (may be None if the
    order-flow tape hasn't loaded yet - the verdict just runs on
    fewer votes in that case, not fail). `last_open`/`last_candle_time`
    are optional - only the volumeSpikes vote uses them (whether the
    latest candle itself was a spike, and which way it closed); every
    other vote only needs last_close.

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
    indicators["_lastOpen"] = last_open
    indicators["_lastCandleTime"] = last_candle_time
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
            "votes": [],
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

    # rvol/volumeAnalysis aren't votes (see the WEIGHTS comment above -
    # neither has an inherent direction), but real participation behind
    # a move is genuinely informative about how much to trust it, so
    # they scale confidence in whatever direction the votes above
    # already agreed on instead.
    if verdict != "NEUTRAL":
        volume_multiplier = 1.0
        rvol = indicators.get("rvol")
        if isinstance(rvol, (int, float)):
            if rvol >= 2.0:
                volume_multiplier *= 1.15
            elif rvol < 0.5:
                volume_multiplier *= 0.85

        vol_analysis = indicators.get("volumeAnalysis")
        if isinstance(vol_analysis, dict):
            if vol_analysis.get("trend") == "rising":
                volume_multiplier *= 1.08
            elif vol_analysis.get("trend") == "falling":
                volume_multiplier *= 0.92

        confidence = round(min(100, confidence * volume_multiplier))

    return {
        "verdict": verdict,
        "score": score,
        "confidence": confidence,
        "bullishSignals": bullish,
        "bearishSignals": bearish,
        "voteCount": len(votes),
        # Raw {key, weight, direction, note} votes - kept alongside the
        # already-existing bullish/bearish note lists (unchanged, so
        # nothing that read those before breaks). Added so callers can
        # tell an indicator's vote (e.g. "rsi") apart from a trading
        # concept's vote (e.g. "trendStructure") by key, which the note
        # strings alone don't make easy to do reliably.
        "votes": votes,
    }


# Fixed classification of every vote `key` above into which of the two
# analysis families it belongs to - used by callers (e.g. the bot's
# Search Signal full-analysis mode) that want to show "indicator info"
# and "concept info" as separate lines instead of one merged list.
# orderFlow is deliberately its own third bucket (live tape, not a
# chart-derived indicator/concept) - see result["orderFlow"] instead.
INDICATOR_VOTE_KEYS = {
    "rsi", "rsiDivergence", "stochRsi", "macd", "mfi", "cci", "adx", "superTrend", "bollinger", "vwap",
    "ema", "sma", "ichimoku", "parabolicSar", "obv", "volumeProfile", "pivotPoints",
    "buySellVolume", "deltaVolume", "volumeSpikes",
}
CONCEPT_VOTE_KEYS = {"trendStructure", "changeOfCharacter", "breakOfStructure", "liquiditySweeps"}


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