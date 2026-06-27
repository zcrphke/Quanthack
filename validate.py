"""validate.py — shim for backwards compatibility."""
from config import COST_BPS, STRESS_COST_BPS  # noqa: F401
from pairs_config import (                      # noqa: F401
    FX_CARRY_SYMS, METALS_SYMS, CRYPTO_SYMS,
    CARRY_DIR, CARRY_MA, METALS_LB, METALS_ATR_MULT,
    CRYPTO_VOL_MULT, CRYPTO_HOLD, RISK_FRACTION,
)
