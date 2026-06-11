# Project Tollbooth: Paywalled MCP Server

Project Tollbooth is a Python-based FastAPI application acting as a Model Context Protocol (MCP) server over **Streamable HTTP**. It exposes pre-computed market regime data from a local Redis cache and monetizes access with **two billing paths**:

1. **Pay-per-call** via the official `x402` payment protocol (USDC on Base) — zero-friction for anonymous agents.
2. **Prepaid credit packs** via API keys — fast repeat access for high-frequency bots (no facilitator round-trip per call).

---

## Core Features

1. **Granular Payment Gating (fail-closed)**: Discovery methods (`initialize`, `ping`, `tools/list`, `resources/list`, `resources/templates/list`, `prompts/list`, `notifications/*`) are free and public. Everything else on `POST /mcp/` is billed. Unparseable bodies and unknown methods are gated, not bypassed.
2. **Official x402 V2 Integration**: The async `x402` SDK (`PaymentMiddlewareASGI`) handles the full challenge / EIP-3009 verification / settlement flow via a facilitator.
3. **Prepaid Credit Ledger**: Atomic Redis-backed (Lua `GET`+`DECRBY`) per-call decrement. Responses include an `x-tollbooth-credits-remaining` header. Invalid keys or drained balances fall through to the x402 402 challenge.
4. **Free Error Pre-check**: Calls for missing or stale regime data are answered free of charge — buyers never pay for "no data".
5. **Batch Protection**: Batches containing more than one billable call are rejected (prevents batch underpricing on per-call payment).
6. **Fail-safe Configuration**: Fails hard at startup on missing/invalid `BASE_WALLET_ADDRESS`, and on Base Mainnet without an explicit facilitator URL.

---

## Environment Configuration

| Variable | Description | Default |
| :--- | :--- | :--- |
| `BASE_WALLET_ADDRESS` | **Required**. Recipient address for USDC payments (validated format). | *None (fails if unset)* |
| `X402_NETWORK` | CAIP-2 chain id: `eip155:8453` (Mainnet) or `eip155:84532` (Sepolia). | `eip155:84532` |
| `X402_FACILITATOR_URL` | Facilitator endpoint. **Required on Mainnet** (testnet defaults to `https://x402.org/facilitator`). | *Derived (testnet only)* |
| `REDIS_URL` | Redis connection string. | `redis://localhost:6379/0` |
| `MAX_REGIME_AGE_SECONDS` | Staleness cutoff for cached regime data. | `900` |
| `TOOL_PRICE_ATOMIC` | Per-call price in USDC atomic units (6 decimals). | `19000` (0.019 USDC) |
| `CREDIT_PACK_CALLS` | Calls included in one prepaid pack. | `10000` |
| `CREDIT_PACK_PRICE_ATOMIC` | Pack price in USDC atomic units. | `150000000` (150 USDC) |
| `MOCK_REDIS` | Serve mocked regime data (testing only). | unset |

---

## Installation & Setup

```bash
uv venv
uv pip install -r requirements.txt

export BASE_WALLET_ADDRESS="0xYourWalletAddress"
export X402_NETWORK="eip155:84532"   # Base Sepolia for testing

# Populate the cache with schema v1.0 format (resolved canonical symbol, e.g. QQQ for NQ)
redis-cli set current_regime:QQQ '{"schema_version":"1.0","ticker":"QQQ","requested_as":"NQ","timestamp":'"$(date +%s)"',"source":"flashalpha","spot":700.81,"regime":{"gamma":"negative","volatility":"expansion","tilt":"GEX negative","consensus":"bearish_trend","gamma_flip":717.36,"distance_to_flip_pct":-2.31,"zero_dte_gex_share_pct":25.4},"exposures_usd":{"gex":-5.52e9,"dex":1.9771e10,"vex":-4.704e9,"charm":5.121e6},"volatility":{"atm_iv":30.73,"hv20":24.14,"hv60":21.72,"vrp":6.59,"put_iv_25d":46.87,"call_iv_25d":38.4,"skew_25d":8.47,"smile_ratio":1.221},"levels":{"call_wall":720.0,"put_wall":700.0,"gamma_flip_eod":717.13,"max_pain":691.0},"key_strikes":[{"strike":700.0,"net_gex_usd":-1.027e9,"call_oi":108108,"put_oi":286899}]}'

.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

---

## Billing Flows

### A. Pay-per-call (x402)

An agent without credentials calls the tool and receives a `402` with a base64 `Payment-Required` challenge header (scheme `exact`, network CAIP-2 id, USDC asset contract, atomic amount, `payTo`). It retries with a signed `Payment-Signature` header; the SDK verifies and settles via the facilitator.

```bash
curl -i -X POST http://127.0.0.1:8000/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_market_regime","arguments":{"ticker":"NQ"}},"id":2}'
# -> HTTP/1.1 402 Payment Required + Payment-Required header
```

### B. Prepaid credits (API key)

Buy a pack (this endpoint is itself x402-gated at the pack price):

```bash
curl -i -X POST http://127.0.0.1:8000/credits/purchase
# -> 402 challenge at pack price; pay via x402 client to receive:
# {"api_key": "tb_...", "credits": 10000, ...}
```

Then call the tool with the key — no payment round-trip, balance returned per response:

```bash
curl -i -X POST http://127.0.0.1:8000/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer tb_yourkey..." \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_market_regime","arguments":{"ticker":"NQ"}},"id":2}'
# -> 200 OK + x-tollbooth-credits-remaining: 9999
```

Check balance (free):

```bash
curl -s http://127.0.0.1:8000/credits/balance -H "Authorization: Bearer tb_yourkey..."
```

> [!NOTE]
> Credit purchase settlement occurs after the handler returns. If a facilitator settle fails post-verification, a key may exist without final settlement — monitor settle failures before scaling.

### Free discovery (no payment, no key)

```bash
curl -i -X POST http://127.0.0.1:8000/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}'
```

---

## Testing

```bash
.venv/bin/python test_server.py
```

Covers: free discovery routes, 402 challenge schema, free-error pre-check, multi-paid batch rejection, gated credit purchase, unknown-key fall-through, and (when local Redis is running) funded-key decrement + drained-key fall-through.

**Not yet covered**: a real end-to-end *paid* call (signed `Payment-Signature` → verify → settle) — run one on Base Sepolia with a funded key before going to Mainnet.

---

## Regulatory and Licensing Compliance

> [!WARNING]
> If your market regime cache contains pre-computed data derived from institutional feeds, inspect your data licensing agreement before reselling or exposing it through a public paywall. Many institutional data providers prohibit or heavily restrict commercial redistribution of derived metrics.
