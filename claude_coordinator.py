"""
claude_coordinator.py — Claude API coordination for 3-strategy netting system

Claude's raw JSON output is validated through ClaudeDecision (models.py)
before it ever reaches strategy sizing. A malformed or out-of-bounds
response (e.g. sizing=15.0, a missing field, markdown-fenced JSON) is
rejected and the deterministic fallback is used instead — the same
"can only de-risk, never silently misbehave" guarantee the risk
supervisor enforces, applied one layer earlier.

OBSERVABILITY (Pydantic Logfire):
Every coordination cycle is wrapped in a Logfire span recording the
input context, the validated ClaudeDecision (as structured attributes,
not a string blob), and whether the response was accepted or fell back.
instrument_anthropic() additionally captures the raw API call — prompt,
completion, token counts, latency — automatically. This gives a full,
queryable audit trail of every decision Claude made during the
competition, which is both genuinely useful for debugging live and the
artifact referenced in the technology-prize submission.

Logfire is entirely optional: if LOGFIRE_TOKEN is not set, configure()
runs in local-only mode (spans are created but not shipped anywhere),
so the system behaves identically with or without it. Nothing about
strategy logic depends on Logfire being configured.
"""
import json, os, re, logging
import anthropic
from pydantic import ValidationError
from models import ClaudeDecision

log = logging.getLogger(__name__)

try:
    import logfire
    logfire.configure(
        token=os.environ.get("LOGFIRE_TOKEN"),   # None -> local-only, never crashes
        service_name="syphonix-trader",
        send_to_logfire="if-token-present",
    )
    logfire.instrument_anthropic()   # auto-traces every Claude API call (prompt/completion/tokens/cost)
    _LOGFIRE_ON = True
except Exception as e:
    log.warning(f"Logfire unavailable, continuing without it: {e}")
    _LOGFIRE_ON = False

SYSTEM_PROMPT = """
You coordinate 3 validated trading strategy GROUPS for a $1M competition.

STRATEGIES (all validated on one-year data, OOS walk-forward confirmed):

1. FX CARRY (OOS Sharpe=24.52)
   Long: USDJPY, USDCAD, AUDUSD  |  Short: GBPUSD
   Only enters when price confirms carry direction (48h MA filter)
   Very high Sharpe, very low drawdown — the CORE strategy

2. METALS TREND (OOS Sharpe=2.64 gold / 2.32 silver)
   Long gold/silver on 48-day breakout, exit on 1.5×ATR trailing stop
   Directional — good return but higher volatility than carry

3. VOL EXPANSION BREAKOUT (OOS Sharpe=4.06-7.15, 8 symbols pass both-halves)
   Enter when today's range > vol_mult × 10-day average range (per-symbol
   threshold, mostly 1.2x). Hold 2 days then exit. Selective — only fires
   on genuine vol expansion.
   Symbols: BTCUSD, ETHUSD, SOLUSD, XRPUSD (crypto)
            EURUSD, USDCHF, EURGBP, EURCHF (FX — added after these failed
            as carry candidates; vol-expansion is the SAME mechanism as
            the crypto sleeve here, not an independent edge)
   IMPORTANT: this is ONE sizing dial (crypto_breakout) controlling BOTH
   the crypto AND FX legs of vol breakout. They share the same risk
   character — sizing this dial up increases exposure to ALL 8 symbols
   simultaneously, not just crypto. Be mindful this is your single
   largest symbol-count strategy (8 of 14 traded instruments).

COMPETITION SCORING: 70% Return + 15% Drawdown + 10% Sharpe + 5% Risk Discipline

DECISION LOGIC:

IF rank <= 20 (WINNING):
  DEFENSIVE — protect Sharpe rank
  fx_carry: 0.8x  |  metals: 0.3x  |  crypto_breakout: 0.1x

IF rank 21-100 (CONTENDING):
  BALANCED — steady return, manage risk
  fx_carry: 1.0x  |  metals: 0.5x  |  crypto_breakout: 0.3x

IF rank >= 101 (BEHIND):
  AGGRESSIVE — need return to qualify
  fx_carry: 1.3x  |  metals: 1.0x  |  crypto_breakout: 0.6x

RISK GUARDRAILS (NEVER VIOLATE — competition rules):
  Margin sustained >90% for 30min  → -20pts penalty
  Leverage sustained >28x for 30min → -20pts penalty
  Single instrument >90% for 30min  → -10pts penalty
  30% drawdown                       → ELIMINATED

Note: FX carry is the highest-Sharpe validated strategy.
Always run it at or near full sizing unless margin/leverage constraints force otherwise.
Crypto and metals are return-boosters — Claude scales them based on competitive position.

OUTPUT JSON ONLY:
{
  "stance": "DEFENSIVE|BALANCED|AGGRESSIVE",
  "fx_carry":        {"sizing": 0.5-1.5},
  "metals_trend":    {"sizing": 0.2-1.2},
  "crypto_breakout": {"sizing": 0.1-0.8},
  "reasoning": "2-3 sentences",
  "confidence": 0.0-1.0
}
"""

class ClaudeCoordinator:
    def __init__(self):
        key = os.environ.get("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(api_key=key) if key else None

    def coordinate(self, context):
        if not _LOGFIRE_ON:
            return self._coordinate_inner(context)
        with logfire.span(
            "claude_coordinate",
            rank=context.get("rank"),
            equity=context.get("equity"),
            hours_to_cutoff=context.get("hours_to_cutoff"),
            margin_pct=context.get("margin_pct"),
            leverage=context.get("leverage"),
        ) as span:
            decision = self._coordinate_inner(context)
            span.set_attribute("stance", decision["stance"])
            span.set_attribute("fx_carry_sizing", decision["fx_carry"]["sizing"])
            span.set_attribute("metals_trend_sizing", decision["metals_trend"]["sizing"])
            span.set_attribute("crypto_breakout_sizing", decision["crypto_breakout"]["sizing"])
            span.set_attribute("reasoning", decision.get("reasoning", ""))
            span.set_attribute("used_fallback", decision.get("reasoning", "").startswith("Fallback"))
            return decision

    def _coordinate_inner(self, context):
        if not self.client:
            return self._fallback(context)
        try:
            msg = self._build_message(context)
            resp = self.client.messages.create(
                model="claude-opus-4-8",
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role":"user","content":msg}]
            )
            text = resp.content[0].text
            decision = self._validate(text)
            log.info(f"Claude: {decision['stance']} "
                     f"carry={decision['fx_carry']['sizing']:.2f}x "
                     f"metals={decision['metals_trend']['sizing']:.2f}x "
                     f"crypto={decision['crypto_breakout']['sizing']:.2f}x")
            return decision
        except Exception as e:
            log.warning(f"Claude error: {e}, using fallback")
            if _LOGFIRE_ON:
                logfire.warning("claude_coordinate_fallback", error=str(e), error_type=type(e).__name__)
            return self._fallback(context)

    @staticmethod
    def _validate(text: str) -> dict:
        """
        Parse + validate Claude's raw text through ClaudeDecision.
        Strips markdown code fences if present (```json ... ```), then
        requires every field to satisfy the bounds declared in models.py
        (sizing ranges, stance enum, confidence in [0,1]). A response that
        fails validation raises — caller falls back to the deterministic
        rank-based sizing rather than trusting unvalidated output.
        """
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        raw = json.loads(cleaned)
        try:
            decision = ClaudeDecision.model_validate(raw)
        except ValidationError as e:
            log.warning(f"Claude response failed schema validation, rejecting: {e}")
            raise
        return decision.to_legacy_dict()

    def _build_message(self, ctx):
        return (f"Rank: {ctx.get('rank')}/thousands | Equity: ${ctx.get('equity'):,.0f} | "
                f"Hours to cutoff: {ctx.get('hours_to_cutoff')}\n"
                f"Margin: {ctx.get('margin_pct')}% | Leverage: {ctx.get('leverage')}x | "
                f"Max DD: {ctx.get('max_dd')}%\n\n"
                f"Decide sizing for: fx_carry, metals_trend, crypto_breakout.\n"
                f"Respond with ONLY valid JSON.")

    def _fallback(self, ctx):
        rank = ctx.get('rank', 500)
        if rank <= 20:
            carry, metals, crypto = 0.8, 0.3, 0.1
            stance = "DEFENSIVE"
        elif rank <= 100:
            carry, metals, crypto = 1.0, 0.5, 0.3
            stance = "BALANCED"
        else:
            carry, metals, crypto = 1.3, 1.0, 0.6
            stance = "AGGRESSIVE"
        return {
            "stance": stance,
            "fx_carry":        {"sizing": carry},
            "metals_trend":    {"sizing": metals},
            "crypto_breakout": {"sizing": crypto},
            "reasoning": f"Fallback: rank={rank}, stance={stance}",
            "confidence": 0.5
        }
