# Quanthack — AI-Coordinated Algorithmic Trading System

An automated, multi-strategy trading system built for the **Syphonix "Model to Market" Quantitative Hack** (June 2026, London) — a live, AI-native trading competition on a simulated $1M account.

The system runs three uncorrelated trading strategies in parallel across 14 instruments, coordinated by an AI layer (Claude) that adjusts risk based on competitive standing, with defense-in-depth risk controls and full observability.

---

## Architecture

A three-tier, event-loop-driven design:

```
┌─────────────────────────────────────────────────────────────┐
│  TIER 1 — EXECUTION                                           │
│  MetaTrader 5 + Expert Advisor (MPB_Tick.mq5)                │
│  · reads live prices   · places trades   · local stops       │
└───────────────────────────┬─────────────────────────────────┘
                            │  HTTP (every ~60s)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  TIER 2 — DECISION                                            │
│  Python Flask server (multi_pairs_server.py)                 │
│  · runs 3 strategies   · sizes positions   · risk limits     │
└───────────────────────────┬─────────────────────────────────┘
                            │  (every 10 min)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  TIER 3 — INTELLIGENCE                                        │
│  Claude coordinator + AI risk supervisor                     │
│  · sets aggression by rank   · approve / scale / veto        │
└─────────────────────────────────────────────────────────────┘
```

### One cycle (every ~60 seconds)

1. The EA reads live prices and account state from MT5, sends them to the server.
2. The server validates the request (Pydantic), runs the three strategies, computes target positions.
3. Every 10 minutes, the server asks Claude how aggressive to be given the current leaderboard rank.
4. The AI risk supervisor reviews the proposed book and may approve, scale down, or veto.
5. The server returns target positions; the EA executes them with the broker.

---

## The Three Strategies

| Strategy | Instruments | Entry | Exit |
|----------|-------------|-------|------|
| **FX Carry** | AUDUSD, GBPUSD, USDCAD, USDJPY | Price confirms carry direction vs 48-hour MA | Price crosses 48h MA against position (or 1.5% stop) |
| **Metals Trend** | XAUUSD, XAGUSD | 48-day breakout (price exceeds 48-day high/low) | 1.5× ATR trailing stop |
| **Vol-Expansion Breakout** | BTC, ETH, SOL, XRP, EURUSD, USDCHF, EURGBP, EURCHF | Today's range > 1.2× the 10-day average | After 2 calendar days (or price stop) |

**Design principle:** each strategy's exit style matches its edge. Carry exits on momentum reversal (carry dies when trend reverses); metals uses a trailing stop (ride trends as far as they go); vol-breakout exits on time (the edge is short-lived). All directions and parameters were fixed by economics or walk-forward out-of-sample validation — never by in-sample curve-fitting.

---

## Risk Management (Defense in Depth)

No single safety net — several independent layers, because the competition eliminates any account at 30% drawdown:

- **Schema validation (Pydantic)** — every message across a trust boundary (EA→server, AI→server) is validated against a strict schema before being acted on. A malformed price or out-of-bounds AI sizing is rejected, not traded.
- **AI risk supervisor** — a second AI call that can only *reduce* risk: approve, scale down, or veto (flatten the book).
- **Per-instrument price stops** — 1.5% on FX, wider on crypto/metals (data-driven from recent volatility).
- **Catastrophic stop** — any single position losing 8% of equity is closed immediately.
- **Daily drawdown failsafe** — EA flattens everything at 5% daily drawdown.
- **Gross leverage cap** — the whole book is scaled down if it would exceed 8× leverage.

---

## AI Coordination

- **Coordinator (`claude_coordinator.py`)** — every 10 minutes, sends rank + risk state to Claude and receives a per-strategy sizing multiplier. Behind on the leaderboard → push; ahead → protect. Calls are throttled and cached to fit a fixed API budget over a multi-day run.
- **Risk supervisor (`risk_supervisor.py`)** — a separate AI call that reviews the proposed book and enforces hard competition limits, with a deterministic fallback if the API is unavailable.

Both AI outputs are validated through Pydantic before use — the AI proposes, the schema disposes. A hallucinated sizing value outside hard bounds is rejected and the system falls back to safe defaults.

---

## File Guide

### Core (Tier 2 — Decision)
| File | Role |
|------|------|
| `multi_pairs_server.py` | Main Flask server — runs the 3 strategies, sizing, risk limits, AI calls |
| `strategy_engine.py` | Per-symbol vol-breakout parameters and signal reference functions |
| `pairs_config.py` | Strategy parameters, sizing dials, per-instrument stops |
| `config.py` | Competition scoring weights, cost assumptions, thresholds |
| `models.py` | Pydantic schemas validating every trust boundary |
| `validate.py` | Backwards-compatibility shim for shared constants |

### Intelligence (Tier 3)
| File | Role |
|------|------|
| `claude_coordinator.py` | Claude API integration — sets per-strategy aggression by rank |
| `risk_supervisor.py` | Second AI check — approve / scale / veto the proposed book |

### Execution (Tier 1)
| File | Role |
|------|------|
| `MPB_Tick.mq5` | MetaTrader 5 Expert Advisor — 14-instrument version, sends H1 + native D1 bars, local emergency stops |

### Single-instrument variant (BTC-only)
| File | Role |
|------|------|
| `btc_server.py` | Alternative server: BTC-only, multi-timeframe (M5/H1/H3) momentum with a t-test confidence filter, ultra-low-risk sizing |
| `MPB_BTC.mq5` | BTC-only EA — sends M5/H1/H3 bars, flattens all other instruments |
| `requirements_btc.txt` | Dependencies for the BTC variant (adds SciPy for the t-test) |

### Supporting
| File | Role |
|------|------|
| `dashboard_st.py` | Streamlit dashboard — live equity, positions, AI decisions |
| `requirements.txt` | Python dependencies (3-strategy system) |
| `Dockerfile` | Container build for cloud deployment (Northflank) |

---

## Tech Stack

| Library | Role |
|---------|------|
| **Flask** | HTTP server receiving EA requests |
| **Gunicorn** | Production WSGI server (cloud deployment) |
| **NumPy** | All numerical computation (MAs, ATR, volatility) |
| **SciPy** | Statistical confidence filter (BTC variant only) |
| **Pydantic** | Schema validation at every trust boundary |
| **Anthropic** | Claude API SDK (coordinator + risk supervisor) |
| **Logfire** | Cloud observability — every AI decision logged and searchable |
| **Streamlit** | Live dashboard UI |
| **Requests** | Dashboard polling of server endpoints |

---

## Setup

### Server (local)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export LOGFIRE_TOKEN=pylf_v1_...        # optional — runs local-only if absent
export RISK_SUPERVISOR=1
export CLAUDE_INTERVAL_SEC=600           # AI call interval (seconds)

python3 multi_pairs_server.py
```

### Dashboard

```bash
streamlit run dashboard_st.py
```

### Expert Advisor

1. Open `MPB_Tick.mq5` in MetaEditor, compile, attach to a chart.
2. Set `InpServerURL` to your server address.
3. Add the server host:port to MT5's WebRequest whitelist
   (Tools → Options → Expert Advisors).

### Cloud deployment (Northflank)

Build from the `Dockerfile`. Set environment variables (`ANTHROPIC_API_KEY`,
`RISK_SUPERVISOR`, `CLAUDE_INTERVAL_SEC`, optional `LOGFIRE_TOKEN`).
Expose port 8003. Mount a persistent volume at `/data` for state.

---

## Configuration

Key dials (all env-overridable, defined in `pairs_config.py`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `RISK_FRACTION` | 0.75 | Proportional scaler on every position |
| `MAX_INSTRUMENT_WEIGHT` | 0.25 | Max share of the book in any single symbol |
| `MAX_GROSS_LEVERAGE` | 8.0 | Hard cap on total book leverage |
| `CLAUDE_INTERVAL_SEC` | 600 | Seconds between AI coordination calls |

---

## Competition Context

- **Scoring:** 70% return rank, 15% drawdown rank, 10% Sharpe rank, 5% risk discipline.
- **Elimination:** 30% margin drawdown → instant forced liquidation.
- **Penalties:** sustained margin >90%, leverage >28×, or single-instrument >90% (30+ min).

The system is tuned for *risk-managed aggression*: maximize return (the dominant scoring weight) while keeping hard guardrails between the book and the elimination line.

---

## Disclaimer

Built for a simulated trading competition. Not financial advice. Not intended for
live capital without independent validation. Trading involves substantial risk of loss.
