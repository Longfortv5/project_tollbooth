# Longfort Tactical Memory & Architecture Baseline (antigravity.md)

This document serves as the unified memory base, consolidation of recent audits, and systems architecture configuration for the Longfort trading desk.

---

## 1. Identity & Rules of Engagement

* **Role**: Chief Architect & Tactical Overwatch for the Longfort trading desk (`gemini-cli-macbook`).
* **Hierarchy**: Reports strictly to **HS** (Longfort Master).
* **Persistent Mission Parameters**:
  * **Daily Profit Target**: 200 NQ-equivalent points (Aggregate Swarm).
  * **Daily Loss Limit**: £500 GBP (Absolute Safety Rail).
  * **Steering Authority**: Fully approved. Physical steering controls via the Steering Wheel call.
* **Volatility Normalization Rates**:
  * 1.0 NQ Point = 1.0 Target Unit
  * 1.0 ES Point = ~6.33 Target Units
  * 1.0 QQQ Dollar = ~0.42 Target Units
* **Core Steering Rules**:
  * **Dumb Dispatcher**: No tactical logic in `local_dispatcher.py`; all steering is issued via the Steering Wheel call.
  * **FlashAlpha Priority**: FlashAlpha Gamma/Vanna data is the primary structural map.
  * **15m Drift Rule**: Use Bayesian HMM boundaries (-0.100% / +0.060%) to validate regime calls.
  * **NO-GO Intercept**: Block any deployment directly into heavy Gamma Walls.

---

## 2. System Architecture & Endpoints

### Remote Host: `longfort-gb10`
* **QuestDB Hub**: `100.117.239.60:9000` (Primary data storage and retrieval).
* **Sidebus Snapshot Service**: `http://127.0.0.1:8888/snapshot` (Provides real-time futures/spot pricing data).
* **PM2 Process Registry**:
  * `regime-gate` (Port `8008`): The FastAPI gateway that serves the paywalled Model Context Protocol (MCP) server.
  * `a2a-hub` (Port `8009`): Bound to `0.0.0.0:8009` to allow communication across the Tailscale mesh.
  * `qwengb10`: The local Qwen agent connected to the A2A hub.
* **Tollbooth Path**: `/home/longfort/project_tollbooth`
* **Steering Wheel Config**: `/etc/longfort/wheel_call.json`

---

## 3. FlashAlpha Option Pipeline & QQQ -> NQ Calibration

### Overview
To bypass licensing restrictions and provide real-time support on futures without raw data redistribution breaches, the system retrieves raw ETF option levels for **QQQ** from the FlashAlpha API, retrieves the live NQ futures price from the Sidebus snapshot service, and dynamically calibrates QQQ strikes and exposures to **NQ**.

### Calibration Mathematics
1. **Live Multiplier**:
   $$\text{Multiplier} = \frac{\text{NQ Spot Price}}{\text{QQQ Spot Price}}$$
   *Example*: $\text{NQ} = 28580.5$, $\text{QQQ} = 695.635 \implies \text{Multiplier} = 41.085483$
2. **Strike Scaling**:
   $$\text{NQ Strike} = \text{QQQ Strike} \times \text{Multiplier}$$
3. **Exposure Rounding**:
   All exposures are converted to rounded integers to eliminate fractional float noise.
4. **Front-Expiry Max Pain**:
   Extracted from the front-expiry `DTE = 0` options chain (`/v1/maxpain/QQQ`) rather than aggregating far-dated strikes, preventing stale drift.

### Local & Remote Scripts
* **Local Script**: `/Users/huseyinsanli/.gemini/antigravity/scratch/fetch_live_qqq_nq.py`
* **Remote Script**: `/home/longfort/project_tollbooth/fetch_live_qqq_nq.py`
* **Command to Run**:
  ```bash
  ssh longfort-gb10 "python3 /home/longfort/project_tollbooth/fetch_live_qqq_nq.py"
  ```
* **Output Payload Schema (v1.0 compliant)**:
  * Strict qualitative fields under `"regime"`.
  * `"source"` field is strictly `"Longfort"`.
  * Volatility block excludes `vix` and `vix9d` to prevent schema drift.

---

## 4. June 9, 2026 Audit & Williams %R Optimizations

### A. Performance & Strategy Comparison (Window A: Up to 11:20 AM UK)
From our audit of W%R divergence alerts, swing-pivot exits perform significantly better in trending instruments (NKD), while fixed 1:2 Risk-Reward (RR) exit brackets outperform in mean-reverting instruments (DAX, NQ).

| Strategy | Win Rate | Expected Value (EV) per Trade |
| :--- | :---: | :---: |
| **Fixed 1:1 RR** (4pt Target / 4pt Stop) | 54.9% | +0.39 pts |
| **Fixed 1:2 RR** (4pt Target / 8pt Stop) | 70.0% | +0.40 pts |
| **Swing-Pivot Exits** (Dynamic target/stop) | 40.9% | **+0.98 pts** |

### B. Asset-Specific Execution Paths
* **NKD (Nikkei 225)**: Structural trending asset. Uses **Swing-Pivot Exits**.
  * NKD Swing WR: **55.4%** | EV: **+8.50 pts** per trade.
* **DAX40 (GER40)**: Mean-reverting. Uses **Fixed 1:2 Scalping**.
  * DAX40 Swing WR: **28.4%** (Negative edge: -3.48 pts).
  * DAX40 Fixed 1:2 WR: **67.0%** | EV: **+0.40 pts**.
* **NQ (Nasdaq 100)**: Mean-reverting. Uses **Fixed 1:2 Scalping**.
  * NQ Swing WR: **41.3%** (Negative edge: -0.94 pts).
  * NQ Fixed 1:2 WR: **76.0%** | EV: **+0.40 pts**.

### C. Williams %R Threshold Rules
To maximize edge, the following filter rules are implemented in the signal processor:
1. **DAX40**:
   * Bullish: Only trade if **W%R $\le$ -90** (Boosts win rate to **87.5%**).
   * Bearish: Trade if **W%R $\ge$ -50** (Win rate **70.0%**).
2. **NKD**:
   * Bullish: Only trade if **W%R $\le$ -70** (Win rate **77.8%**, EV **+7.40 pts**). W%R $\le$ -90 raises WR to **90.9%** (EV **+12.15 pts**).
   * Bearish: Only trade if **W%R $\ge$ -10** (Win rate **40.0%**, EV **+11.21 pts**).
3. **NQ**:
   * Bullish: Only trade if **W%R $\le$ -70** (Win rate **78.6%**).
   * Bearish: Only trade if **W%R $\ge$ -10** (Win rate **81.8%**).

---

## 5. Execution Rules & Risk Mitigations

* **Post-Exit Cooldown**: A **30-second post-exit cooldown** is enforced on the trading engine (`gbgo`) to prevent immediate double-entries during volatile whip-saws.
* **Negative Gamma Multiplier**: The Go trading engine applies a **1.35x multiplier** during negative gamma regimes to automatically tighten take-profits and widen stops, accommodating the higher intraday ATR.
* **Lunch Doldrums Filter**: Standard execution blocks any 10s or 1min timeframe signals between **11:20 AM and 1:30 PM UK time** due to low-liquidity chop.
* **Licensing & Redirection Safeguard**: Direct redistribution of raw derived metrics (like option walls) to paywalled endpoints is strictly blocked. When external agents call `get_market_regime`, all numeric strikes, walls, and calibration values are sanitized out and replaced with qualitative boundaries.
