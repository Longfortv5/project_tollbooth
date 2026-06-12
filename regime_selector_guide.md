# Longfort Regime Gate Integration Guide

Welcome to the **Longfort Regime Gate** documentation. This service is designed to serve as an institutional risk-management gateway and execution filter for autonomous trading agents.

---

## 1. Service Overview & Specifications
* **Name**: `yu_global_macro_supervisor` (Longfort Regime Gate)
* **Description**: Provides real-time institutional market microstructure analysis, Gamma Exposure (GEX) tilts, Order Flow Imbalance (OFI), and consolidated market regime states (volatility expansion, range consolidation) for global equities, indices (NQ, ES, DAX, NIY), and commodities. Designed for algorithmic execution gates and automated risk management guardrails. Cross-verified via dual-model (Qwen/Gemini) consensus.
* **Pricing**: **0.019 USDC** per tool execution call.
* **Payment Protocol**: `x402` (Base chain Sepolia/Mainnet).

---

## 2. Exposed MCP Tool
External agents query market microstructure using a single, unified tool:

### `get_market_regime`
* **Purpose**: Retrieves options walls boundaries, volatility state, and GEX consensus.
* **Parameters**:
  - `ticker` (string, required): Ticker symbol. Supports index and ETF symbols, as well as colloquial futures aliases:
    - **Nasdaq**: `QQQ` (alias `NQ`, `NDX`)
    - **S&P 500**: `SPY` (alias `ES`, `SPX`)
    - **Russell 2000**: `IWM` (alias `RTY`)
    - **Gold**: `GLD` (alias `GOLD`, `GC`)
    - **WTI Crude**: `USO` (alias `WTI`, `CL`, `OIL`)
* **Output**: Qualitative consensus verdict (`BULLISH`, `BEARISH`, `CHOP`, `AVOID`), spot price, and qualitative option boundaries description (`walls_status`).

---

## 3. The x402 Gating and Billing Lifecycle
Access to the tool is monetized using the `x402` protocol. The lifecycle of a pay-per-call query is as follows:

1. **Anonymous Call**: The agent sends a JSON-RPC request to `/mcp/`.
2. **Challenge (402)**: The server returns `402 Payment Required` with a base64-encoded `Payment-Required` header details specifying:
   - Asset Contract (Base USDC: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
   - Network (Base Mainnet: `eip155:8453`, Sepolia: `eip155:84532`)
   - Price: `19000` (atomic units of USDC, equivalent to 0.019 USDC)
   - Destination Wallet (`BASE_WALLET_ADDRESS`)
3. **Signature Response**: The agent signs a standard CAIP-compliant payment draft using an EIP-3009 transfer authorization and retries the call including the `Payment-Signature` header.
4. **Settlement & Output**: The server verifies the signature, triggers settlement via the facilitator, and returns the qualitative payload.

---

## 4. Integration Snippet (Python)

Agents can query the gate programmatically using the following snippet:

```python
import urllib.request
import json
import base64

REGIME_GATE_URL = "http://100.116.134.114:8008/mcp"

def call_market_regime(ticker="NQ") -> dict:
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "get_market_regime",
            "arguments": {"ticker": ticker}
        },
        "id": 1
    }
    
    req = urllib.request.Request(
        REGIME_GATE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=5.0) as r:
            res = json.loads(r.read().decode())
            content_list = res.get("result", {}).get("content", [])
            if content_list and content_list[0].get("type") == "text":
                return json.loads(content_list[0]["text"])
            return res
    except urllib.error.HTTPError as e:
        if e.code == 402:
            challenge = e.headers.get("Payment-Required")
            # Challenge payload is decoded and signed by agent's private key before retrying
            return {"error": "Payment Required (402)", "challenge": challenge}
        return {"error": f"HTTP Error {e.code}", "detail": e.read().decode()}
```
