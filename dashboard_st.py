"""
dashboard_st.py — Live dashboard for the 4-strategy Syphonix trading system.

Shows:
  - Live status + equity / PnL / rank header
  - Claude's latest decision (stance, sizing per strategy, reasoning)
  - Open positions (target lots) and per-strategy signal decisions
  - Rolling Claude decision log
  - Equity curve
  - Full competition rules + risk parameters reference panel

Run:  streamlit run dashboard_st.py
Auto-refreshes every 5 seconds.
"""
import os
import requests
import pandas as pd
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except Exception:
    _HAS_AUTOREFRESH = False

SERVER = os.environ.get("DASH_SERVER", "http://127.0.0.1:8003")

st.set_page_config(page_title="Syphonix Live", page_icon="⚡", layout="wide",
                   initial_sidebar_state="expanded")

if _HAS_AUTOREFRESH:
    st_autorefresh(interval=5000, key="autorefresh")

st.markdown("""
<style>
[data-testid="stMetric"]{background:#141925;border:1px solid #262d3d;border-radius:10px;padding:12px 16px}
[data-testid="stMetricValue"]{font-size:24px!important;font-weight:700}
</style>
""", unsafe_allow_html=True)


def fetch(path):
    try:
        r = requests.get(SERVER + path, timeout=3)
        return r.json() if r.ok else None
    except Exception:
        return None


with st.sidebar:
    st.header("Competition Rules")
    st.markdown("""
**Scoring**
`70% Return + 15% Drawdown + 10% Sharpe + 5% Risk Discipline`

Sharpe is **non-annualised**, from 15-min equity returns: `mean(r)/std(r)`.

**Elimination**
- 30% margin level -> forced liquidation -> instant elimination

**Risk-discipline penalties** (sustained 30 min)
- Leverage > 28x -> -20 pts
- Margin > 90% -> -20 pts
- Single instrument > 90% -> -10 pts

**Schedule (BST)**
- Launch 21 Jun 22:00
- Round 1 ends 22 Jun
- Round 2 ends 23 Jun
- Round 3 ends 24 Jun
- Finals 24-26 Jun (blinded leaderboard)
- Awards 27 Jun

**Best Sharpe Award:** $10k - Finals + Top 50 + no red-line + >=30 trades
""")

    st.header("Risk Parameters")
    st.markdown("""
**Per-instrument stops** (data-driven, last 7 days)
- FX: 1.5%
- BTC 6.0% / ETH 6.9% / XAU 6.5%
- SOL 10.4% / XRP 10.5% / XAG 10.8%

**Guardrails**
- Catastrophic stop: 8% equity / position
- Max instrument weight: 25% of gross
- Max hold: 48h FX / 5d daily
- Local daily DD failsafe: 5%

**Sizing**
- RISK_FRACTION 0.75
- MAX_GROSS_LEVERAGE 6.0
""")

    st.header("Strategy Arsenal")
    st.markdown("""
**FX Carry** (hourly, 4 symbols)
USDJPY / USDCAD / AUDUSD long, GBPUSD short
Filter: 48h MA confirms carry direction
OOS Sharpe: 24.52

**Metals Trend** (daily, 2 symbols)
XAUUSD / XAGUSD — 48-day breakout entry
Exit: 1.5x ATR trailing stop
OOS Sharpe: 2.64 / 2.32

**Crypto Vol Breakout** (daily, 4 symbols)
BTC / ETH / SOL / XRP
Entry: range > 1.2x 10-day avg, hold 2 days
OOS Sharpe: 5.66-6.31
""")


metrics   = fetch("/metrics")
claude    = fetch("/claude")
positions = fetch("/positions")
dlog      = fetch("/decision_log")
curve     = fetch("/curve")

st.title("Syphonix - 4-Strategy Live Dashboard")

if metrics is None:
    st.error(f"Cannot reach server at {SERVER} - is multi_pairs_server.py running?")
    st.stop()

since = metrics.get("since_post_sec")
if since is None:
    st.info("No rebalance posted yet - waiting for EA to connect.")
elif since > 600:
    st.warning(f"STALE - last rebalance {since//60}m ago")
else:
    ago = f"{since}s ago" if since < 90 else f"{since//60}m ago"
    st.success(f"LIVE - last rebalance {ago}")

c1, c2, c3, c4, c5, c6 = st.columns(6)
equity = metrics.get("equity")
pnl = metrics.get("pnl_pct")
c1.metric("Equity", f"${equity:,.0f}" if equity else "-")
c2.metric("PnL", f"{pnl:+.3f}%" if pnl is not None else "-")
c3.metric("Rank", metrics.get("rank") or "-")
c4.metric("Claude Stance", metrics.get("stance") or "-")
c5.metric("Open Positions", metrics.get("n_open", 0))
next_call = metrics.get("claude_secs_until_next_call")
interval = metrics.get("claude_interval_sec")
if next_call is not None:
    c6.metric("Next Claude Call", f"{int(next_call)}s", help=f"Interval: {int(interval)}s — strategy signals still run every cycle, only AI sizing is throttled")
else:
    c6.metric("Next Claude Call", "-")

st.divider()

left, right = st.columns([1, 1])

with left:
    st.subheader("Claude's Latest Decision")
    if claude and claude.get("sizing"):
        sizing = claude["sizing"]
        sdf = pd.DataFrame([
            {"Strategy": "FX Carry",           "Sizing": f"{sizing.get('fx_carry', 0):.2f}x"},
            {"Strategy": "Metals Trend",        "Sizing": f"{sizing.get('metals_trend', 0):.2f}x"},
            {"Strategy": "Crypto Vol Breakout", "Sizing": f"{sizing.get('crypto_breakout', 0):.2f}x"},
        ])
        st.dataframe(sdf, hide_index=True, use_container_width=True)
        conf = claude.get("confidence")
        if conf is not None:
            st.caption(f"Confidence: {conf}")
        if claude.get("reasoning"):
            st.info(claude["reasoning"])
    else:
        st.caption("No Claude decision yet (using fallback sizing until first cycle).")

with right:
    st.subheader("Equity Curve")
    if curve and len(curve) > 1:
        cdf = pd.DataFrame(curve)
        cdf["t"] = pd.to_datetime(cdf["t"])
        cdf = cdf.set_index("t")
        st.line_chart(cdf["equity"], height=240)
    else:
        st.caption("Equity curve builds as rebalances arrive.")

st.divider()

st.subheader("Open Positions (target lots)")
if positions and positions.get("lots"):
    lots = positions["lots"]
    gross = sum(abs(v) for v in lots.values()) or 1.0
    rows = []
    for sym, lot in sorted(lots.items(), key=lambda x: -abs(x[1])):
        rows.append({
            "Instrument": sym,
            "Target Lots": f"{lot:+.2f}",
            "Side": "LONG" if lot > 0 else "SHORT",
            "% of Gross": f"{abs(lot)/gross*100:.1f}%",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
else:
    st.caption("No open positions.")

st.subheader("Strategy Signals This Cycle")
if positions and positions.get("decisions"):
    decs = positions["decisions"]
    rows = []
    for d in decs:
        rows.append({
            "Strategy":  d.get("strategy", ""),
            "Symbol":    d.get("symbol") or d.get("pair") or d.get("instrument") or "",
            "Signal":    d.get("signal", ""),
            "Action":    d.get("action", ""),
            "Stop":      f"{d['stop']:.2f}" if d.get("stop") else "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
else:
    st.caption("No signals this cycle.")

st.divider()

st.subheader("Claude Decision Log")
if dlog:
    rows = []
    for d in reversed(dlog[-40:]):
        sz = d.get("sizing", {})
        rows.append({
            "Time (UTC)": d.get("time_utc", "")[11:19],
            "Stance":     d.get("stance", ""),
            "Equity":     f"${d.get('equity', 0):,.0f}",
            "Carry":      f"{sz.get('fx_carry', 0):.2f}x",
            "Metals":     f"{sz.get('metals_trend', 0):.2f}x",
            "Crypto":     f"{sz.get('crypto_breakout', 0):.2f}x",
            "Carry#":     d.get("n_carry", 0),
            "Metals#":    d.get("n_metals", 0),
            "Crypto#":    d.get("n_crypto", 0),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
else:
    st.caption("Decision log builds as Claude makes calls.")

st.caption(f"Server: {SERVER} | Auto-refresh: {'on (5s)' if _HAS_AUTOREFRESH else 'off - pip install streamlit-autorefresh'}")
