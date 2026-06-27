"""
pairs_config.py — Revised 4-Strategy Configuration (Netting Account)

VALIDATED STRATEGIES (one-year data, walk-forward OOS):

  1. FX CARRY:        OOS Sharpe=24.52  Ret=+0.71%  DD=0.01%
  2. METALS TREND:    OOS Sharpe=2.64 (gold) / 2.32 (silver)
  3. VOL BREAKOUT:    OOS Sharpe=5.66-7.15 (8 symbols pass both-halves)
     - Original crypto sleeve: BTCUSD, ETHUSD, SOLUSD, XRPUSD
     - Reconsidered FX additions: EURUSD, USDCHF, EURGBP, EURCHF
       (first tested as carry candidates — that test had a methodology
       bug letting the backtest pick whichever direction looked good;
       honest re-test found no carry edge but found vol-expansion
       breakout, the SAME mechanism as crypto, transfers cleanly.
       Same risk character as the crypto sleeve, not a new diversifying
       edge — sized accordingly in the coordinator.)

REMOVED (failed netting simulation):
  - FX Mean Reversion (z-score) — negative OOS on netting account
  - FX Trend Following (EMA) — negative OOS on netting account
  - "Carry" on EURUSD/USDCHF/EURGBP/EURCHF — methodology bug, no real edge
"""
import os

# Strategy symbols
FX_CARRY_SYMS  = ["AUDUSD","GBPUSD","USDCAD","USDJPY"]
METALS_SYMS    = ["XAUUSD","XAGUSD"]
CRYPTO_SYMS    = ["BTCUSD","ETHUSD","SOLUSD","XRPUSD"]
FX_VOLBO_SYMS  = ["EURUSD","USDCHF","EURGBP","EURCHF"]   # reconsidered FX additions
VOL_BREAKOUT_SYMS = CRYPTO_SYMS + FX_VOLBO_SYMS
ALL_SYMS       = FX_CARRY_SYMS + METALS_SYMS + VOL_BREAKOUT_SYMS

# Carry direction (+1=long, -1=short, 0=skip)
CARRY_DIR = {"USDJPY": 1, "USDCAD": 1, "AUDUSD": 1, "GBPUSD": -1}

# Strategy parameters (validated on full year, OOS confirmed)
CARRY_MA        = 48     # h — MA filter: only enter carry when price above/below this MA
METALS_LB       = 48     # days — breakout lookback for metals
METALS_ATR_MULT = 1.5    # ATR multiplier for metals trailing stop
CRYPTO_VOL_MULT = 1.2    # vol expansion multiplier (default; per-symbol overrides in strategy_engine.VOL_BREAKOUT_PARAMS)
CRYPTO_HOLD     = 2      # days to hold vol-breakout position
CRYPTO_VOL_LB   = 10     # days for baseline vol calculation

# Risk sizing (env-overridable)
# Conservative defaults — the bug fixes (decay, hold-counter, data starvation,
# phantom concentration) are all in place, but sizing is kept at the original
# safe levels. To go more aggressive later, raise RISK_FRACTION toward 0.90 and
# MAX_INSTRUMENT_WEIGHT toward 0.40 via env vars (no code change needed) once
# you've watched the fixed code behave correctly live.
# MAX_GROSS_LEVERAGE is now actually ENFORCED in the server (scales the whole
# book down if it would exceed this) — a real backstop against the 30%
# elimination drawdown, not the dead config value it used to be.
RISK_FRACTION        = float(os.environ.get("RISK_FRACTION",        "0.75"))
MAX_GROSS_LEVERAGE   = float(os.environ.get("MAX_GROSS_LEVERAGE",   "8.0"))
MAX_INSTRUMENT_WEIGHT= float(os.environ.get("MAX_INSTRUMENT_WEIGHT","0.25"))

# Per-instrument stops (data-driven from last 7 days, used in EA)
FX_STOP_PCT = {"AUDUSD":1.5,"GBPUSD":1.5,"USDCAD":1.5,"USDJPY":1.5,
               "EURUSD":1.5,"USDCHF":1.5,"EURGBP":1.5,"EURCHF":1.5}
METAL_STOP_PCT = {"XAUUSD":6.51,"XAGUSD":10.76}
CRYPTO_STOP_PCT = {"BTCUSD":6.0,"ETHUSD":6.9,"SOLUSD":10.38,"XRPUSD":10.5}

CATASTROPHIC_STOP_EQUITY_PCT = float(os.environ.get("CAT_STOP_PCT","8.0"))
MAX_HOLD_HOURS_FX    = float(os.environ.get("MAX_HOLD_HOURS_FX",    "48"))
MAX_HOLD_DAYS_DAILY  = float(os.environ.get("MAX_HOLD_DAYS_DAILY",  "5"))

# Legacy (kept so any old import doesn't crash — not used by active strategies)
PAIRS    = []
