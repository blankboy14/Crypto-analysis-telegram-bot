"""
tests/test_indicators.py

Basic sanity tests for engine/indicators - run with:
    pytest tests/test_indicators.py

These aren't meant to validate exact indicator math (that's covered by
each indicator's own docstring/formula) - they check the things that
would actually break the bot if broken: compute_all_indicators() runs
without raising on realistic candle data, respects the enabled/disabled
toggle map (what database/indicator_toggles.json feeds it), and
degrades gracefully (returns None, not an exception) when there isn't
enough candle history yet - since a newly-listed pair with only a
handful of candles is a normal, expected input during a live scan, not
an error case.
"""
import os
import random
import sys

import pytest

# So `pytest` run from the project root (or anywhere) can find `engine`
# without needing the project installed as a package.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from engine.indicators.analysis import compute_all_indicators, INDICATOR_KEYS
from engine.indicators.rsi import compute_rsi


def make_candles(n=300, start_price=100.0, seed=42):
    """
    Synthetic OHLCV candles with a mild upward drift plus noise - enough
    variation for every indicator (including ones that need distinct
    high/low/close spread, like ATR or CCI) to actually compute a value
    instead of returning None because every candle looked identical.
    """
    rng = random.Random(seed)
    candles = []
    price = start_price
    for i in range(n):
        price += rng.uniform(-1.5, 1.6)
        price = max(price, 1.0)
        open_ = price - rng.uniform(0, 1.0)
        high = max(open_, price) + rng.uniform(0.1, 1.0)
        low = min(open_, price) - rng.uniform(0.1, 1.0)
        volume = rng.uniform(500, 5000)
        candles.append({
            "time": i, "open": open_, "high": high, "low": low,
            "close": price, "volume": volume,
        })
    return candles


class TestComputeAllIndicators:
    def test_runs_without_error_on_realistic_candles(self):
        candles = make_candles(300)
        result = compute_all_indicators(candles)
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_every_enabled_key_present_or_none(self):
        """Each INDICATOR_KEYS entry should show up in the output (its
        value may legitimately be None if that indicator needs more
        history than was given, but the key itself should be there -
        a missing key usually means a wiring bug, not a data gap)."""
        candles = make_candles(300)
        result = compute_all_indicators(candles)
        for key in INDICATOR_KEYS:
            if key == "sma":
                # sma expands into sma20/sma50/sma200 rather than one
                # "sma" key - see analysis.py's special-cased handling.
                assert "sma20" in result
                assert "sma50" in result
                assert "sma200" in result
            else:
                assert key in result, f"expected '{key}' in compute_all_indicators() output"

    def test_disabled_indicator_is_skipped(self):
        candles = make_candles(300)
        enabled = {key: True for key in INDICATOR_KEYS}
        enabled["rsi"] = False
        result = compute_all_indicators(candles, enabled=enabled)
        assert "rsi" not in result
        # everything else should still be there
        assert "macd" in result

    def test_not_enough_candles_degrades_gracefully(self):
        """A handful of candles (a just-listed pair, or the very start
        of a scan) shouldn't raise - each indicator should just come
        back None until there's enough history for it specifically."""
        candles = make_candles(5)
        result = compute_all_indicators(candles)
        assert isinstance(result, dict)
        # rsi needs 15+ closes by default - too little history here.
        assert result.get("rsi") is None

    def test_return_errors_flag_returns_tuple(self):
        """With return_errors=True, compute_all_indicators() returns
        (out, errors) instead of just out - errors is a diagnosable
        {key: message} map for anything that genuinely raised, rather
        than an indicator that's merely None from too little history
        being indistinguishable from a real bug."""
        candles = make_candles(300)
        out, errors = compute_all_indicators(candles, return_errors=True)
        assert isinstance(out, dict)
        assert isinstance(errors, dict)
        assert len(out) > 0


class TestRsiSanity:
    def test_rsi_within_bounds(self):
        candles = make_candles(60)
        closes = [c["close"] for c in candles]
        value = compute_rsi(closes, period=14)
        if value is not None:
            assert 0.0 <= value <= 100.0

    def test_rsi_none_when_too_few_closes(self):
        assert compute_rsi([1.0, 2.0, 3.0], period=14) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))