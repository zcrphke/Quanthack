"""
strategy_engine.py — Signal computation for 3 validated strategies.

Imported by multi_pairs_server.py for inline signal computation,
and importable standalone for backtesting.
"""
import numpy as np
import pandas as pd

# -----------------------------------------------------------------------
# Strategy 1: FX Carry (MA-filtered)
# -----------------------------------------------------------------------
CARRY_DIR = {"USDJPY": 1, "USDCAD": 1, "AUDUSD": 1, "GBPUSD": -1}

def fx_carry_signal(sym, prices_h1, ma_bars=48):
    """
    Return +1 (long), -1 (short), 0 (flat) for a carry symbol.
    Enters carry direction only when price confirms via MA filter.
    """
    carry = CARRY_DIR.get(sym, 0)
    if carry == 0 or len(prices_h1) < ma_bars + 2:
        return 0
    ma    = float(np.mean(prices_h1[-ma_bars-1:-1]))
    price = float(prices_h1[-1])
    if carry ==  1: return 1 if price > ma else 0
    if carry == -1: return -1 if price < ma else 0
    return 0

# -----------------------------------------------------------------------
# Strategy 2: Metals Trend (ATR trailing stop — stateful)
# -----------------------------------------------------------------------

def metals_trend_signal(sym, prices_daily, state, lookback=48, atr_mult=1.5):
    """
    Stateful: state dict is mutated to track position, entry, stop.
    Returns current position: +1, -1, or 0.
    Call every bar. State keys: {sym}_pos, {sym}_entry, {sym}_stop.
    """
    if len(prices_daily) < lookback + 15:
        return 0

    price = float(prices_daily[-1])
    atr   = float(np.mean(np.abs(np.diff(prices_daily[-15:]))))
    pos   = float(state.get(f"{sym}_pos",   0.0))
    stop  = state.get(f"{sym}_stop", None)

    # Check trailing stop hit
    if pos == 1.0 and stop is not None and price < stop:
        state[f"{sym}_pos"] = 0.0; state[f"{sym}_stop"] = None
        return 0
    if pos == -1.0 and stop is not None and price > stop:
        state[f"{sym}_pos"] = 0.0; state[f"{sym}_stop"] = None
        return 0

    # Trail the stop while in position
    if pos == 1.0:
        new_stop = price - atr_mult * atr
        state[f"{sym}_stop"] = max(stop or 0.0, new_stop)
    elif pos == -1.0:
        new_stop = price + atr_mult * atr
        state[f"{sym}_stop"] = min(stop or 1e9, new_stop)

    # New entry on breakout
    if pos == 0.0:
        hi = float(np.max(prices_daily[-lookback-1:-1]))
        lo = float(np.min(prices_daily[-lookback-1:-1]))
        if price > hi:
            state[f"{sym}_pos"]   =  1.0
            state[f"{sym}_entry"] = price
            state[f"{sym}_stop"]  = price - atr_mult * atr
        elif price < lo:
            state[f"{sym}_pos"]   = -1.0
            state[f"{sym}_entry"] = price
            state[f"{sym}_stop"]  = price + atr_mult * atr

    return float(state.get(f"{sym}_pos", 0.0))

# -----------------------------------------------------------------------
# Strategy 3: Vol Expansion Breakout (stateful)
#
# Originally validated on crypto (BTC/ETH/SOL/XRP). Re-validated on
# EURUSD/USDCHF/EURGBP/EURCHF after the carry-direction reconsideration
# for these four found NO honest carry edge (the original "carry" pass
# was a methodology bug — both LONG and SHORT passed simultaneously,
# which is the signature of curve-fit MA-direction-picking, not a real
# yield-differential signal). Vol expansion breakout, the SAME mechanism
# already validated on crypto, transfers cleanly: both-halves robust,
# 12-21 OOS trades per symbol, modest proportional returns (0.04-0.12%),
# no implausible Sharpe numbers. This is the same strategy applied to a
# wider symbol set, NOT a new independently-diversifying edge — these FX
# additions carry the same kind of risk as the crypto sleeve, just on
# different instruments. Sized accordingly (modest) in the coordinator.
# -----------------------------------------------------------------------

# Per-symbol validated parameters. Three of four FX additions converge on
# the same params as crypto (1.2x / 2 days); EURCHF wants a wider 1.8x
# threshold — convergence across independently-swept symbols is itself
# evidence the signal is real rather than overfit per-instrument.
VOL_BREAKOUT_PARAMS = {
    # original crypto sleeve
    "BTCUSD": (1.2, 2), "ETHUSD": (1.2, 2), "SOLUSD": (1.2, 2), "XRPUSD": (1.2, 2),
    # reconsidered FX additions
    "EURUSD": (1.2, 2), "USDCHF": (1.2, 2), "EURGBP": (1.2, 2), "EURCHF": (1.8, 2),
}

def crypto_vol_signal(sym, prices_daily, state, vol_mult=1.2, vol_lb=10, hold_days=2):
    """
    Stateful: state dict is mutated to track position and hold count.
    Returns current position: +1, -1, or 0.
    State keys: {sym}_pos, {sym}_hold.
    """
    if len(prices_daily) < vol_lb + 2:
        return 0

    today_rng = abs(prices_daily[-1] - prices_daily[-2]) / (prices_daily[-2] + 1e-12)
    avg_rng   = float(np.mean([
        abs(prices_daily[-i-1] - prices_daily[-i-2]) / (prices_daily[-i-2] + 1e-12)
        for i in range(vol_lb)
    ]))

    pos  = float(state.get(f"{sym}_pos",  0.0))
    hold = int(state.get(f"{sym}_hold", 0))

    # Exit after hold period
    if pos != 0:
        hold += 1
        state[f"{sym}_hold"] = hold
        if hold >= hold_days:
            state[f"{sym}_pos"]  = 0.0
            state[f"{sym}_hold"] = 0
            return 0

    # New entry on vol expansion
    if pos == 0 and today_rng > vol_mult * avg_rng:
        sig = 1.0 if prices_daily[-1] > prices_daily[-2] else -1.0
        state[f"{sym}_pos"]  = sig
        state[f"{sym}_hold"] = 0
        return sig

    return pos
