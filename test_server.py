import subprocess
import time
import httpx
import sys
import os
import json
import base64

def run_test():
    print("Starting Uvicorn server in background...")
    env = os.environ.copy()
    env["BASE_WALLET_ADDRESS"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
    env["X402_NETWORK"] = "eip155:84532"  # Sepolia
    env["MOCK_REDIS"] = "1"
    
    proc = subprocess.Popen(
        [".venv/bin/uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8085"],
        env=env
    )
    
    # Wait for server to start
    time.sleep(3.0)
    
    client = httpx.Client(headers={"Accept": "application/json, text/event-stream"}, follow_redirects=True)
    success = True
    
    try:
        # Test 1: Query tools/list (should be free -> 200 OK)
        print("\nTest 1: Querying tools/list (Free route)...")
        payload_list = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": 1
        }
        with client.stream("POST", "http://127.0.0.1:8085/mcp/", json=payload_list) as r:
            print(f"Response status code: {r.status_code}")
            if r.status_code != 200:
                print("[-] Test 1 Failed: tools/list returned non-200 code.")
                success = False
            else:
                print("[+] Test 1 Passed: Received 200 OK for free route")
                # Read the first event from the stream
                for line in r.iter_lines():
                    print(f"[Stream Output] {line}")
                    if line.strip():
                        break
            
        # Test 2: Query tools/call for get_market_regime (should require payment -> 402)
        print("\nTest 2: Querying get_market_regime tool (Gated route)...")
        payload_call = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_market_regime",
                "arguments": {"ticker": "NQ"}
            },
            "id": 2
        }
        res_call = client.post("http://127.0.0.1:8085/mcp/", json=payload_call)
        print(f"Response status code: {res_call.status_code}")
        
        if res_call.status_code != 402:
            print(f"[-] Test 2 Failed: Expected 402 Payment Required, got {res_call.status_code}. Body: {res_call.text}")
            success = False
        else:
            print("[+] Test 2 Passed: Received 402 Payment Required")
            
        # Verify challenge headers
        req_header = res_call.headers.get("Payment-Required")
        if not req_header:
            print("[-] Test 2 Failed: Missing 'Payment-Required' header in 402 response")
            success = False
        else:
            try:
                decoded = base64.b64decode(req_header).decode("utf-8")
                print(f"Decoded 'Payment-Required' header payload:\n  {decoded}")
                req_data = json.loads(decoded)
                if "accepts" in req_data:
                    print("[+] Verified 'Payment-Required' challenge schema compliance.")
            except Exception as e:
                print(f"[-] Test 2 Failed: Failed to decode or parse Payment-Required header: {e}")
                success = False

        # Test 3: Query prompts/list (should be free whitelist -> 200 OK)
        print("\nTest 3: Querying prompts/list (Free route whitelist)...")
        payload_prompts = {
            "jsonrpc": "2.0",
            "method": "prompts/list",
            "params": {},
            "id": 3
        }
        res_prompts = client.post("http://127.0.0.1:8085/mcp/", json=payload_prompts)
        print(f"Response status code: {res_prompts.status_code}")
        if res_prompts.status_code != 200:
            print("[-] Test 3 Failed: prompts/list returned non-200 code.")
            success = False
        else:
            print("[+] Test 3 Passed: Received 200 OK for prompts whitelist")

        # Test 4: Query get_market_regime for a missing ticker (Pre-check free error -> 200 OK with error body)
        print("\nTest 4: Querying get_market_regime for missing ticker (Free error check)...")
        payload_missing = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_market_regime",
                "arguments": {"ticker": "XYZ"}
            },
            "id": 4
        }
        res_missing = client.post("http://127.0.0.1:8085/mcp/", json=payload_missing)
        print(f"Response status code: {res_missing.status_code}")
        if res_missing.status_code != 200:
            print(f"[-] Test 4 Failed: Expected 200 OK, got {res_missing.status_code}")
            success = False
        else:
            print(f"Body: {res_missing.text}")
            if "error" in res_missing.text:
                print("[+] Test 4 Passed: Received JSON-RPC error response free of charge")
            else:
                print("[-] Test 4 Failed: Response body does not contain expected error structure")
                success = False

        # Test 5: Query batch request with multiple paid calls (Reject batches -> 200 OK with batch error)
        print("\nTest 5: Querying batch with multiple paid calls (Reject batch check)...")
        payload_batch = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "get_market_regime",
                    "arguments": {"ticker": "NQ"}
                },
                "id": 5
            },
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "get_market_regime",
                    "arguments": {"ticker": "ES"}
                },
                "id": 6
            }
        ]
        res_batch = client.post("http://127.0.0.1:8085/mcp/", json=payload_batch)
        print(f"Response status code: {res_batch.status_code}")
        if res_batch.status_code != 200:
            print(f"[-] Test 5 Failed: Expected 200 OK, got {res_batch.status_code}")
            success = False
        else:
            print(f"Body: {res_batch.text}")
            if "Batch requests containing multiple paid calls are not supported" in res_batch.text:
                print("[+] Test 5 Passed: Multi-paid batch rejected cleanly free of charge")
            else:
                print("[-] Test 5 Failed: Response does not contain expected batch rejection error")
                success = False

        # Test 6: Purchase credits without payment (should require payment -> 402)
        print("\nTest 6: POST /credits/purchase without payment (Gated purchase)...")
        res_buy = client.post("http://127.0.0.1:8085/credits/purchase")
        print(f"Response status code: {res_buy.status_code}")
        if res_buy.status_code != 402:
            print(f"[-] Test 6 Failed: Expected 402, got {res_buy.status_code}. Body: {res_buy.text}")
            success = False
        else:
            buy_header = res_buy.headers.get("Payment-Required")
            if buy_header and b'"150000000"' in base64.b64decode(buy_header):
                print("[+] Test 6 Passed: Purchase gated at credit-pack price (150 USDC atomic)")
            elif buy_header:
                print("[+] Test 6 Passed: Received 402 with Payment-Required header")
            else:
                print("[-] Test 6 Failed: Missing Payment-Required header")
                success = False

        # Test 7: tools/call with an unknown (but well-formed) API key -> falls through to 402
        print("\nTest 7: tools/call with unknown bearer key (should still be 402)...")
        res_badkey = client.post(
            "http://127.0.0.1:8085/mcp/",
            json={"jsonrpc": "2.0", "method": "tools/call",
                  "params": {"name": "get_market_regime", "arguments": {"ticker": "NQ"}}, "id": 7},
            headers={"Authorization": "Bearer tb_unknownkey_0123456789abcdef"}
        )
        print(f"Response status code: {res_badkey.status_code}")
        if res_badkey.status_code != 402:
            print(f"[-] Test 7 Failed: Expected 402, got {res_badkey.status_code}")
            success = False
        else:
            print("[+] Test 7 Passed: Unknown key falls through to x402 challenge")

        # Test 8: tools/call with a funded credit key (requires local Redis; skipped otherwise)
        print("\nTest 8: tools/call with funded credit key (credit decrement)...")
        try:
            import redis as redis_sync
            rc = redis_sync.from_url("redis://localhost:6379/0")
            rc.ping()
            test_key = "tb_testcreditkey_0123456789abcdef"
            rc.set(f"credits:{test_key}", 2)

            res_credit = client.post(
                "http://127.0.0.1:8085/mcp/",
                json={"jsonrpc": "2.0", "method": "tools/call",
                      "params": {"name": "get_market_regime", "arguments": {"ticker": "NQ"}}, "id": 8},
                headers={"Authorization": f"Bearer {test_key}"}
            )
            print(f"Response status code: {res_credit.status_code}")
            remaining = res_credit.headers.get("x-tollbooth-credits-remaining")
            if res_credit.status_code == 200 and remaining == "1":
                try:
                    # Response may be plain JSON or SSE-framed ("data: {...}")
                    try:
                        res_json = res_credit.json()
                    except Exception:
                        data_lines = [l[5:].strip() for l in res_credit.text.splitlines()
                                      if l.startswith("data:")]
                        res_json = json.loads(data_lines[-1])
                    tool_result_text = res_json["result"]["content"][0]["text"]
                    tool_data = json.loads(tool_result_text)

                    # Assert canonical schema fields are present and correct
                    assert tool_data.get("schema_version") == "1.0", f"Unexpected schema_version: {tool_data.get('schema_version')}"
                    assert tool_data.get("ticker") == "QQQ", f"Unexpected resolved ticker: {tool_data.get('ticker')}"
                    assert tool_data.get("requested_as") == "NQ", f"Unexpected requested_as: {tool_data.get('requested_as')}"
                    assert tool_data.get("spot") is not None, "Missing spot price"
                    assert tool_data.get("verdict") == "BEARISH", f"Unexpected verdict: {tool_data.get('verdict')}"
                    assert tool_data.get("walls_status") is not None, "Missing walls_status"
                    assert tool_data.get("reason_brief") is not None, "Missing reason_brief"
                    assert tool_data.get("regime", {}).get("gamma") == "negative", f"Unexpected gamma: {tool_data.get('regime')}"
                    
                    # Assert no raw pricing/calibration numbers leak (spot is allowed as public)
                    for raw_key in ["call_wall", "put_wall", "gamma_flip", "netgex", "net_dex", "net_vex", "key_strikes", "levels", "exposures_usd", "volatility"]:
                        assert raw_key not in tool_data, f"Raw leak in top-level: {raw_key}"
                        assert raw_key not in tool_data.get("regime", {}), f"Raw leak in regime: {raw_key}"
                    
                    print("[+] Test 8 Passed: Call served on credits, verified sanitized qualitative payload compliance.")
                except Exception as parse_err:
                    print(f"[-] Test 8 Failed: Schema assertion error: {parse_err}. Body: {res_credit.text}")
                    success = False
            else:
                print(f"[-] Test 8 Failed: status={res_credit.status_code}, remaining header={remaining}")
                success = False

            # Drain balance and confirm fall-through to 402
            rc.set(f"credits:{test_key}", 0)
            res_drained = client.post(
                "http://127.0.0.1:8085/mcp/",
                json={"jsonrpc": "2.0", "method": "tools/call",
                      "params": {"name": "get_market_regime", "arguments": {"ticker": "NQ"}}, "id": 9},
                headers={"Authorization": f"Bearer {test_key}"}
            )
            if res_drained.status_code == 402:
                print("[+] Test 8b Passed: Drained key falls through to 402")
            else:
                print(f"[-] Test 8b Failed: Expected 402 after drain, got {res_drained.status_code}")
                success = False
            rc.delete(f"credits:{test_key}")
        except Exception as redis_err:
            print(f"[!] Test 8 Skipped: local Redis not reachable ({redis_err})")

    except Exception as e:
        print(f"[-] Exception during testing: {e}")
        success = False
    finally:
        print("\nStopping Uvicorn server...")
        proc.terminate()
        proc.wait()
        
    if success:
        print("\n[=== ALL TESTS PASSED ===]")
        sys.exit(0)
    else:
        print("\n[=== SOME TESTS FAILED ===]")
        sys.exit(1)

if __name__ == "__main__":
    run_test()
