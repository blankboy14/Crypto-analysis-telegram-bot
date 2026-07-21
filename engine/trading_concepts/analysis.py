# analysis.py - runs every trading-concept module in this package
# against one candle window and returns everything in one dict. Same
# pattern as core/indicators/analysis.py's compute_all_indicators().

from .market_structure import analyze_market_structure
from .fibonacci import analyze_fibonacci
from .support_resistance import analyze_support_resistance
from .supply_demand import analyze_supply_demand
from .order_blocks import analyze_order_blocks
from .fair_value_gaps import analyze_fair_value_gaps
from .break_of_structure import analyze_break_of_structure
from .change_of_character import analyze_change_of_character
from .liquidity_sweeps import analyze_liquidity_sweeps
from .liquidity_identification import analyze_liquidity_identification
from .premium_discount_zones import analyze_premium_discount_zones
from .trend_structure import analyze_trend_structure
from .ict import analyze_ict
from .smc import analyze_smc
from .wyckoff import analyze_wyckoff
from .elliott_wave import analyze_elliott_wave
from .price_action import analyze_price_action
from .candlestick_patterns import analyze_candlestick_patterns
from .session_analysis import analyze_session_analysis
from .institutional_order_flow import analyze_institutional_order_flow
from .market_cycles import analyze_market_cycles

# The 21 concepts from the Phase 4.2 spec ("World Best Trading
# Concepts"), in spec order. `kind` mirrors the split explained when
# this package was planned: "building" = a shared primitive other
# concept files import from directly (e.g. ict.py imports
# order_blocks.py); "concept" = a full standalone methodology/read.
CONCEPT_META = {
    "ict":                      {"label": "ICT (Inner Circle Trader)",         "kind": "concept"},
    "smc":                      {"label": "Smart Money Concepts (SMC)",        "kind": "concept"},
    "wyckoff":                  {"label": "Wyckoff Method",                    "kind": "concept"},
    "elliottWave":              {"label": "Elliott Wave",                      "kind": "concept"},
    "fibonacci":                {"label": "Fibonacci Retracement & Extension", "kind": "building"},
    "supportResistance":        {"label": "Support & Resistance",              "kind": "building"},
    "supplyDemand":             {"label": "Supply & Demand",                   "kind": "building"},
    "orderBlocks":              {"label": "Order Blocks",                      "kind": "building"},
    "fairValueGaps":            {"label": "Fair Value Gaps (FVG)",             "kind": "building"},
    "breakOfStructure":         {"label": "Break of Structure (BOS)",          "kind": "building"},
    "changeOfCharacter":        {"label": "Change of Character (CHoCH)",       "kind": "building"},
    "liquiditySweeps":          {"label": "Liquidity Sweeps",                  "kind": "building"},
    "liquidityIdentification":  {"label": "Liquidity Identification",         "kind": "building"},
    "premiumDiscountZones":     {"label": "Premium & Discount Zones",          "kind": "building"},
    "marketStructure":          {"label": "Market Structure",                  "kind": "building"},
    "trendStructure":           {"label": "Trend Structure",                   "kind": "building"},
    "priceAction":              {"label": "Price Action",                      "kind": "concept"},
    "candlestickPatterns":      {"label": "Candlestick Patterns",              "kind": "concept"},
    "sessionAnalysis":          {"label": "Session Analysis",                  "kind": "concept"},
    "institutionalOrderFlow":   {"label": "Institutional Order Flow",          "kind": "concept"},
    "marketCycles":             {"label": "Market Cycles",                     "kind": "concept"},
}
CONCEPT_KEYS = list(CONCEPT_META.keys())

_ANALYZERS = {
    "marketStructure": lambda c, lb: analyze_market_structure(c, lb),
    "fibonacci": lambda c, lb: analyze_fibonacci(c, lb),
    "supportResistance": lambda c, lb: analyze_support_resistance(c, lb),
    "supplyDemand": lambda c, lb: analyze_supply_demand(c),
    "orderBlocks": lambda c, lb: analyze_order_blocks(c),
    "fairValueGaps": lambda c, lb: analyze_fair_value_gaps(c),
    "breakOfStructure": lambda c, lb: analyze_break_of_structure(c, lb),
    "changeOfCharacter": lambda c, lb: analyze_change_of_character(c, lb),
    "liquiditySweeps": lambda c, lb: analyze_liquidity_sweeps(c, lb),
    "liquidityIdentification": lambda c, lb: analyze_liquidity_identification(c, lb),
    "premiumDiscountZones": lambda c, lb: analyze_premium_discount_zones(c, lb),
    "trendStructure": lambda c, lb: analyze_trend_structure(c, lb),
    "ict": lambda c, lb: analyze_ict(c, lb),
    "smc": lambda c, lb: analyze_smc(c, lb),
    "wyckoff": lambda c, lb: analyze_wyckoff(c),
    "elliottWave": lambda c, lb: analyze_elliott_wave(c, lb),
    "priceAction": lambda c, lb: analyze_price_action(c),
    "candlestickPatterns": lambda c, lb: analyze_candlestick_patterns(c),
    "sessionAnalysis": lambda c, lb: analyze_session_analysis(c),
    "institutionalOrderFlow": lambda c, lb: analyze_institutional_order_flow(c, lb),
    "marketCycles": lambda c, lb: analyze_market_cycles(c),
}


def compute_all_concepts(candles, enabled=None, lookback=3, return_errors=False):
    """
    Runs every concept above against one candle window. `enabled` is
    an optional {key: bool} map (see CONCEPT_KEYS) - concepts mapped
    to False are skipped entirely, same toggle pattern as
    compute_all_indicators(). `lookback` is the swing-point sensitivity
    shared by every structure-based concept (smaller = more swings
    found = more sensitive/noisier).

    A single concept raising an exception doesn't take down the whole
    result - it's recorded (and, if return_errors=True, reported back
    per-concept) instead of aborting everything else that DID compute
    cleanly.
    """
    def is_on(key):
        return enabled is None or enabled.get(key, True)

    out = {}
    errors = {}

    for key, fn in _ANALYZERS.items():
        if not is_on(key):
            continue
        try:
            out[key] = fn(candles, lookback)
        except Exception as exc:
            out[key] = None
            errors[key] = str(exc)

    if return_errors:
        return out, errors
    return out
