"""
risk_supervisor.py — Claude-powered risk supervisor for the 3-strategy system.

Runs as a secondary check AFTER Claude coordinator decides sizing.
Receives the full book state and can SCALE DOWN or VETO if risk is too high.
Cannot increase sizing — multiplier is clamped to [0, 1].

THREE STRATEGIES MONITORED:
  1. FX Carry  (AUDUSD/GBPUSD/USDCAD/USDJPY)
  2. Metals Trend (XAUUSD/XAGUSD)
  3. Crypto Vol Breakout (BTC/ETH/SOL/XRP)

COMPETITION RULES ENFORCED:
  - Margin > 90% sustained 30min  → -20pts
  - Leverage > 28x sustained 30min → -20pts
  - Single instrument > 90% sustained 30min → -10pts
  - 30% drawdown → ELIMINATED
"""
import json, os, re, logging
from pydantic import ValidationError
from models import RiskVerdict

log = logging.getLogger(__name__)

try:
    import logfire
    _LOGFIRE_ON = True
except Exception:
    _LOGFIRE_ON = False

MULT_MIN = 0.0
MULT_MAX = 1.0
MODEL = os.environ.get("RISK_MODEL", "claude-opus-4-8")
RISK_SUPERVISOR_ON = os.environ.get("RISK_SUPERVISOR", "1") == "1"

_SYSTEM = """You are a risk supervisor for a $1M trading competition account.

THREE ACTIVE STRATEGIES:
1. FX Carry       — long USDJPY/USDCAD/AUDUSD, short GBPUSD (highest Sharpe, core)
2. Metals Trend   — long/short XAUUSD/XAGUSD on 48-day breakout with ATR stop
3. Crypto VolBO   — BTC/ETH/SOL/XRP vol breakout, hold 2 days

COMPETITION ELIMINATION RULES:
- 30% drawdown → instant elimination (avoid at ALL costs)
- Margin > 90% sustained 30min → -20pts penalty
- Leverage > 28x sustained 30min → -20pts penalty
- Single instrument > 90% of book sustained 30min → -10pts penalty

YOUR ONLY JOB: review the current book state and decide whether to APPROVE, SCALE, or VETO.
- APPROVE: all good, proceed at full sizing
- SCALE: reduce by multiplier (e.g. 0.5 = halve all sizes)
- VETO: flatten everything (multiplier = 0)

You can ONLY reduce risk, never increase it. Multiplier must be in [0, 1].
Reserve VETO for: drawdown approaching 25%, margin > 85%, leverage > 24x.
Reserve SCALE for: elevated risk (DD > 4%, margin 70-85%, concentration building).
Most cycles should be APPROVE.

Respond with ONLY valid JSON, no prose:
{"action":"APPROVE"|"SCALE"|"VETO","multiplier":0.0-1.0,"reason":"one sentence"}
"""

def _build_state_message(state):
    """Format book state as clear text for Claude."""
    return f"""Competition state:
  Round: {state.get('round', '?')} | Hours to cutoff: {state.get('hours_to_cutoff', '?')}
  Rank: {state.get('rank', '?')} | Equity: ${state.get('equity', 0):,.0f}

Risk metrics:
  Daily drawdown:      {state.get('daily_dd_pct', 0):.2f}% (limit: {state.get('daily_dd_limit_pct', 5):.1f}%)
  Max drawdown:        {state.get('max_dd_pct', 0):.2f}% (elimination at 30%)
  Margin used:         {state.get('margin_pct', 0):.1f}% (penalty threshold: 90%)
  Gross leverage:      {state.get('gross_lev', 0):.1f}x (penalty threshold: 28x)
  Largest instrument:  {state.get('max_instrument_pct', 0):.1f}% of book (penalty: >90%)

Active positions:
  FX carry open:       {state.get('n_carry', 0)} symbols
  Metals open:         {state.get('n_metals', 0)} symbols
  Crypto open:         {state.get('n_crypto', 0)} symbols
  Total open:          {state.get('n_open', 0)} symbols

Proposed gross exposure: {state.get('proposed_gross_lev', 0):.2f}x

Decide: APPROVE, SCALE (with multiplier), or VETO."""


def _fallback(state):
    """Deterministic fallback when Claude is off or unavailable."""
    dd       = state.get('daily_dd_pct', 0)
    dd_lim   = state.get('daily_dd_limit_pct', 5.0)
    max_dd   = state.get('max_dd_pct', 0)
    margin   = state.get('margin_pct', 0)
    lev      = state.get('gross_lev', 0)

    # Elimination firewall
    if max_dd >= 25:
        return {"action":"VETO","multiplier":0.0,
                "reason":"fallback: max drawdown >=25%, near elimination threshold"}
    if margin >= 87:
        return {"action":"VETO","multiplier":0.0,
                "reason":"fallback: margin >=87%, approaching 90% penalty zone"}
    if lev >= 25:
        return {"action":"VETO","multiplier":0.0,
                "reason":"fallback: leverage >=25x, approaching 28x penalty zone"}

    # Scale down on elevated risk
    mult = 1.0; reason = "fallback: nominal"
    if dd >= 0.75 * dd_lim:
        mult = min(mult, 0.3); reason = f"fallback: daily DD {dd:.1f}% near limit"
    elif dd >= 0.5 * dd_lim:
        mult = min(mult, 0.6); reason = f"fallback: daily DD {dd:.1f}% elevated"
    if margin >= 75:
        mult = min(mult, 0.5); reason = f"fallback: margin {margin:.0f}% elevated"
    if lev >= 20:
        mult = min(mult, 0.6); reason = f"fallback: leverage {lev:.1f}x elevated"

    action = "APPROVE" if mult == 1.0 else "SCALE"
    return {"action":action, "multiplier":mult, "reason":reason}


def _parse(text):
    """
    Parse Claude's risk verdict and validate through RiskVerdict.
    The model_validator on RiskVerdict already enforces that VETO forces
    multiplier=0 and APPROVE forces multiplier=1, so a malformed verdict
    (e.g. Claude says VETO but reports multiplier=0.6) is corrected
    rather than silently propagated into position sizing.
    """
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m: raise ValueError("no JSON in response")
    raw = json.loads(m.group(0))
    try:
        verdict = RiskVerdict.model_validate({
            "action": str(raw.get("action", "APPROVE")).upper(),
            "multiplier": raw.get("multiplier", 1.0),
            "reason": str(raw.get("reason", ""))[:200],
            "source": "validated",
        })
    except ValidationError as e:
        log.warning(f"risk verdict failed schema validation, rejecting: {e}")
        raise
    return {"action": verdict.action.value, "multiplier": verdict.multiplier,
            "reason": verdict.reason}


def review(state):
    """
    Main entry point. Call this after Claude coordinator decides sizing
    but before sending positions to the EA.

    state dict should include:
      equity, daily_dd_pct, daily_dd_limit_pct, max_dd_pct,
      margin_pct, gross_lev, max_instrument_pct, proposed_gross_lev,
      n_carry, n_metals, n_crypto, n_open, rank, round, hours_to_cutoff

    Returns: {action, multiplier, reason, source}
    """
    if not _LOGFIRE_ON:
        return _review_inner(state)
    with logfire.span(
        "risk_supervisor_review",
        rank=state.get("rank"),
        equity=state.get("equity"),
        daily_dd_pct=state.get("daily_dd_pct"),
        max_dd_pct=state.get("max_dd_pct"),
        margin_pct=state.get("margin_pct"),
        gross_lev=state.get("gross_lev"),
        proposed_gross_lev=state.get("proposed_gross_lev"),
    ) as span:
        r = _review_inner(state)
        span.set_attribute("action", r["action"])
        span.set_attribute("multiplier", r["multiplier"])
        span.set_attribute("reason", r["reason"])
        span.set_attribute("source", r["source"])
        if r["action"] == "VETO":
            logfire.warning("risk_supervisor_veto", reason=r["reason"],
                            max_dd_pct=state.get("max_dd_pct"), margin_pct=state.get("margin_pct"))
        return r


def _review_inner(state):
    if not RISK_SUPERVISOR_ON:
        r = _fallback(state); r["source"] = "fallback(off)"; return r

    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=MODEL,
            max_tokens=150,
            system=_SYSTEM,
            messages=[{"role":"user","content":_build_state_message(state)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b,"type","")=="text")
        r = _parse(text)
        r["source"] = MODEL
        log.info(f"risk_supervisor: {r['action']} mult={r['multiplier']:.2f} — {r['reason']}")
        return r
    except Exception as e:
        r = _fallback(state)
        r["source"] = f"fallback({type(e).__name__})"
        log.warning(f"risk_supervisor fallback: {e}")
        return r


if __name__ == "__main__":
    # Self-test (no API key needed — tests fallback logic)
    tests = [
        ("nominal",     {"daily_dd_pct":0.3,"daily_dd_limit_pct":5.0,"max_dd_pct":0.5,
                         "margin_pct":20,"gross_lev":3.0,"proposed_gross_lev":3.5}),
        ("dd elevated", {"daily_dd_pct":3.0,"daily_dd_limit_pct":5.0,"max_dd_pct":4.0,
                         "margin_pct":40,"gross_lev":5.0,"proposed_gross_lev":5.5}),
        ("near limit",  {"daily_dd_pct":4.2,"daily_dd_limit_pct":5.0,"max_dd_pct":8.0,
                         "margin_pct":60,"gross_lev":8.0,"proposed_gross_lev":9.0}),
        ("danger",      {"daily_dd_pct":0.1,"daily_dd_limit_pct":5.0,"max_dd_pct":26.0,
                         "margin_pct":88,"gross_lev":26.0,"proposed_gross_lev":27.0}),
    ]
    for name, state in tests:
        r = _fallback(state)
        print(f"{name:15s} → {r['action']:7s} mult={r['multiplier']:.2f}  {r['reason']}")
