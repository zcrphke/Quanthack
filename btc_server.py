"""
btc_server.py — BTC-Only Multi-Timeframe Confidence-Filtered Strategy

DESIGN:
  Single instrument: BTCUSD
  Signal: weighted composite of M5(50%), H1(35%), H3(15%) momentum
  Filter: 70% confidence t-test on recent returns before entry
  Exit: stop-loss (2%) OR signal reversal, whichever comes first
  Sizing: ~$5,500 notional (~0.085 lots at BTC=$65,000)

TARGETS:
  Sharpe >= 0.2 (measured every 15 min by competition)
  Max drawdown <= 0.02% ($200 on $1M account)
  Loss recovery: from ~-$1,000 back to positive

WHY THIS WORKS:
  - 0.0055x leverage keeps worst-case loss ~$110 even on 2% BTC move
  - Multi-timeframe composite filters noise better than single TF
  - t-test gate ensures we only enter when recent price action gives
    statistically significant directional evidence (70% confidence)
  - Signal reversal exit prevents holding losing trades
  - Sharpe 0.2 is achievable with ~14% consistent edge per 15-min period

ALL OTHER STRATEGIES ARE DISABLED. This server trades BTCUSD only.
Flat everything else immediately on startup.
"""
from flask import Flask, jsonify, request
from datetime import datetime, timezone
import numpy as np, logging, os, json
from scipy import stats

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)
CORS = {'Access-Control-Allow-Origin': '*'}

# ============================================================================
# STRATEGY PARAMETERS
# ============================================================================
SYM = "BTCUSD"

# Multi-timeframe weights (must sum to 1.0)
TF_WEIGHTS = {"M5": 0.50, "H1": 0.35, "H3": 0.15}

# Confidence filter: one-tailed t-test, 70% confidence = t >= 0.524
CONFIDENCE_THRESHOLD = 0.70
T_STAT_THRESHOLD = 0.524    # scipy.stats.t.ppf(0.70, df=large) ≈ 0.524
LOOKBACK_BARS = 20          # number of recent returns to t-test

# Entry: composite signal must exceed this threshold in [-1, 1]
SIGNAL_THRESHOLD = 0.3      # moderate filter, not too hair-trigger

# Sizing: target ~$5,500 notional to keep max DD within 0.02% = $200
TARGET_NOTIONAL = float(os.environ.get("BTC_NOTIONAL", "5500"))
STOP_PCT = float(os.environ.get("BTC_STOP_PCT", "2.0"))  # % of entry price

# Momentum lookback per timeframe (in bars of that TF)
MOMENTUM_LB = {"M5": 12, "H1": 8, "H3": 4}

# ============================================================================
# STATE
# ============================================================================
STATE = {
    "pos": 0.0,           # current position in lots (+ve long, -ve short)
    "entry_price": None,  # price at entry
    "entry_time": None,   # UTC timestamp at entry
    "stop_price": None,   # current stop level
    "equity_curve": [],
    "decision_log": [],
    "last_rebalance": None,
    "pnl_15min": [],      # list of 15-min P&L snapshots for Sharpe tracking
    "last_equity_snap": None,
    "last_snap_time": None,
}

# ============================================================================
# SIGNAL FUNCTIONS
# ============================================================================

def momentum_signal(prices, lookback):
    """
    Simple momentum signal in [-1, +1].
    Positive = recent trend up, negative = trend down.
    Uses normalised return over lookback window.
    """
    if len(prices) < lookback + 1:
        return 0.0
    recent = prices[-lookback:]
    ret = (recent[-1] - recent[0]) / recent[0]
    # Normalise by rolling std to get a z-score-like signal
    all_rets = np.diff(prices[-lookback*3:]) / prices[-lookback*3:-1] if len(prices) >= lookback*3 else np.diff(prices) / prices[:-1]
    vol = np.std(all_rets) if len(all_rets) > 2 else 0.01
    z = ret / (vol * np.sqrt(lookback)) if vol > 0 else 0.0
    # Clip to [-1, 1] using tanh (smooth)
    return float(np.tanh(z))


def composite_signal(m5_prices, h1_prices, h3_prices):
    """
    Weighted composite of M5, H1, H3 momentum signals.
    Returns score in [-1, +1].
    > SIGNAL_THRESHOLD -> long
    < -SIGNAL_THRESHOLD -> short
    else -> flat
    """
    s_m5 = momentum_signal(m5_prices, MOMENTUM_LB["M5"])
    s_h1 = momentum_signal(h1_prices, MOMENTUM_LB["H1"])
    s_h3 = momentum_signal(h3_prices, MOMENTUM_LB["H3"])

    score = (TF_WEIGHTS["M5"] * s_m5 +
             TF_WEIGHTS["H1"] * s_h1 +
             TF_WEIGHTS["H3"] * s_h3)

    log.debug(f"signals M5={s_m5:.3f} H1={s_h1:.3f} H3={s_h3:.3f} -> composite={score:.3f}")
    return score, {"M5": s_m5, "H1": s_h1, "H3": s_h3}


def confidence_filter(recent_returns, direction):
    """
    One-tailed t-test: are the last LOOKBACK_BARS returns
    statistically positive (for long) or negative (for short)
    at 70% confidence?

    direction: +1 for long (test if mean > 0), -1 for short (test if mean < 0)

    Returns (passes: bool, t_stat: float, confidence: float)
    """
    if len(recent_returns) < 5:
        return False, 0.0, 0.0

    rets = np.array(recent_returns[-LOOKBACK_BARS:])
    if direction == -1:
        rets = -rets  # flip: test if negative returns are positively significant

    n = len(rets)
    mean_r = np.mean(rets)
    std_r = np.std(rets, ddof=1)

    if std_r < 1e-10 or mean_r <= 0:
        return False, 0.0, 0.0

    t_stat = mean_r / (std_r / np.sqrt(n))
    # One-tailed p-value
    p_value = 1 - stats.t.cdf(t_stat, df=n-1)
    confidence = 1 - p_value

    passes = t_stat >= T_STAT_THRESHOLD and confidence >= CONFIDENCE_THRESHOLD
    return passes, t_stat, confidence


def compute_stop(entry_price, direction):
    """Stop price at STOP_PCT% adverse from entry."""
    if direction == 1:
        return entry_price * (1 - STOP_PCT / 100)
    else:
        return entry_price * (1 + STOP_PCT / 100)


def target_lots(btc_price):
    """Convert target notional to lots."""
    if btc_price <= 0:
        return 0.0
    return round(TARGET_NOTIONAL / btc_price, 4)

# ============================================================================
# REBALANCE
# ============================================================================

@app.route('/rebalance', methods=['POST'])
def rebalance():
    try:
        req = request.get_json()
        equity = float(req.get('equity', 1_000_000))
        symbols = req.get('symbols', {})
        time_utc = datetime.now(timezone.utc).isoformat()
        STATE['last_rebalance'] = time_utc

        # Persist equity for Sharpe tracking (15-min snapshots)
        _update_equity_snapshot(equity, time_utc)

        # Get BTC price data for all three timeframes
        btc_data = symbols.get(SYM, {})
        m5_prices  = np.array(btc_data.get('m5',  []), dtype=float)
        h1_prices  = np.array(btc_data.get('h1',  []), dtype=float)
        h3_prices  = np.array(btc_data.get('h3',  []), dtype=float)
        cur_price  = float(btc_data.get('price', 0.0))

        if cur_price <= 0 or len(m5_prices) < 10:
            log.warning(f"BTC: insufficient data (price={cur_price}, m5_bars={len(m5_prices)})")
            return _build_response(STATE['pos']), 200, CORS

        # ----------------------------------------------------------------
        # STEP 1: Check stop loss on existing position
        # ----------------------------------------------------------------
        if STATE['pos'] != 0 and STATE['stop_price'] is not None:
            stop_hit = (STATE['pos'] > 0 and cur_price <= STATE['stop_price']) or \
                       (STATE['pos'] < 0 and cur_price >= STATE['stop_price'])
            if stop_hit:
                pnl = STATE['pos'] * (cur_price - STATE['entry_price'])
                log.info(f"STOP HIT: BTC {'long' if STATE['pos']>0 else 'short'} "
                         f"entry={STATE['entry_price']:.2f} stop={STATE['stop_price']:.2f} "
                         f"cur={cur_price:.2f} pnl=${pnl:.2f}")
                _log_decision("STOP", 0.0, cur_price, pnl, {})
                STATE['pos'] = 0.0
                STATE['entry_price'] = None
                STATE['stop_price'] = None

        # ----------------------------------------------------------------
        # STEP 2: Compute composite signal
        # ----------------------------------------------------------------
        score, tf_signals = composite_signal(m5_prices, h1_prices, h3_prices)

        # Determine intended direction from signal
        if score > SIGNAL_THRESHOLD:
            intended = 1
        elif score < -SIGNAL_THRESHOLD:
            intended = -1
        else:
            intended = 0

        # ----------------------------------------------------------------
        # STEP 3: Check signal reversal exit
        # ----------------------------------------------------------------
        if STATE['pos'] != 0:
            current_dir = 1 if STATE['pos'] > 0 else -1
            if intended != 0 and intended != current_dir:
                pnl = STATE['pos'] * (cur_price - STATE['entry_price']) if STATE['entry_price'] else 0
                log.info(f"SIGNAL REVERSAL: closing {'long' if current_dir>0 else 'short'} "
                         f"(score={score:.3f} -> {intended:+d}) pnl=${pnl:.2f}")
                _log_decision("REVERSAL", 0.0, cur_price, pnl, tf_signals)
                STATE['pos'] = 0.0
                STATE['entry_price'] = None
                STATE['stop_price'] = None
            elif intended == 0:
                # Signal gone neutral — hold existing position, don't exit
                log.debug(f"Signal neutral ({score:.3f}), holding position")
                _update_state_log(equity, score, tf_signals, "HOLD", 0)
                return _build_response(STATE['pos']), 200, CORS

        # ----------------------------------------------------------------
        # STEP 4: Entry — only if flat and signal is clear
        # ----------------------------------------------------------------
        if STATE['pos'] == 0 and intended != 0:
            # Build recent returns for confidence filter
            # Use M5 prices (most data points) for the t-test
            if len(m5_prices) >= LOOKBACK_BARS + 1:
                recent_rets = list(np.diff(m5_prices[-LOOKBACK_BARS-1:]) /
                                   m5_prices[-LOOKBACK_BARS-1:-1])
            else:
                recent_rets = []

            passes, t_stat, confidence = confidence_filter(recent_rets, intended)

            if passes:
                lots = target_lots(cur_price)
                stop = compute_stop(cur_price, intended)
                STATE['pos'] = lots * intended
                STATE['entry_price'] = cur_price
                STATE['stop_price'] = stop
                STATE['entry_time'] = time_utc
                direction_str = "LONG" if intended == 1 else "SHORT"
                log.info(f"ENTRY {direction_str}: BTC @ {cur_price:.2f} "
                         f"lots={lots:.4f} notional=${lots*cur_price:,.0f} "
                         f"stop={stop:.2f} "
                         f"t={t_stat:.3f} confidence={confidence:.1%} "
                         f"score={score:.3f}")
                _log_decision("ENTRY", STATE['pos'], cur_price, 0, tf_signals)
            else:
                log.info(f"ENTRY BLOCKED: confidence={confidence:.1%} < {CONFIDENCE_THRESHOLD:.0%} "
                         f"(t={t_stat:.3f}, need {T_STAT_THRESHOLD:.3f}) score={score:.3f}")
                _log_decision("BLOCKED", 0.0, cur_price, 0, tf_signals)

        # Hold existing position — update trailing state
        elif STATE['pos'] != 0:
            log.debug(f"HOLDING {'long' if STATE['pos']>0 else 'short'}: "
                      f"entry={STATE['entry_price']:.2f} cur={cur_price:.2f} "
                      f"stop={STATE['stop_price']:.2f} score={score:.3f}")

        _update_state_log(equity, score, tf_signals, "OK", intended)
        return _build_response(STATE['pos']), 200, CORS

    except Exception as e:
        log.error(f"rebalance error: {e}", exc_info=True)
        return "error", 500, CORS


# ============================================================================
# HELPERS
# ============================================================================

def _build_response(btc_lots):
    """
    Wire format for the EA: flat all 13 old symbols, BTC at target.
    ALL OTHER SYMBOLS ARE ALWAYS ZERO.
    """
    all_syms = ["AUDUSD","GBPUSD","USDCAD","USDJPY",
                "XAUUSD","XAGUSD",
                "BTCUSD","ETHUSD","SOLUSD","XRPUSD",
                "EURUSD","USDCHF","EURGBP","EURCHF"]
    resp = "version,1,halt,0\n"
    for sym in all_syms:
        val = btc_lots if sym == SYM else 0.0
        resp += f"{sym},{val:.4f}\n"
    return resp


def _update_equity_snapshot(equity, time_utc):
    """Track 15-minute equity snapshots for Sharpe computation."""
    now = datetime.now(timezone.utc)
    STATE.setdefault('last_snap_time', None)
    STATE.setdefault('last_equity_snap', equity)
    STATE.setdefault('pnl_15min', [])

    last = STATE['last_snap_time']
    if last is None:
        STATE['last_snap_time'] = now
        STATE['last_equity_snap'] = equity
        return

    mins_elapsed = (now - last).total_seconds() / 60
    if mins_elapsed >= 15:
        prev_eq = STATE['last_equity_snap']
        ret_15min = (equity - prev_eq) / prev_eq if prev_eq > 0 else 0.0
        STATE['pnl_15min'].append(ret_15min)
        STATE['last_snap_time'] = now
        STATE['last_equity_snap'] = equity
        # Keep last 200 snapshots
        if len(STATE['pnl_15min']) > 200:
            STATE['pnl_15min'] = STATE['pnl_15min'][-200:]


def _compute_sharpe():
    """Annualised Sharpe from 15-min return snapshots."""
    rets = STATE.get('pnl_15min', [])
    if len(rets) < 3:
        return None
    r = np.array(rets)
    if np.std(r) < 1e-12:
        return None
    return float(np.mean(r) / np.std(r) * np.sqrt(4 * 252))  # 4 fifteen-min per hour, 252 days


def _log_decision(action, pos, price, pnl, tf_signals):
    STATE.setdefault('decision_log', [])
    STATE['decision_log'].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "pos": pos,
        "price": price,
        "pnl": round(pnl, 2),
        "tf_signals": tf_signals,
    })
    if len(STATE['decision_log']) > 500:
        STATE['decision_log'] = STATE['decision_log'][-500:]


def _update_state_log(equity, score, tf_signals, status, intended):
    STATE['last_decision'] = {
        "equity": equity,
        "score": round(score, 4),
        "intended": intended,
        "tf_signals": tf_signals,
        "pos": STATE['pos'],
        "entry_price": STATE['entry_price'],
        "stop_price": STATE['stop_price'],
        "status": status,
        "time": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# DASHBOARD ENDPOINTS
# ============================================================================

@app.route('/health')
def health():
    return jsonify({"status": "ok",
                    "timestamp": datetime.now(timezone.utc).isoformat()}), 200, CORS


@app.route('/state')
def get_state():
    sharpe = _compute_sharpe()
    return jsonify({
        **STATE,
        "live_sharpe": round(sharpe, 4) if sharpe else None,
        "target_sharpe": 0.2,
        "target_notional": TARGET_NOTIONAL,
        "stop_pct": STOP_PCT,
        "n_15min_snapshots": len(STATE.get('pnl_15min', [])),
    }), 200, CORS


@app.route('/metrics')
def get_metrics():
    sharpe = _compute_sharpe()
    ec = STATE.get('equity_curve', [])
    eq = ec[-1]['equity'] if ec else None
    pos = STATE.get('pos', 0.0)
    return jsonify({
        "equity": eq,
        "pos_lots": pos,
        "pos_notional": abs(pos) * (STATE.get('last_decision', {}).get('price', 0) or 0),
        "entry_price": STATE.get('entry_price'),
        "stop_price": STATE.get('stop_price'),
        "live_sharpe": round(sharpe, 4) if sharpe else None,
        "n_snapshots": len(STATE.get('pnl_15min', [])),
        "last_score": STATE.get('last_decision', {}).get('score'),
    }), 200, CORS


@app.route('/decision_log')
def get_decision_log():
    return jsonify(STATE.get('decision_log', [])), 200, CORS


@app.route('/curve')
def get_curve():
    return jsonify(STATE.get('equity_curve', [])), 200, CORS


@app.route('/rank', methods=['POST'])
def update_rank():
    body = request.get_json() or {}
    STATE['rank'] = int(body.get('rank', STATE.get('rank', 500)))
    return jsonify({'rank': STATE['rank']}), 200, CORS


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8003))
    log.info(f"BTC-only server starting on port {port}")
    log.info(f"Target notional: ${TARGET_NOTIONAL:,} | Stop: {STOP_PCT}% | "
             f"Confidence: {CONFIDENCE_THRESHOLD:.0%} | "
             f"Weights: M5={TF_WEIGHTS['M5']} H1={TF_WEIGHTS['H1']} H3={TF_WEIGHTS['H3']}")
    app.run(host="0.0.0.0", port=port, debug=False)
