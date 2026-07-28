"""
engine/risk_manager.py

Money Management add-on - turns a saved wallet balance + a trade
plan's entry/stop-loss into a suggested position size and leverage.

This is a FIXED-RISK-PER-TRADE calculator, nothing more:
  1. Decide a dollar amount you're willing to lose if the stop-loss is
     hit: `riskAmount = balance * risk_pct_per_trade / 100`.
  2. The stop's distance from entry (as a % of entry) tells you how
     large a position needs to be so that hitting the stop actually
     costs exactly `riskAmount`:
         positionNotional = riskAmount / stopDistancePct
  3. Leverage does NOT change that risk in dollar terms - the position
     size (and therefore the dollar loss on a stop-out) is the same
     whether it's opened at 3x or 20x. What leverage changes is how
     much margin (collateral) is locked up to hold that position:
         marginRequired = positionNotional / leverage
     So this picks the smallest leverage (the pair's configured
     minimum, or the global default) that still leaves enough margin
     headroom in the wallet - i.e. it never suggests more leverage
     than necessary. Only if the wallet genuinely can't cover the
     margin at that floor does it raise leverage further (capped at
     `max_leverage`), and if even that isn't enough it shrinks the
     position to fit instead of ever suggesting more leverage than the
     configured ceiling.

Nothing here reads a real exchange balance or places/modifies/closes
any order - `wallet_balance` is only ever the number the user typed
into the "Wallet Balance" button (see bot/handlers/wallet_balance.py
and bot/state_store.py). This is a sizing suggestion only, not
financial advice and not a guarantee against loss.
"""
from __future__ import annotations

import math

DEFAULT_RISK_PCT_PER_TRADE = 2.0
DEFAULT_MIN_LEVERAGE = 3
DEFAULT_MAX_LEVERAGE = 20


def _min_leverage_for(symbol: str | None, mm_cfg: dict) -> int:
    per_pair = mm_cfg.get("min_leverage_by_pair") or {}
    global_min = mm_cfg.get("min_leverage", DEFAULT_MIN_LEVERAGE)
    if symbol and symbol in per_pair:
        return per_pair[symbol]
    return global_min


def compute_money_management(
    wallet_balance: float | None,
    entry: float | None,
    stop_loss: float | None,
    symbol: str | None,
    mm_cfg: dict | None,
) -> dict | None:
    """
    Returns None when there isn't enough to compute from - no saved
    balance yet, or the trade plan is missing an entry/stop-loss (some
    verdicts, e.g. NEUTRAL/WAIT, don't have a tradePlan at all).
    Otherwise returns a dict consumed by
    bot.formatters.format_money_management_block():

        balance          - the wallet balance used (float)
        leverage          - suggested leverage, an int
        positionNotional  - suggested position size in USDT (float)
        marginRequired     - USDT margin the position needs at that
                              leverage (float)
        actualRiskAmount   - USDT actually at risk if the stop hits
                              (float) - equals the target risk amount
                              unless `capped` is True
        actualRiskPct      - actualRiskAmount as a % of balance
        targetRiskPct      - the configured risk_pct_per_trade
        capped             - True if the wallet couldn't cover the
                              margin even at max_leverage, so the
                              position (and therefore the risk) had to
                              be shrunk below target
    """
    mm_cfg = mm_cfg or {}

    if not wallet_balance or wallet_balance <= 0:
        return None
    if not entry or not stop_loss or entry <= 0:
        return None

    stop_distance_pct = abs(entry - stop_loss) / entry
    if stop_distance_pct <= 0:
        return None

    target_risk_pct = mm_cfg.get("risk_pct_per_trade", DEFAULT_RISK_PCT_PER_TRADE)
    max_leverage = mm_cfg.get("max_leverage", DEFAULT_MAX_LEVERAGE)
    min_leverage = _min_leverage_for(symbol, mm_cfg)
    # A misconfigured settings.yaml (min > max) shouldn't produce a
    # nonsensical suggestion - keep the floor from exceeding the ceiling.
    min_leverage = min(min_leverage, max_leverage)

    risk_amount = wallet_balance * (target_risk_pct / 100.0)
    position_notional = risk_amount / stop_distance_pct

    leverage = min_leverage
    margin_required = position_notional / leverage
    capped = False

    if margin_required > wallet_balance:
        # The wallet can't cover the margin at the pair's minimum
        # leverage - raise leverage just enough to fit, never past the
        # configured ceiling.
        needed_leverage = position_notional / wallet_balance
        leverage = min(max_leverage, math.ceil(needed_leverage))
        margin_required = position_notional / leverage

        if margin_required > wallet_balance:
            # Even at max leverage the position doesn't fit - shrink
            # the position (and therefore the risk) to what the wallet
            # can actually margin, rather than ever exceeding
            # max_leverage.
            leverage = max_leverage
            position_notional = wallet_balance * leverage
            margin_required = wallet_balance
            capped = True

    actual_risk_amount = position_notional * stop_distance_pct
    actual_risk_pct = (actual_risk_amount / wallet_balance) * 100.0

    return {
        "balance": wallet_balance,
        "leverage": leverage,
        "positionNotional": position_notional,
        "marginRequired": margin_required,
        "actualRiskAmount": actual_risk_amount,
        "actualRiskPct": actual_risk_pct,
        "targetRiskPct": target_risk_pct,
        "capped": capped,
    }


# =========================================================================
# --- Live Trade money management (Trade ID / Active a Trade system) ---
# A DIFFERENT calculator from compute_money_management() above - that
# one is a risk-% sizing SUGGESTION shown alongside every signal.
# This one is what "Active a Trade" actually uses: the WHOLE saved
# Wallet Balance as margin, at the pair's REAL max leverage (fetched
# live from Bitget, not a settings.yaml ceiling) - i.e. what would
# genuinely happen if this trade were opened right now with everything
# you've got. See engine/bitget_api.py's fetch_bitget_futures_contract_config().
# =========================================================================

def compute_live_trade_plan(wallet_balance: float | None, entry: float | None, stop_loss: float | None,
                             tp1: float | None, tp2: float | None, tp3: float | None,
                             verdict: str, contract_info: dict | None) -> dict | None:
    """
    Returns None only when there's nothing to compute from at all (no
    balance, no entry). Otherwise always returns a full breakdown -
    including when the position would come out BELOW the pair's
    minimum order size (belowMinSize=True) - so the caller can show
    "you need at least X USDT for this pair" instead of just silently
    failing, which is exactly the "shows 0" problem being fixed here.

        leverage          - the pair's real max leverage (int), or
                             DEFAULT_MAX_LEVERAGE if Bitget's contract
                             config couldn't be read for this pair
        positionNotional  - balance * leverage, in USDT
        quantity          - positionNotional / entry, in the base asset
        belowMinSize      - True if quantity is under the pair's
                             minTradeSize (position too small to place)
        minTradeSize      - that pair's minimum order size, if known
        minUsdtNeeded     - the smallest balance that WOULD clear
                             minTradeSize at this leverage, if known
        slUsdt / slPct    - USDT and % (of balance) result if SL hits
        tp1Usdt / tp1Pct, tp2Usdt / tp2Pct, tp3Usdt / tp3Pct - same,
                             for each target that exists
    """
    if not wallet_balance or wallet_balance <= 0 or not entry or entry <= 0:
        return None

    contract_info = contract_info or {}
    leverage = contract_info.get("maxLeverage") or DEFAULT_MAX_LEVERAGE
    min_trade_size = contract_info.get("minTradeSize")

    position_notional = wallet_balance * leverage
    quantity = position_notional / entry

    below_min_size = min_trade_size is not None and quantity < min_trade_size
    min_usdt_needed = (min_trade_size * entry / leverage) if (min_trade_size and leverage) else None

    def _pnl_at(target_price):
        if target_price is None or target_price <= 0:
            return None, None
        pct_move = (target_price - entry) / entry if verdict == "BUY" else (entry - target_price) / entry
        usdt = position_notional * pct_move
        pct_of_balance = (usdt / wallet_balance) * 100.0
        return usdt, pct_of_balance

    sl_usdt, sl_pct = _pnl_at(stop_loss)
    tp1_usdt, tp1_pct = _pnl_at(tp1)
    tp2_usdt, tp2_pct = _pnl_at(tp2)
    tp3_usdt, tp3_pct = _pnl_at(tp3)

    return {
        "balance": wallet_balance,
        "leverage": leverage,
        "positionNotional": position_notional,
        "quantity": quantity,
        "belowMinSize": below_min_size,
        "minTradeSize": min_trade_size,
        "minUsdtNeeded": min_usdt_needed,
        "slUsdt": sl_usdt, "slPct": sl_pct,
        "tp1Usdt": tp1_usdt, "tp1Pct": tp1_pct,
        "tp2Usdt": tp2_usdt, "tp2Pct": tp2_pct,
        "tp3Usdt": tp3_usdt, "tp3Pct": tp3_pct,
    }


def pnl_at_price(position_notional: float, entry: float, balance: float, verdict: str, price: float) -> tuple:
    """
    Live/floating USDT + % P/L for an already-open live-balance trade,
    at whatever the CURRENT market price is - same math as the
    per-target calculation inside compute_live_trade_plan(), just
    exposed standalone since the current price isn't known until the
    trade is already active (jobs/signal_outcome_tracker.py and the
    Trade Information detail view both need this after the fact, not
    only at the TP/SL levels planned up front).
    """
    if not entry or entry <= 0 or not balance or balance <= 0:
        return None, None
    pct_move = (price - entry) / entry if verdict == "BUY" else (entry - price) / entry
    usdt = position_notional * pct_move
    pct_of_balance = (usdt / balance) * 100.0
    return usdt, pct_of_balance