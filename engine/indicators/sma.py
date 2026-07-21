# sma.py - Simple Moving Average
# Works on a plain list of closing prices (oldest -> newest).


def compute_sma(values, period):
    """Simple Moving Average of the most recent `period` values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period