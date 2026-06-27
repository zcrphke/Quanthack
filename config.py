"""
config.py — Settings for the 3-strategy Syphonix trading system.

VALIDATED STRATEGIES:
  1. FX Carry       (AUDUSD/GBPUSD/USDCAD/USDJPY)   OOS Sharpe=24.52
  2. Metals Trend   (XAUUSD/XAGUSD)                  OOS Sharpe=2.64/2.32
  3. Crypto VolBO   (BTC/ETH/SOL/XRP)                OOS Sharpe=5.66-6.31
"""

# -----------------------------------------------------------------------
# Instrument costs (one-way, bps) — used in research/backtest only
# -----------------------------------------------------------------------
COST_BPS = {
    "fx":     0.6,   # FX carry (tight spreads on majors)
    "metals": 1.5,   # gold/silver spreads
    "crypto": 4.0,   # crypto wider spreads
}
STRESS_COST_BPS = {
    "fx":     1.2,
    "metals": 3.0,
    "crypto": 8.0,
}

# -----------------------------------------------------------------------
# Strategy parameters (validated on one-year data)
# -----------------------------------------------------------------------
CARRY_MA_BARS       = 48      # H1 bars for MA filter
METALS_LOOKBACK     = 48      # daily bars for breakout
METALS_ATR_MULT     = 1.5     # ATR multiplier for trailing stop
CRYPTO_VOL_MULT     = 1.2     # vol expansion threshold
CRYPTO_HOLD_DAYS    = 2       # crypto position hold duration

# -----------------------------------------------------------------------
# Live risk limits
# -----------------------------------------------------------------------
MAX_DAILY_DD_PCT    = 5.0     # EA local failsafe: flatten if daily DD exceeds this
MAX_TOTAL_DD_PCT    = 10.0    # server-side alert threshold (competition eliminates at 30%)
MARGIN_WARN_PCT     = 80.0    # warn when margin approaches 90% penalty zone
LEVERAGE_WARN_X     = 22.0    # warn when leverage approaches 28x penalty zone

# -----------------------------------------------------------------------
# Competition scoring (for Claude context)
# -----------------------------------------------------------------------
SCORE_WEIGHTS = {
    "return":       0.70,
    "drawdown":     0.15,
    "sharpe":       0.10,
    "discipline":   0.05,
}
ELIMINATION_DD_PCT  = 30.0    # forced liquidation threshold
MARGIN_PENALTY_PCT  = 90.0    # sustained >30min → -20pts
LEVERAGE_PENALTY_X  = 28.0    # sustained >30min → -20pts
CONCENTRATION_PCT   = 90.0    # single instrument sustained >30min → -10pts

# -----------------------------------------------------------------------
# File paths (env-overridable for Northflank deployment)
# -----------------------------------------------------------------------
import os
STATE_FILE = os.environ.get("STATE_FILE", "mpairs_state.json")
LOG_FILE   = os.environ.get("LOG_FILE",   "mpairs_trader.log")
