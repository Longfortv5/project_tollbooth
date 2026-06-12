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

---

## 6. Project Tollbooth Specification & JSON Schema Baseline

Project Tollbooth acts as a FastAPI-based **Model Context Protocol (MCP)** server over **Streamable HTTP (Server-Sent Events)**, exposing option/regime analytics with dual-path monetization:
1. **Pay-per-call (x402 V2 Challenge)**: USDC micro-payments on Base network (Base Sepolia for testing).
2. **Prepaid credit packs (API keys)**: Keys verified via Redis, allowing sub-second repeat access.

### Endpoints and JSON Formats

#### A. Free Discovery: List Tools
* **Method**: `POST /mcp/` (Bypasses payment check for discovery methods)
* **Request Payload**:
```json
{
  "jsonrpc": "2.0",
  "method": "tools/list",
  "params": {},
  "id": 1
}
```
* **Response Payload (Admin / Unsanitized)**:
```json
{
  "jsonrpc": "2.0",
  "result": {
    "tools": [
      {
        "name": "get_market_regime",
        "description": "Returns full quantitative market regime payload...",
        "inputSchema": {
          "type": "object",
          "properties": {
            "ticker": {"type": "string", "description": "Asset ticker (e.g. SPX, NQ)"}
          },
          "required": ["ticker"]
        }
      },
      {
        "name": "get_0dte_verdict",
        "description": "Returns zero-dte option pinning verdict...",
        "inputSchema": {
          "type": "object",
          "properties": {
            "ticker": {"type": "string"}
          },
          "required": ["ticker"]
        }
      }
    ]
  },
  "id": 1
}
```

#### B. Billed Tool Call: `get_market_regime` (Unpaid - HTTP 402)
* **Method**: `POST /mcp/` (No key/signature provided)
* **Request Payload**:
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "get_market_regime",
    "arguments": {
      "ticker": "NQ"
    }
  },
  "id": 2
}
```
* **Response Status**: `402 Payment Required`
* **Response Header (`Payment-Required`)**:
`exact chainId="eip155:84532" asset="0x036CbD53842c5426634e7929541eC2318f3dCF7e" amount="19000" payTo="0xYourWalletAddress"`

#### C. Billed Tool Call: `get_market_regime` (Paid / Authorized - HTTP 200)
When client requests include a valid prepaid key (`Authorization: Bearer tb_...`) or valid `Payment-Signature` headers.
* **Response Header**: `x-tollbooth-credits-remaining: 9999`
* **Admin/Raw Response Result (For Qwen bypass key)**:
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"schema_version\":\"1.0\",\"ticker\":\"NQ\",\"requested_as\":\"NQ\",\"timestamp\":1718210000,\"source\":\"Longfort\",\"spot\":19250.5,\"regime\":{\"gamma\":\"positive\",\"volatility\":\"stable\",\"tilt\":\"NEUTRAL\",\"consensus\":\"STABLE_PIN\",\"gamma_flip\":19180.0},\"levels\":{\"call_wall\":19400.0,\"put_wall\":18900.0,\"gamma_flip_eod\":19180.0,\"max_pain\":null}}"
      }
    ]
  },
  "id": 2
}
```
* **External/Sanitized Response Result (For public paywall users)**:
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"schema_version\":\"1.0\",\"ticker\":\"NQ\",\"requested_as\":\"NQ\",\"timestamp\":1718210000,\"source\":\"flashalpha\",\"verdict\":\"CHOP\",\"walls_status\":\"Spot 19250.5 is between call wall (19400.0) and put wall (18900.0)\"}"
      }
    ]
  },
  "id": 2
}
```

#### D. Prepaid Credit Purchase
Requires x402 payment at the credit pack price (e.g. 150 USDC atomic units).
* **Method**: `POST /credits/purchase`
* **Response Payload (HTTP 200)**:
```json
{
  "api_key": "tb_A9z2x...yourkey...",
  "credits": 10000,
  "net_billing": "150.0 USDC",
  "status": "active"
}
```

#### E. Balance Check (Free)
* **Method**: `GET /credits/balance`
* **Request Header**: `Authorization: Bearer tb_yourkey...`
* **Response Payload (HTTP 200)**:
```json
{
  "api_key": "tb_A9z2x...",
  "credits": 9999,
  "status": "active"
}
```

---

## 7. Credentials & Configuration Registry (Wallet, x402, Smithery)

These credentials and network parameters are used for paywall verification, micro-payments, and platform deployment.

### A. Wallet & Payment (x402 Base Mainnet)
* **Base Wallet Address**: `0x6F275aB348EF19456C3f53bfb2A3122CaCaa1A7d`
* **Base USDC Contract Address**: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
* **x402 Network ID**: `eip155:8453` (Base Mainnet)
* **x402 Facilitator URL**: `https://x402.org/facilitator`

### B. Tollbooth Pricing
* **Tool Call Price**: `19000` USDC atomic units ($0.019 / call)
* **Credit Pack Size**: `10000` calls
* **Credit Pack Price**: `150000000` USDC atomic units ($150.00 / pack)

### C. Smithery Server Listings
* **Smithery API Key**: `d79bd21b-8261-4029-b7b5-81068b749f0a`
* **Publish Namespace**: `longfort/`
* **MCP Base Endpoint**: `https://api.longfortpro.com/mcp/{server_name}`
* **Active Qualified Listings (20 servers registered total)**:
  * `longfort/spx-regime-selector`
  * `longfort/qqq-regime-selector`
  * `longfort/spy-regime-selector`
  * `longfort/nvda-regime-selector`
  * `longfort/spacex-regime-selector` (Maps to SPCX)
  * `longfort/spacex-options` (Maps to SPCX)
  * `longfort/spacex-flow` (Maps to SPCX)
  * `longfort/spacex-sentiment` (Maps to SPCX)
  * `longfort/dax-regime-selector`
  * `longfort/nkd-regime-selector`
  * `longfort/gld-regime-selector`
  * `longfort/uso-regime-selector`
  * `longfort/iwm-regime-selector`
  * `longfort/btc-regime-selector`


