---
name: "longfort-market-regime-tollbooth"
description: "Real-time options telemetry, GEX aggregates, and qualitative 0DTE pinning verdicts for QQQ, SPY, and SPX."
version: "1.0.0"
homepage: "https://longfort.com/tollbooth"
mcp_endpoint: "https://api.longfortpro.com/mcp"
capabilities:
  - "options-gex"
  - "0dte-pin-risk"
  - "market-regime-classification"
  - "dealer-hedging-walls"
x402:
  pay_to: "0x6F275aB348EF19456C3f53bfb2A3122CaCaa1A7d"
  network: "eip155:845"
  asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
  pricing:
    get_market_regime: "19000"
    get_0dte_verdict: "19000"
    get_spx_gamma: "19000"
    get_spy_gamma: "19000"
    get_qqq_gex: "19000"
---

# Longfort Market Regime Tollbooth

This skill exposes real-time quantitative options microstructure metrics, dealer hedging risk aggregates (GEX, DEX, VEX, Charm), option boundary walls, and specialized 0DTE pinning risk evaluation.

## Exposed Tools

### 1. `get_market_regime`
Returns the qualitative market regime evaluation, reason brief, and options walls status.
*   **Parameters:**
    *   `ticker` (string, required): Ticker symbol to query (QQQ, SPY, SPX, IWM; aliases NQ, ES, RTY resolve automatically).
*   **Response Schema (Gated/Sanitized):**
    ```json
    {
      "schema_version": "1.0",
      "ticker": "QQQ",
      "requested_as": "NQ",
      "timestamp": 1781287522,
      "source": "Longfort",
      "spot": 700.81,
      "regime": {
        "gamma": "negative"
      },
      "verdict": "BEARISH",
      "walls_status": "Price is trading in negative gamma territory, bounded by gamma flip resistance above and put wall support below.",
      "reason_brief": "Market microstructure indicates a stable state with a negative tilt, dominated by negative gamma hedging constraints.",
      "zero_dte": {
        "verdict": "MODERATE_PIN_POTENTIAL",
        "gex_share_pct": 25.4
      }
    }
    ```

### 2. `get_0dte_verdict`
Returns only the 0DTE (Zero Days to Expiration) option pinning verdict and GEX share percentage. Optimal for index day-trading.
*   **Parameters:**
    *   `ticker` (string, required): Ticker symbol to query.
*   **Response Schema:**
    ```json
    {
      "zero_dte": {
        "verdict": "STRONG_PIN_POTENTIAL",
        "gex_share_pct": 36.2
      }
    }
    ```

### 3. `get_spx_gamma`
Pre-configured tool returning the SPX (S&P 500 Index) options gamma profile and volatility state. Takes no arguments.

### 4. `get_spy_gamma`
Pre-configured tool returning the SPY (S&P 500 ETF) options gamma profile. Takes no arguments.

### 5. `get_qqq_gex`
Pre-configured tool returning the QQQ (Nasdaq 100 ETF) options gamma profile. Takes no arguments.

---

## The x402 Micropayment Lifecycle

Autonomous agents calling this API must process the following HTTP-native challenge cycle:

1.  **Request**: Agent sends an MCP JSON-RPC tool call payload to `https://api.longfortpro.com/mcp`.
2.  **Challenge**: If unsigned, the Tollbooth server returns `HTTP 402 Payment Required` with the `Payment-Required` header containing details:
    *   Receiver Address: `0x6F275aB348EF19456C3f53bfb2A3122CaCaa1A7d` (Base L2)
    *   Asset Contract: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (USDC)
    *   Price: `19000` (atomic units = $0.019 USDC)
3.  **Settlement**: Agent wallet executes transfer on Base L2 and receives transaction hash `0x...`.
4.  **Submission**: Agent resends request attaching the payment proof header:
    ```http
    Authorization: x402-exact tx_0x...
    ```

---

## Automated Client Examples

### Python Integration (Prepaid Credit Balance Check)
For high-frequency calls, prepaid credit packs are recommended to avoid paying gas on every individual call.

```python
import httpx
import json

API_KEY = "your_prepaid_api_key_here"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def check_balance():
    resp = httpx.get("https://api.longfortpro.com/credits/balance", headers=HEADERS)
    print("Credits remaining:", resp.json().get("credits_remaining"))

def query_regime(ticker: str):
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "get_0dte_verdict",
            "arguments": {"ticker": ticker}
        },
        "id": 1
    }
    resp = httpx.post("https://api.longfortpro.com/mcp", json=payload, headers=HEADERS)
    print("Result:", resp.json())

if __name__ == "__main__":
    check_balance()
    query_regime("SPX")
```
