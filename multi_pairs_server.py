"""
multi_pairs_server.py — Revised 4-Strategy Engine (Netting Account)

VALIDATED STRATEGIES (from one-year data, walk-forward OOS):

  1. FX CARRY (MA-filtered)
     Symbols:  USDJPY(long), USDCAD(long), AUDUSD(long), GBPUSD(short)
     Filter:   enter only when price above/below 48h MA (momentum confirm)
     OOS:      Sharpe=24.52  Ret=+0.71%  DD=0.01%

  2. METALS TREND (ATR-trailed stop)
     Symbols:  XAUUSD, XAGUSD
     Entry:    48-day price breakout (daily bar)
     Stop:     1.5×ATR(14) trailing stop
     OOS:      Gold Sharpe=2.64  Silver Sharpe=2.32

  3. VOL EXPANSION BREAKOUT (originally "crypto vol breakout" — now multi-asset)
     Symbols:  BTCUSD, ETHUSD, SOLUSD, XRPUSD (original)
               EURUSD, USDCHF, EURGBP, EURCHF (added after reconsideration —
               see strategy_engine.py for the validation note: these 4 were
               first tested as carry candidates, that test had a methodology
               bug, and honest re-testing found NO carry edge but DID find
               this vol-expansion mechanism transfers cleanly)
     Entry:    today's range > vol_mult × rolling 10-day avg range (per-symbol
               vol_mult — see strategy_engine.VOL_BREAKOUT_PARAMS)
     Exit:     after 2 days
     OOS:      crypto Sharpe=5.66-6.31, FX additions Sharpe=4.06-7.15
               (all 8 pass both-halves robustness)

WHAT WAS REMOVED (failed netting validation):
  - FX Mean Reversion (z-score pairs) — negative OOS on netting
  - FX Trend Following (EMA crossover) — negative OOS on netting
  - Daily EMA (crypto) — superseded by vol breakout
  - "Carry" on EURUSD/USDCHF/EURGBP/EURCHF — methodology bug, no real edge found

Claude decides sizing across all three strategy GROUPS (fx_carry, metals_trend,
vol_breakout — the latter now spans both crypto and FX) based on rank and regime.
"""
from flask import Flask, jsonify, request
from datetime import datetime, timezone
import numpy as np, pandas as pd, json, logging, os
import pairs_config as PC
from claude_coordinator import ClaudeCoordinator
from strategy_engine import VOL_BREAKOUT_PARAMS
from pydantic import ValidationError
from models import RebalanceRequest
import risk_supervisor

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
CORS = {'Access-Control-Allow-Origin':'*',
        'Access-Control-Allow-Methods':'GET,POST',
        'Access-Control-Allow-Headers':'Content-Type'}

# All symbols the system trades
FX_CARRY_SYMS    = ["AUDUSD","GBPUSD","USDCAD","USDJPY"]
METALS_SYMS      = ["XAUUSD","XAGUSD"]
CRYPTO_SYMS      = ["BTCUSD","ETHUSD","SOLUSD","XRPUSD"]
FX_VOLBO_SYMS    = ["EURUSD","USDCHF","EURGBP","EURCHF"]   # reconsidered FX additions
VOL_BREAKOUT_SYMS = CRYPTO_SYMS + FX_VOLBO_SYMS
ALL_SYMS         = FX_CARRY_SYMS + METALS_SYMS + VOL_BREAKOUT_SYMS

# Carry direction: +1=long, -1=short, 0=skip
CARRY_DIR = {"USDJPY":1, "USDCAD":1, "AUDUSD":1, "GBPUSD":-1}
CARRY_MA  = 48    # hours
METALS_LB = 48    # days
METALS_ATR= 1.5   # ATR multiplier for trailing stop
VOL_LB    = 10    # days for baseline vol (shared across vol-breakout symbols)

# ----------------------------------------------------------------------------
# CLAUDE CALL THROTTLING — budget math
# The EA rebalances every ~60s (InpCheckSec), but Claude (Opus) is only
# consulted once per CLAUDE_INTERVAL_SEC; strategy signal logic (carry/
# metals/vol-breakout) still runs every rebalance using the cached sizing.
# At 10 min intervals: 864 calls over 6 days x ~$0.014/call ≈ $12 — well
# inside a $50 budget with margin for local testing before competition
# start. Override with CLAUDE_INTERVAL_SEC env var if you want it tighter
# or looser; 300s (5min) ≈ $25, 900s (15min) ≈ $8 over the full 6 days.
# ----------------------------------------------------------------------------
CLAUDE_INTERVAL_SEC = float(os.environ.get("CLAUDE_INTERVAL_SEC", "600"))  # default 10 min

STATE = {}
claude = ClaudeCoordinator()
log.info(f"Claude coordination interval: {CLAUDE_INTERVAL_SEC:.0f}s "
        f"(~{CLAUDE_INTERVAL_SEC/60:.0f} min) — strategy signals still run every cycle")

# ============================================================================
# SIGNAL COMPUTATION
# ============================================================================

def fx_carry_signal(sym, prices_h1):
    """
    Return +1 (long), -1 (short), 0 (flat) for carry symbol.
    Only in carry direction when price is above/below 48h MA.
    """
    carry = CARRY_DIR.get(sym, 0)
    if carry == 0 or len(prices_h1) < CARRY_MA + 1:
        return 0
    ma    = np.mean(prices_h1[-CARRY_MA-1:-1])
    price = prices_h1[-1]
    if carry == 1:
        return 1 if price > ma else 0
    else:
        return -1 if price < ma else 0

def metals_trend_signal(sym, prices_daily, metal_state):
    """
    Return (+1/-1/0) and updated stop for metals trend strategy.
    Entry: 48-day breakout. Stop: 1.5×ATR14 trailing.
    """
    if len(prices_daily) < METALS_LB + 14 + 1:
        return 0, None
    price = prices_daily[-1]
    atr   = float(np.mean(np.abs(np.diff(prices_daily[-15:]))))
    pos   = metal_state.get(f"{sym}_pos", 0.0)
    stop  = metal_state.get(f"{sym}_stop", None)
    entry = metal_state.get(f"{sym}_entry", price)

    # Check trailing stop
    if pos == 1.0 and stop is not None and price < stop:
        log.info(f"METALS STOP {sym}: price={price:.2f} stop={stop:.2f}")
        metal_state[f"{sym}_pos"]  = 0.0
        metal_state[f"{sym}_stop"] = None
        return 0, None
    if pos == -1.0 and stop is not None and price > stop:
        log.info(f"METALS STOP {sym}: price={price:.2f} stop={stop:.2f}")
        metal_state[f"{sym}_pos"]  = 0.0
        metal_state[f"{sym}_stop"] = None
        return 0, None

    # Trail the stop if in position
    if pos == 1.0:
        new_stop = price - METALS_ATR * atr
        metal_state[f"{sym}_stop"] = max(stop or 0, new_stop)
    elif pos == -1.0:
        new_stop = price + METALS_ATR * atr
        metal_state[f"{sym}_stop"] = min(stop or 1e9, new_stop)

    # New entry signal
    if pos == 0.0:
        hi = np.max(prices_daily[-METALS_LB-1:-1])
        lo = np.min(prices_daily[-METALS_LB-1:-1])
        if price > hi:
            metal_state[f"{sym}_pos"]   = 1.0
            metal_state[f"{sym}_entry"] = price
            metal_state[f"{sym}_stop"]  = price - METALS_ATR * atr
            log.info(f"METALS ENTRY {sym}: long @ {price:.2f} stop={metal_state[f'{sym}_stop']:.2f}")
        elif price < lo:
            metal_state[f"{sym}_pos"]   = -1.0
            metal_state[f"{sym}_entry"] = price
            metal_state[f"{sym}_stop"]  = price + METALS_ATR * atr
            log.info(f"METALS ENTRY {sym}: short @ {price:.2f} stop={metal_state[f'{sym}_stop']:.2f}")

    return float(metal_state.get(f"{sym}_pos", 0.0)), metal_state.get(f"{sym}_stop")

def vol_breakout_signal(sym, prices_daily, vb_state, today_doy=None):
    """
    Vol expansion breakout — multi-asset (crypto + 4 reconsidered FX symbols).
    Per-symbol vol_mult/hold_days from strategy_engine.VOL_BREAKOUT_PARAMS.

    HOLD TRACKING IS BY CALENDAR DAY, NOT CALL COUNT. This function is
    called every ~60s by the EA, but a position must be held for
    hold_days *days*, not hold_days *cycles*. We record the day-of-year
    at entry and exit only when that many distinct days have elapsed.
    (The earlier per-call counter collapsed "hold 2 days" into "hold 2
    minutes" — positions were force-closed almost immediately after entry.)
    """
    if len(prices_daily) < VOL_LB + 2:
        return 0
    if today_doy is None:
        today_doy = datetime.now(timezone.utc).timetuple().tm_yday
    vol_mult, hold_days = VOL_BREAKOUT_PARAMS.get(sym, (1.2, 2))
    today_range = abs(prices_daily[-1] - prices_daily[-2]) / prices_daily[-2]
    avg_range   = float(np.mean([abs(prices_daily[-i-1] - prices_daily[-i-2]) / prices_daily[-i-2]
                                  for i in range(VOL_LB)]))
    pos        = vb_state.get(f"{sym}_pos", 0.0)
    entry_doy  = vb_state.get(f"{sym}_entry_doy")

    # Exit after hold_days CALENDAR DAYS (handles year wrap with modulo)
    if pos != 0 and entry_doy is not None:
        days_held = (today_doy - entry_doy) % 366
        if days_held >= hold_days:
            log.info(f"VOLBO EXIT {sym}: held {days_held}d (limit {hold_days}d)")
            vb_state[f"{sym}_pos"]       = 0.0
            vb_state[f"{sym}_entry_doy"] = None
            return 0

    # New entry on vol expansion
    if pos == 0 and today_range > vol_mult * avg_range:
        sig = 1.0 if prices_daily[-1] > prices_daily[-2] else -1.0
        vb_state[f"{sym}_pos"]       = sig
        vb_state[f"{sym}_entry_doy"] = today_doy
        log.info(f"VOLBO ENTRY {sym}: {'long' if sig>0 else 'short'} "
                 f"range={today_range:.2%} avg={avg_range:.2%} mult={today_range/avg_range:.1f}× "
                 f"(threshold={vol_mult}x)")
        return sig

    return float(vb_state.get(f"{sym}_pos", 0.0))

# ============================================================================
# REBALANCE
# ============================================================================

@app.route('/rebalance', methods=['POST'])
def rebalance():
    global STATE
    try:
        raw_req = request.get_json()

        # Validate the EA's request through Pydantic before trusting any of it.
        # Falls back to permissive defaults only for genuinely optional fields
        # (margin/leverage may be absent on an older EA build mid-rollout);
        # equity and symbols are required and a bad request is rejected outright.
        try:
            validated = RebalanceRequest.model_validate(raw_req)
        except ValidationError as e:
            log.error(f"rebalance request failed validation, rejecting: {e}")
            return "error", 400, CORS

        equity       = validated.equity
        prev_pos     = validated.prev_pos
        symbols_data = {k: v.model_dump() for k, v in validated.symbols.items()}
        time_utc     = datetime.now(timezone.utc).isoformat()
        STATE['last_rebalance'] = time_utc

        # Persist REAL account metrics from the EA into STATE. These replace
        # the hardcoded fallback defaults (margin_pct=20, leverage=4.0, etc.)
        # that previously fed Claude fiction instead of the actual account
        # state. If the EA hasn't sent a metric yet (older build, or first
        # ever call), fall back to the last known value in STATE rather than
        # a hardcoded guess, so a single missing field doesn't reset context.
        if validated.rank is not None:
            STATE['rank'] = validated.rank
        if validated.margin_used_pct is not None:
            STATE['margin_used_pct'] = validated.margin_used_pct
        if validated.margin_level is not None:
            STATE['margin_level'] = validated.margin_level
        if validated.gross_leverage is not None:
            STATE['gross_leverage'] = validated.gross_leverage
        if validated.max_instrument_pct is not None:
            STATE['max_instrument_pct'] = validated.max_instrument_pct
        if validated.daily_dd_pct is not None:
            STATE['daily_dd_pct'] = validated.daily_dd_pct
        # Track max drawdown ever seen this run (for the elimination firewall)
        STATE['max_dd_pct'] = max(STATE.get('max_dd_pct', 0.0), STATE.get('daily_dd_pct', 0.0))

        # Get Claude decision — THROTTLED to CLAUDE_INTERVAL_SEC, not called
        # every rebalance. Strategy signal logic below still runs every
        # cycle (free, no API cost); only the Claude sizing call is cached
        # between intervals. This is what makes the $50 budget last the
        # full 6-day competition — see CLAUDE_INTERVAL_SEC for the math.
        rank = STATE.get('rank', 500)
        ctx  = {'rank': rank, 'equity': equity,
                'hours_to_cutoff': STATE.get('hours_to_cutoff', 8),
                'margin_pct': STATE.get('margin_used_pct', 20),   # REAL value once EA reports it
                'leverage': STATE.get('gross_leverage', 4.0),     # REAL value once EA reports it
                'max_dd': STATE.get('max_dd_pct', 2.0)}           # REAL value once EA reports it

        now_ts = datetime.now(timezone.utc).timestamp()
        last_call_ts = STATE.get('claude_last_call_ts', 0.0)
        cached_cd    = STATE.get('claude_cached_decision')
        due_for_call = (now_ts - last_call_ts) >= CLAUDE_INTERVAL_SEC

        if cached_cd is None or due_for_call:
            cd = claude.coordinate(ctx)
            STATE['claude_cached_decision'] = cd
            STATE['claude_last_call_ts']    = now_ts
            log.info(f"Claude: {cd.get('stance','?')} "
                     f"carry={cd.get('fx_carry',{}).get('sizing',1.0):.2f}x "
                     f"metals={cd.get('metals_trend',{}).get('sizing',0.5):.2f}x "
                     f"crypto={cd.get('crypto_breakout',{}).get('sizing',0.3):.2f}x "
                     f"rank={rank}  [FRESH — next call in {CLAUDE_INTERVAL_SEC}s]")
        else:
            cd = cached_cd
            secs_until_next = CLAUDE_INTERVAL_SEC - (now_ts - last_call_ts)
            log.debug(f"Claude: using cached decision, {secs_until_next:.0f}s until next call")

        carry_sz = float(cd.get('fx_carry',      {}).get('sizing', 1.0))
        metals_sz= float(cd.get('metals_trend',  {}).get('sizing', 0.5))
        crypto_sz= float(cd.get('crypto_breakout',{}).get('sizing', 0.3))

        # Strategy state (persisted in STATE)
        metal_state  = STATE.setdefault('metal_state', {})
        crypto_state = STATE.setdefault('crypto_state', {})
        carry_state  = STATE.setdefault('carry_state', {})

        target_abs   = {s: float(prev_pos.get(s, 0.0)) for s in ALL_SYMS}
        decisions    = []

        # ------------------------------------------------------------------
        # STRATEGY 1 — FX CARRY
        # ------------------------------------------------------------------
        for sym in FX_CARRY_SYMS:
            if sym not in symbols_data: continue
            prices_h1 = np.array(symbols_data[sym].get('close', []), dtype=float)
            if len(prices_h1) < CARRY_MA + 2: continue
            sig = fx_carry_signal(sym, prices_h1)
            prev_sig = carry_state.get(sym, 0)

            # Sync: if EA flat but we think positioned, reset
            ea_net = float(prev_pos.get(sym, 0.0))
            if prev_sig != 0 and abs(ea_net) < 1e-6:
                log.info(f"carry sync: {sym} reset (EA flat)")
                carry_state[sym] = 0; prev_sig = 0

            # CRITICAL: target_abs[sym] is recomputed FRESH from sig*sizing on
            # EVERY cycle, never left at prev_pos (the EA's already-adjusted
            # reported holding). Falling through to prev_pos here was the bug
            # that compounded RISK_FRACTION + risk_supervisor multiplier onto
            # an already-shrunk number every cycle, causing positions to decay
            # toward zero and the EA to repeatedly close/reopen on every tick.
            if sig != 0:
                carry_state[sym] = sig
                target_abs[sym]  = sig * carry_sz * 1.0
                action = 'enter' if sig != prev_sig else 'hold'
                if action == 'enter':
                    log.info(f"CARRY enter: {sym} sig={sig} sized={target_abs[sym]:.2f}")
                decisions.append({'strategy':'fx_carry','symbol':sym,
                                  'signal':sig,'action':action})
            else:
                carry_state[sym] = 0
                target_abs[sym]  = 0.0
                if prev_sig != 0:
                    decisions.append({'strategy':'fx_carry','symbol':sym,
                                      'signal':0,'action':'exit'})
                    log.info(f"CARRY exit: {sym}")

        # ------------------------------------------------------------------
        # STRATEGY 2 — METALS TREND
        # ------------------------------------------------------------------
        for sym in METALS_SYMS:
            if sym not in symbols_data: continue
            prices_raw = np.array(symbols_data[sym].get('close', []), dtype=float)
            if len(prices_raw) < 10: continue
            # EA now sends native D1 bars for metals (tf="D1") — use directly.
            # Fallback to the old stride-24 H1 approximation only if an
            # older EA build (pre-tf-field) is still connected.
            tf = symbols_data[sym].get('tf')
            if tf == 'D1':
                prices_daily = prices_raw
            else:
                stride = 24 if len(prices_raw) > 50 else 1
                prices_daily = prices_raw[::stride]
            if len(prices_daily) < METALS_LB + 14 + 1:
                log.debug(f"metals {sym}: only {len(prices_daily)} daily bars, need {METALS_LB+14+1} — skipping")
                continue

            # Sync metal state with EA
            ea_net = float(prev_pos.get(sym, 0.0))
            if metal_state.get(f"{sym}_pos", 0) != 0 and abs(ea_net) < 1e-6:
                log.info(f"metals sync: {sym} reset (EA flat — stop fired)")
                metal_state[f"{sym}_pos"] = 0.0
                metal_state[f"{sym}_stop"] = None

            sig, stop = metals_trend_signal(sym, prices_daily, metal_state)
            sized = sig * metals_sz
            target_abs[sym] = sized
            action = 'hold' if sig == carry_state.get(f"metals_{sym}", 0) else ('enter' if sig != 0 else 'exit')
            carry_state[f"metals_{sym}"] = sig
            decisions.append({'strategy':'metals_trend','symbol':sym,
                               'signal':sig,'stop':stop,'action':action})

        # ------------------------------------------------------------------
        # STRATEGY 3 — VOL EXPANSION BREAKOUT (crypto + reconsidered FX)
        # ------------------------------------------------------------------
        for sym in VOL_BREAKOUT_SYMS:
            if sym not in symbols_data: continue
            prices_raw = np.array(symbols_data[sym].get('close', []), dtype=float)
            if len(prices_raw) < VOL_LB + 3: continue
            tf = symbols_data[sym].get('tf')
            if tf == 'D1':
                prices_daily = prices_raw
            else:
                stride = 24 if len(prices_raw) > 30 else 1
                prices_daily = prices_raw[::stride]
            if len(prices_daily) < VOL_LB + 3:
                log.debug(f"volbo {sym}: only {len(prices_daily)} daily bars, need {VOL_LB+3} — skipping")
                continue

            ea_net = float(prev_pos.get(sym, 0.0))
            if crypto_state.get(f"{sym}_pos", 0) != 0 and abs(ea_net) < 1e-6:
                log.info(f"volbo sync: {sym} reset (EA flat)")
                crypto_state[f"{sym}_pos"] = 0.0
                crypto_state[f"{sym}_entry_doy"] = None

            sig = vol_breakout_signal(sym, prices_daily, crypto_state)
            # Unconditional set — never fall through to prev_pos (the EA's
            # already-adjusted reported holding). See fx_carry fix above for
            # why a conditional/near-miss check here caused compounding decay.
            target_abs[sym] = sig * crypto_sz * 0.5
            action = 'hold' if sig == carry_state.get(f"crypto_{sym}", 0) else ('enter' if sig!=0 else 'exit')
            carry_state[f"crypto_{sym}"] = sig
            if sig != 0:
                asset_class = 'crypto' if sym in CRYPTO_SYMS else 'fx'
                decisions.append({'strategy':'vol_breakout','symbol':sym,'asset_class':asset_class,
                                   'signal':sig,'action':action})

        # ------------------------------------------------------------------
        # RISK FRACTION + CONCENTRATION CLAMP
        # ------------------------------------------------------------------
        for sym in ALL_SYMS:
            target_abs[sym] = target_abs.get(sym, 0.0) * PC.RISK_FRACTION

        gross = sum(abs(v) for v in target_abs.values())
        if gross > 1e-9:
            cap = PC.MAX_INSTRUMENT_WEIGHT * gross
            for sym in target_abs:
                if abs(target_abs[sym]) > cap:
                    orig = target_abs[sym]
                    target_abs[sym] = cap if orig > 0 else -cap
                    log.info(f"clamp {sym}: {orig:+.3f} → {target_abs[sym]:+.3f}")

        # Gross leverage ceiling — NOW ENFORCED (was previously a dead config
        # value). If the summed gross book in lots exceeds MAX_GROSS_LEVERAGE,
        # scale the WHOLE book down proportionally so no single bad move can
        # run leverage toward the 30% margin-drawdown elimination line. This
        # is a hard backstop on top of the per-instrument and catastrophic
        # stops — especially important now that RISK_FRACTION is aggressive.
        gross_after_clamp = sum(abs(v) for v in target_abs.values())
        if gross_after_clamp > PC.MAX_GROSS_LEVERAGE:
            scale = PC.MAX_GROSS_LEVERAGE / gross_after_clamp
            for sym in target_abs:
                target_abs[sym] *= scale
            log.info(f"GROSS LEVERAGE CAP: book {gross_after_clamp:.2f} > "
                     f"{PC.MAX_GROSS_LEVERAGE} → scaled {scale:.3f}x to "
                     f"{sum(abs(v) for v in target_abs.values()):.2f}")

        # ------------------------------------------------------------------
        # RISK SUPERVISOR — secondary check, runs AFTER the clamp, on the
        # actual proposed book. Can only de-risk (multiplier in [0,1]),
        # never increase exposure. Throttled the same way as the Claude
        # coordinator call (own cache/interval) since this is also an
        # Opus call with its own cost — see CLAUDE_INTERVAL_SEC comment.
        # This is the elimination firewall: VETO sets the whole book to 0.
        #
        # max_inst_pct / gross_post_clamp here are computed FRESH from
        # target_abs (the book this cycle is about to propose). These are
        # preferred over STATE['max_instrument_pct'] / STATE['gross_leverage']
        # (the EA's self-reported values), because the EA's numbers describe
        # its position state as of its LAST post — one round-trip behind the
        # decision being made right now. Using a stale EA-reported value here
        # previously caused the risk supervisor to react to the wrong number
        # (e.g. a transient imbalance mid-close/reopen) instead of the actual
        # proposed book.
        # ------------------------------------------------------------------
        gross_post_clamp = sum(abs(v) for v in target_abs.values())
        max_inst_val = max((abs(v) for v in target_abs.values()), default=0.0)
        max_inst_pct = (max_inst_val / gross_post_clamp * 100.0) if gross_post_clamp > 1e-9 else 0.0

        risk_state = {
            'rank': rank, 'equity': equity,
            'round': STATE.get('round'), 'hours_to_cutoff': STATE.get('hours_to_cutoff', 8),
            'daily_dd_pct': STATE.get('daily_dd_pct', 0.0),
            'daily_dd_limit_pct': 5.0,
            'max_dd_pct': STATE.get('max_dd_pct', 0.0),
            'margin_pct': STATE.get('margin_used_pct', 0.0),
            'gross_lev': gross_post_clamp,
            'max_instrument_pct': max_inst_pct,
            'proposed_gross_lev': gross_post_clamp,
            'n_carry': len([d for d in decisions if d['strategy']=='fx_carry' and d.get('signal')]),
            'n_metals': len([d for d in decisions if d['strategy']=='metals_trend' and d.get('signal')]),
            'n_crypto': len([d for d in decisions if d['strategy']=='vol_breakout' and d.get('signal')]),
            'n_open': len([v for v in target_abs.values() if abs(v) > 1e-6]),
        }

        risk_last_ts = STATE.get('risk_last_call_ts', 0.0)
        risk_cached  = STATE.get('risk_cached_verdict')
        risk_due     = (now_ts - risk_last_ts) >= CLAUDE_INTERVAL_SEC

        if risk_cached is None or risk_due:
            verdict = risk_supervisor.review(risk_state)
            STATE['risk_cached_verdict'] = verdict
            STATE['risk_last_call_ts']   = now_ts
            log.info(f"RiskSupervisor: {verdict['action']} mult={verdict['multiplier']:.2f} "
                     f"— {verdict['reason']}  [FRESH]")
        else:
            verdict = risk_cached

        risk_mult = float(verdict.get('multiplier', 1.0))
        if risk_mult < 1.0:
            for sym in target_abs:
                target_abs[sym] *= risk_mult
            if verdict['action'] == 'VETO':
                log.warning(f"RISK VETO — book flattened to 0 ({verdict['reason']})")
            else:
                log.info(f"RISK SCALE {risk_mult:.2f}x applied to all positions ({verdict['reason']})")

        # ------------------------------------------------------------------
        # Response
        # ------------------------------------------------------------------
        resp = "version,1,halt,0\n"
        for sym in ALL_SYMS:
            resp += f"{sym},{target_abs.get(sym,0.0):.4f}\n"

        # State for dashboard
        STATE['last_decision'] = {'time_utc':time_utc,'equity':equity,
                                   'decisions':decisions,'stance':cd.get('stance','BALANCED')}
        STATE['claude'] = {'time_utc':time_utc,'stance':cd.get('stance','BALANCED'),
                           'reasoning':cd.get('reasoning',''),'confidence':cd.get('confidence'),
                           'sizing':{'fx_carry':carry_sz,'metals_trend':metals_sz,'crypto_breakout':crypto_sz},
                           'rank':rank}
        STATE['risk_supervisor'] = verdict
        STATE['lots'] = {s:round(target_abs.get(s,0.0),4) for s in ALL_SYMS if abs(target_abs.get(s,0.0))>1e-6}

        dlog = STATE.setdefault('decision_log', [])
        dlog.append({'time_utc':time_utc,'equity':round(equity,2),
                     'stance':cd.get('stance','BALANCED'),
                     'sizing':STATE['claude']['sizing'],
                     'n_carry':len([d for d in decisions if d['strategy']=='fx_carry']),
                     'n_metals':len([d for d in decisions if d['strategy']=='metals_trend']),
                     'n_crypto':len([d for d in decisions if d['strategy']=='crypto_breakout'])})
        if len(dlog)>200: STATE['decision_log']=dlog[-200:]

        ec = STATE.setdefault('equity_curve', [])
        ec.append({'t':time_utc,'equity':round(equity,2)})
        if len(ec)>500: STATE['equity_curve']=ec[-500:]

        n_open = sum(1 for v in target_abs.values() if abs(v)>1e-6)
        log.info(f"rebalance: equity={equity:,.0f} open={n_open} "
                 f"carry={len([d for d in decisions if d['strategy']=='fx_carry'])} "
                 f"metals={len([d for d in decisions if d['strategy']=='metals_trend'])} "
                 f"crypto={len([d for d in decisions if d['strategy']=='crypto_breakout'])}")
        return resp, 200, CORS

    except Exception as e:
        log.error(f"rebalance error: {e}", exc_info=True)
        return "error", 500, CORS

# ============================================================================
# DASHBOARD ENDPOINTS
# ============================================================================
@app.route('/health')
def health():
    return jsonify({'status':'ok','timestamp':datetime.now(timezone.utc).isoformat()}), 200, CORS

@app.route('/state')
def get_state():       return jsonify(STATE), 200, CORS

@app.route('/claude')
def get_claude():      return jsonify(STATE.get('claude',{})), 200, CORS

@app.route('/positions')
def get_positions():
    return jsonify({'lots':STATE.get('lots',{}),'decisions':STATE.get('last_decision',{}).get('decisions',[]),
                    'time_utc':STATE.get('last_decision',{}).get('time_utc')}), 200, CORS

@app.route('/decision_log')
def get_decision_log(): return jsonify(STATE.get('decision_log',[])), 200, CORS

@app.route('/curve')
def get_curve():        return jsonify(STATE.get('equity_curve',[])), 200, CORS

@app.route('/rank', methods=['POST'])
def update_rank():
    body = request.get_json() or {}
    STATE['rank'] = int(body.get('rank', STATE.get('rank',500)))
    log.info(f"rank updated: {STATE['rank']}")
    return jsonify({'rank':STATE['rank']}), 200, CORS

@app.route('/metrics')
def get_metrics():
    ec  = STATE.get('equity_curve',[])
    eq  = ec[-1]['equity'] if ec else None
    st  = ec[0]['equity']  if ec else None
    pnl = ((eq-st)/st*100) if (eq and st) else None
    last = STATE.get('last_rebalance')
    since = None
    if last:
        try: since=int((datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds())
        except: pass
    last_claude_ts = STATE.get('claude_last_call_ts')
    secs_since_claude = (datetime.now(timezone.utc).timestamp() - last_claude_ts) if last_claude_ts else None
    secs_until_next_claude = max(0, CLAUDE_INTERVAL_SEC - secs_since_claude) if secs_since_claude is not None else None
    return jsonify({'equity':eq,'start_equity':st,'pnl_pct':pnl,'rank':STATE.get('rank'),
                    'stance':STATE.get('claude',{}).get('stance'),'since_post_sec':since,
                    'n_open':len(STATE.get('lots',{})),
                    'claude_interval_sec':CLAUDE_INTERVAL_SEC,
                    'claude_secs_until_next_call':secs_until_next_claude}), 200, CORS

if __name__ == "__main__":
    port = int(os.environ.get("PORT",8003))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    log.info(f"starting {host}:{port} — 4-symbol-group netting strategies, "
            f"Claude every {CLAUDE_INTERVAL_SEC:.0f}s")
    app.run(host=host, port=port, debug=False)
