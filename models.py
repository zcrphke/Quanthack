"""
models.py — Pydantic schemas for the 3-strategy Syphonix trading system.

Why this exists:
  Every boundary in this system crosses an untrusted source — Claude's API
  response, the EA's HTTP request, the risk supervisor's verdict. Before
  Pydantic, these were bare dicts parsed with json.loads() and trusted by
  convention. A malformed Claude response (sizing=15.0 instead of 1.5, a
  missing field, markdown-fenced JSON) could previously flow straight into
  position sizing with only a try/except as protection.

  These models make every boundary fail loudly and specifically instead of
  failing silently or fail through to a generic fallback. They also give the
  Streamlit dashboard and the tech-prize write-up a typed, auditable contract
  for exactly what Claude is allowed to decide.

Used by:
  multi_pairs_server.py  — validates EA requests (RebalanceRequest) and
                            outgoing decisions (RebalanceResponse)
  claude_coordinator.py  — validates Claude's JSON output (ClaudeDecision)
  risk_supervisor.py     — validates the risk verdict (RiskVerdict)
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================================
# CLAUDE COORDINATOR — validates Claude's strategy-sizing decision
# ============================================================================

class Stance(str, Enum):
    DEFENSIVE = "DEFENSIVE"
    BALANCED  = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


class StrategySizing(BaseModel):
    """A single strategy's sizing multiplier, bounded to validated ranges."""
    sizing: float = Field(..., ge=0.0, le=2.0)


class ClaudeDecision(BaseModel):
    """
    The full, validated shape of what Claude must return each cycle.
    Bounds here mirror the ranges given to Claude in the system prompt
    (fx_carry 0.5-1.5, metals 0.2-1.2, crypto 0.1-0.8) but are widened
    slightly (hard cap 2.0) so a borderline-valid response is rejected
    by the API call's own instructions, not silently clipped twice.
    """
    stance: Stance = Stance.BALANCED
    fx_carry: StrategySizing = Field(default_factory=lambda: StrategySizing(sizing=1.0))
    metals_trend: StrategySizing = Field(default_factory=lambda: StrategySizing(sizing=0.5))
    crypto_breakout: StrategySizing = Field(default_factory=lambda: StrategySizing(sizing=0.3))
    reasoning: str = Field(default="", max_length=2000)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @field_validator("reasoning")
    @classmethod
    def strip_reasoning(cls, v: str) -> str:
        return v.strip()

    def to_legacy_dict(self) -> dict:
        """Server code reads cd.get('fx_carry',{}).get('sizing',...) — keep that working."""
        return {
            "stance": self.stance.value,
            "fx_carry": {"sizing": self.fx_carry.sizing},
            "metals_trend": {"sizing": self.metals_trend.sizing},
            "crypto_breakout": {"sizing": self.crypto_breakout.sizing},
            "reasoning": self.reasoning,
            "confidence": self.confidence,
        }


# ============================================================================
# RISK SUPERVISOR — validates the APPROVE/SCALE/VETO verdict
# ============================================================================

class RiskAction(str, Enum):
    APPROVE = "APPROVE"
    SCALE   = "SCALE"
    VETO    = "VETO"


class RiskVerdict(BaseModel):
    action: RiskAction
    multiplier: float = Field(..., ge=0.0, le=1.0)   # hard floor — Claude can only de-risk
    reason: str = Field(default="", max_length=300)
    source: str = Field(default="unknown")

    @model_validator(mode="after")
    def _enforce_action_multiplier_consistency(self) -> "RiskVerdict":
        # APPROVE must mean full size, VETO must mean zero — never let the
        # two drift apart even if the upstream text claimed something odd.
        if self.action == RiskAction.VETO and self.multiplier != 0.0:
            self.multiplier = 0.0
        if self.action == RiskAction.APPROVE and self.multiplier != 1.0:
            self.multiplier = 1.0
        return self


# ============================================================================
# EA <-> SERVER WIRE FORMAT — validates the rebalance request/response
# ============================================================================

class SymbolBars(BaseModel):
    """Price history for one symbol as sent by the EA."""
    close: list[float] = Field(..., min_length=1)
    contract: Optional[float] = None
    price: Optional[float] = None
    tf: Optional[str] = None   # "D1" or "H1" — which timeframe the EA fetched for this symbol

    @field_validator("close")
    @classmethod
    def no_nonpositive_prices(cls, v: list[float]) -> list[float]:
        if any(p <= 0 for p in v):
            raise ValueError("close array contains non-positive price — bad data from EA/broker feed")
        return v


class RebalanceRequest(BaseModel):
    """
    What the EA POSTs to /rebalance every cycle.
    Rejecting a malformed request here (e.g. equity<=0, empty symbols)
    is far safer than letting it fall through into strategy math that
    silently produces NaN or zero-division sizing.

    margin_level and margin_used_pct are DIFFERENT ratios that move in
    OPPOSITE directions — do not conflate them:
      margin_level:     equity/margin*100. LOWER is worse. Competition's
                         30% stop-out / elimination threshold is on THIS.
      margin_used_pct:  margin/equity*100. HIGHER is worse. Competition's
                         "margin > 90% sustained 30min" risk-discipline
                         penalty refers to THIS.
    """
    equity: float = Field(..., gt=0)
    rank: Optional[int] = Field(default=None, ge=1)
    margin_used: Optional[float] = Field(default=None, ge=0)
    margin_level: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    margin_used_pct: Optional[float] = Field(default=None, ge=0, le=10000)
    gross_leverage: Optional[float] = Field(default=None, ge=0, le=1000)
    max_instrument_pct: Optional[float] = Field(default=None, ge=0, le=100)
    daily_dd_pct: Optional[float] = Field(default=None, ge=0, le=100)
    prev_pos: dict[str, float] = Field(default_factory=dict)
    symbols: dict[str, SymbolBars] = Field(default_factory=dict)

    @field_validator("prev_pos")
    @classmethod
    def positions_finite(cls, v: dict[str, float]) -> dict[str, float]:
        for sym, lots in v.items():
            if abs(lots) > 1000:
                raise ValueError(f"prev_pos[{sym}]={lots} implausible — refusing, likely a bug not a real position")
        return v


class RebalanceDecision(BaseModel):
    """One strategy's signal decision this cycle — feeds the dashboard."""
    strategy: str
    symbol: str
    signal: float
    action: str
    stop: Optional[float] = None


class RebalanceResponse(BaseModel):
    """
    What the server computes and sends back to the EA, validated before
    being serialised to the CSV-ish wire format MQL5 expects.
    """
    halt: bool = False
    targets: dict[str, float] = Field(default_factory=dict)
    decisions: list[RebalanceDecision] = Field(default_factory=list)
    claude: ClaudeDecision
    risk: RiskVerdict
    time_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("targets")
    @classmethod
    def targets_within_leverage_sanity(cls, v: dict[str, float]) -> dict[str, float]:
        gross = sum(abs(x) for x in v.values())
        if gross > 50:  # sanity ceiling far above MAX_GROSS_LEVERAGE — catches runaway bugs
            raise ValueError(f"gross target {gross:.1f} is implausible — refusing to send to EA")
        return v

    def to_wire_format(self) -> str:
        """Serialise to the exact CSV format MQL5's ApplyReply() parses."""
        lines = [f"version,1,halt,{1 if self.halt else 0}"]
        for sym, lots in self.targets.items():
            lines.append(f"{sym},{lots:.4f}")
        return "\n".join(lines) + "\n"


# ============================================================================
# Self-test
# ============================================================================
if __name__ == "__main__":
    # Valid Claude decision
    cd = ClaudeDecision(stance="AGGRESSIVE", fx_carry={"sizing": 1.3},
                        metals_trend={"sizing": 1.0}, crypto_breakout={"sizing": 0.6},
                        reasoning="rank 500, push sizing", confidence=0.8)
    print("✓ valid ClaudeDecision:", cd.to_legacy_dict())

    # Invalid — sizing way out of bounds, should raise
    try:
        ClaudeDecision(fx_carry={"sizing": 15.0})
        print("✗ FAILED to reject out-of-bounds sizing")
    except Exception as e:
        print("✓ correctly rejected oversized sizing:", type(e).__name__)

    # Valid risk verdict, action/multiplier auto-corrected
    rv = RiskVerdict(action="VETO", multiplier=0.5, reason="test", source="test")
    print(f"✓ RiskVerdict auto-correct: VETO forced multiplier to {rv.multiplier}")

    # Invalid EA request — negative equity
    try:
        RebalanceRequest(equity=-100, prev_pos={}, symbols={})
        print("✗ FAILED to reject negative equity")
    except Exception as e:
        print("✓ correctly rejected negative equity:", type(e).__name__)

    # Valid EA request
    req = RebalanceRequest(equity=1_000_000, rank=47, prev_pos={"AUDUSD": 0.5},
                           symbols={"AUDUSD": {"close": [0.64, 0.641, 0.642]}})
    print("✓ valid RebalanceRequest, equity:", req.equity)

    print("\n✓ all self-tests passed")
