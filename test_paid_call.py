"""
Project Tollbooth - Buyer-side end-to-end paid call test.

Exercises the FULL x402 loop against a running Tollbooth server:
  1. Bare request           -> expect 402 + Payment-Required challenge
  2. Challenge inspection   -> decode, verify network/price, enforce safety cap
  3. Auto-paying request    -> x402 client signs EIP-3009, retries
  4. Settlement receipt     -> decode PAYMENT-RESPONSE header (tx hash)
  5. Payload validation     -> parse tool result, check schema v1.0

This is the test that proves Tollbooth actually collects money. Run it on
Sepolia BEFORE any mainnet work.

Setup (Sepolia):
  1. Generate a throwaway buyer wallet:
       python -c "from eth_account import Account; a=Account.create(); print(a.address, a.key.hex())"
  2. Fund it with Base Sepolia USDC (Circle faucet) + a little Base Sepolia ETH.
  3. export BUYER_PRIVATE_KEY=0x...
  4. Start the server (X402_NETWORK=eip155:84532) and populate Redis.

Usage:
  .venv/bin/python test_paid_call.py                          # sepolia, tool call
  .venv/bin/python test_paid_call.py --ticker SPX
  .venv/bin/python test_paid_call.py --buy-credits            # purchase a pack
  .venv/bin/python test_paid_call.py --network mainnet --url https://api.longfort.com/mcp/ --yes

NEVER pass the private key as an argument; only via BUYER_PRIVATE_KEY.
"""

import argparse
import asyncio
import base64
import json
import os
import sys

NETWORKS = {
    "sepolia": "eip155:84532",
    "mainnet": "eip155:8453",
}
USDC_DECIMALS = 6


def fail(msg: str):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def decode_b64_json(value: str):
    return json.loads(base64.b64decode(value).decode("utf-8"))


def mcp_body(ticker: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "get_market_regime", "arguments": {"ticker": ticker}},
        "id": 1,
    }


def parse_mcp_response(resp_text: str, content_type: str):
    """Handle both plain JSON and SSE-framed MCP responses."""
    if "text/event-stream" in content_type:
        data_lines = [l[5:].strip() for l in resp_text.splitlines() if l.startswith("data:")]
        if not data_lines:
            return None
        return json.loads(data_lines[-1])
    return json.loads(resp_text)


async def run(args):
    import httpx
    from eth_account import Account
    from x402 import x402Client
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.http.clients import x402HttpxClient

    expected_network = NETWORKS[args.network]
    key = os.getenv("BUYER_PRIVATE_KEY")
    if not key:
        fail("BUYER_PRIVATE_KEY environment variable not set.")
    account = Account.from_key(key)
    print(f"[*] Buyer wallet:     {account.address}")
    print(f"[*] Target:           {args.url}")
    print(f"[*] Expected network: {expected_network} ({args.network})")

    if args.buy_credits:
        target_url = args.url.rstrip("/").rsplit("/mcp", 1)[0] + "/credits/purchase"
        request_kwargs = {}
        max_usd = args.max_usd if args.max_usd is not None else 200.0
        print(f"[*] Mode:             credit pack purchase -> {target_url}")
    else:
        target_url = args.url
        request_kwargs = {
            "json": mcp_body(args.ticker),
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        }
        max_usd = args.max_usd if args.max_usd is not None else 0.05
        print(f"[*] Mode:             tool call (ticker={args.ticker}), price cap ${max_usd}")

    # ------------------------------------------------------------------
    # Step 1: bare request -> expect 402 challenge
    # ------------------------------------------------------------------
    print("\n=== Step 1: bare request (expect 402) ===")
    async with httpx.AsyncClient(timeout=30) as bare:
        r = await bare.post(target_url, **request_kwargs)
    print(f"Status: {r.status_code}")
    if r.status_code != 402:
        fail(f"Expected 402, got {r.status_code}. Body: {r.text[:400]}")

    challenge_header = r.headers.get("Payment-Required") or r.headers.get("PAYMENT-REQUIRED")
    if not challenge_header:
        fail("402 received but no Payment-Required header (v2). Check server SDK version.")
    challenge = decode_b64_json(challenge_header)
    accepts = challenge.get("accepts") or []
    if not accepts:
        fail(f"Challenge has no 'accepts' options: {challenge}")
    option = accepts[0]
    print(f"Challenge: scheme={option.get('scheme')} network={option.get('network')} "
          f"amount={option.get('amount')} payTo={option.get('payTo')}")

    # ------------------------------------------------------------------
    # Step 2: safety checks before signing anything
    # ------------------------------------------------------------------
    print("\n=== Step 2: safety checks ===")
    if option.get("network") != expected_network:
        fail(f"Network mismatch! Challenge wants {option.get('network')}, "
             f"you expected {expected_network}. Aborting before any signature.")
    amount_usd = int(option.get("amount", "0")) / 10 ** USDC_DECIMALS
    print(f"Price: {amount_usd:.6f} USDC")
    if amount_usd > max_usd:
        fail(f"Price {amount_usd} exceeds safety cap {max_usd}. Use --max-usd to override.")
    if args.network == "mainnet" and not args.yes:
        answer = input(f"About to spend {amount_usd} REAL USDC on Base mainnet. Type 'pay' to proceed: ")
        if answer.strip().lower() != "pay":
            fail("Aborted by user.")
    print("[OK] Network and price verified.")

    # ------------------------------------------------------------------
    # Step 3: auto-paying request via x402 client
    # ------------------------------------------------------------------
    print("\n=== Step 3: paid request (sign -> retry -> verify -> settle) ===")
    x402_client = x402Client()
    register_exact_evm_client(x402_client, EthAccountSigner(account))

    async with x402HttpxClient(x402_client, timeout=60) as paying:
        r2 = await paying.post(target_url, **request_kwargs)
        body_text = r2.text
    print(f"Status: {r2.status_code}")
    if r2.status_code != 200:
        fail(f"Paid request did not return 200. Body: {body_text[:400]}")

    # ------------------------------------------------------------------
    # Step 4: settlement receipt
    # ------------------------------------------------------------------
    print("\n=== Step 4: settlement receipt ===")
    receipt_header = r2.headers.get("Payment-Response") or r2.headers.get("PAYMENT-RESPONSE")
    if receipt_header:
        try:
            receipt = decode_b64_json(receipt_header)
            print(json.dumps(receipt, indent=2)[:800])
            tx = receipt.get("transaction") or receipt.get("txHash")
            if tx:
                scan = "sepolia.basescan.org" if args.network == "sepolia" else "basescan.org"
                print(f"[OK] Settled on-chain: https://{scan}/tx/{tx}")
        except Exception as e:
            print(f"[WARN] Could not decode Payment-Response header: {e}")
    else:
        print("[WARN] No Payment-Response header — verify settlement on BaseScan manually "
              f"(recipient {option.get('payTo')}).")

    # ------------------------------------------------------------------
    # Step 5: payload validation
    # ------------------------------------------------------------------
    print("\n=== Step 5: payload validation ===")
    if args.buy_credits:
        data = json.loads(body_text)
        if not str(data.get("api_key", "")).startswith("tb_"):
            fail(f"Purchase response missing api_key: {body_text[:300]}")
        print(f"[OK] Credit pack purchased: {data.get('credits')} calls. "
              f"api_key={data['api_key'][:14]}... (full key NOT printed; re-run with care)")
    else:
        rpc = parse_mcp_response(body_text, r2.headers.get("content-type", ""))
        if rpc is None:
            fail(f"Could not parse MCP response: {body_text[:300]}")
        if "error" in rpc:
            fail(f"JSON-RPC error after payment (!): {rpc['error']}")
        tool_text = rpc["result"]["content"][0]["text"]
        payload = json.loads(tool_text)
        if payload.get("error"):
            fail(f"PAID for an error payload — pre-check should have caught this: {payload['error']}")
        if payload.get("schema_version") != "1.0":
            fail(f"Unexpected schema_version: {payload.get('schema_version')}")
        print(f"[OK] Received schema v1.0 payload: ticker={payload.get('ticker')} "
              f"spot={payload.get('spot')} gamma={payload.get('regime', {}).get('gamma')} "
              f"complete={payload.get('data_quality', {}).get('complete')}")

    print("\n[=== PAID CALL E2E PASSED ===]")
    print("Money flow verified: 402 -> EIP-3009 signature -> facilitator verify -> "
          "settle -> payload delivered.")


def main():
    p = argparse.ArgumentParser(description="Tollbooth buyer-side paid-call E2E test")
    p.add_argument("--network", choices=["sepolia", "mainnet"], default="sepolia")
    p.add_argument("--url", default="http://127.0.0.1:8000/mcp/",
                   help="MCP endpoint URL (default local dev server)")
    p.add_argument("--ticker", default="NQ")
    p.add_argument("--buy-credits", action="store_true",
                   help="Purchase a credit pack instead of a tool call")
    p.add_argument("--max-usd", type=float, default=None,
                   help="Safety cap in USD (default 0.05 tool / 200 credits)")
    p.add_argument("--yes", action="store_true",
                   help="Skip the mainnet confirmation prompt")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
