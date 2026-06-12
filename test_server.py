import subprocess
import time
import httpx
import sys
import os
import json
import base64

def parse_response_json(res) -> dict:
    try:
        return res.json()
    except Exception:
        data_lines = [l[5:].strip() for l in res.text.splitlines() if l.startswith("data:")]
        if not data_lines:
            raise ValueError(f"No 'data:' prefix found in response lines: {res.text}")
        return json.loads(data_lines[-1])

def run_test():
    # Setup temporary mock SQLite database locally
    import sqlite3
    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_hud_state.db"))
    if os.path.exists(db_path):
        os.remove(db_path)
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE fa_snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, underlying TEXT NOT NULL, spot REAL, netgex REAL, net_dex REAL, net_vex REAL, net_chex REAL,
        gamma_flip REAL, call_wall REAL, put_wall REAL, max_pain REAL, regime TEXT, vix REAL, vix9d REAL, vrp REAL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, top_oi_changes_json TEXT
    );
    """)
    c.execute("""
    CREATE TABLE fa_zero_dte (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, as_of TEXT NOT NULL, underlying_price REAL,
        expiration TEXT, pin_magnet_strike REAL, pin_score INTEGER, pin_distance_pct REAL,
        pct_of_total_gex REAL, em_1sd_pct REAL, straddle_price REAL, net_gex REAL,
        call_wall REAL, put_wall REAL, ts_ingested TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    c.execute("""
    CREATE TABLE fa_volatility (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, as_of TEXT NOT NULL, underlying_price REAL,
        rv_20d REAL, rv_60d REAL, atm_iv REAL, vrp_20d REAL, skew_profiles_json TEXT,
        ts_ingested TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    top_oi_json = json.dumps([
        {"strike": 700.0, "net_gex": -1.027e9, "call_oi": 108108, "put_oi": 286899},
        {"strike": 705.0, "net_gex": -4.42355e8, "call_oi": 42211, "put_oi": 111965}
    ])
    c.execute("""
    INSERT INTO fa_snapshots (symbol, underlying, spot, netgex, net_dex, net_vex, net_chex, gamma_flip, call_wall, put_wall, max_pain, regime, vix, vix9d, vrp, top_oi_changes_json)
    VALUES ('QQQ', 'QQQ', 700.81, -5.52e9, 1.9771e10, -4.704e9, 5.121e6, 717.36, 720.0, 700.0, 691.0, 'bearish_trend', 25.0, 26.0, 6.59, ?)
    """, (top_oi_json,))
    c.execute("""
    INSERT INTO fa_snapshots (symbol, underlying, spot, netgex, net_dex, net_vex, net_chex, gamma_flip, call_wall, put_wall, max_pain, regime, vix, vix9d, vrp, top_oi_changes_json)
    VALUES ('SPY', 'SPY', 500.0, -1.0e9, 5e9, -1e9, 1e6, 505.0, 510.0, 495.0, 490.0, 'bullish_trend', 15.0, 14.0, 2.0, ?)
    """, (top_oi_json,))
    c.execute("""
    INSERT INTO fa_snapshots (symbol, underlying, spot, netgex, net_dex, net_vex, net_chex, gamma_flip, call_wall, put_wall, max_pain, regime, vix, vix9d, vrp, top_oi_changes_json)
    VALUES ('SPX', 'SPX', 5000.0, -1.0e10, 5e10, -1e10, 1e7, 5050.0, 5100.0, 4950.0, 4900.0, 'bullish_trend', 15.0, 14.0, 2.0, ?)
    """, (top_oi_json,))
    
    c.execute("""
    INSERT INTO fa_zero_dte (symbol, as_of, underlying_price, expiration, pin_magnet_strike, pin_score, pin_distance_pct, pct_of_total_gex)
    VALUES ('QQQ', '2026-06-09', 700.81, '2026-06-09', 700.0, 54, -0.12, 25.4)
    """)
    c.execute("""
    INSERT INTO fa_zero_dte (symbol, as_of, underlying_price, expiration, pin_magnet_strike, pin_score, pin_distance_pct, pct_of_total_gex)
    VALUES ('SPY', '2026-06-09', 500.0, '2026-06-09', 500.0, 54, -0.12, 25.4)
    """)
    c.execute("""
    INSERT INTO fa_zero_dte (symbol, as_of, underlying_price, expiration, pin_magnet_strike, pin_score, pin_distance_pct, pct_of_total_gex)
    VALUES ('SPX', '2026-06-09', 5000.0, '2026-06-09', 5000.0, 54, -0.12, 25.4)
    """)
    
    skew_json = json.dumps([
        {"expiry": "2026-06-09", "days_to_expiry": 0, "put_25d_iv": 46.87, "atm_iv": 30.73, "call_25d_iv": 38.4, "skew_25d": 8.47, "smile_ratio": 1.221}
    ])
    c.execute("""
    INSERT INTO fa_volatility (symbol, as_of, underlying_price, rv_20d, rv_60d, atm_iv, vrp_20d, skew_profiles_json)
    VALUES ('QQQ', '2026-06-09', 700.81, 24.14, 21.72, 30.73, 6.59, ?)
    """, (skew_json,))
    c.execute("""
    INSERT INTO fa_volatility (symbol, as_of, underlying_price, rv_20d, rv_60d, atm_iv, vrp_20d, skew_profiles_json)
    VALUES ('SPY', '2026-06-09', 500.0, 12.0, 11.0, 14.0, 2.0, ?)
    """, (skew_json,))
    c.execute("""
    INSERT INTO fa_volatility (symbol, as_of, underlying_price, rv_20d, rv_60d, atm_iv, vrp_20d, skew_profiles_json)
    VALUES ('SPX', '2026-06-09', 5000.0, 12.0, 11.0, 14.0, 2.0, ?)
    """, (skew_json,))
    
    conn.commit()
    conn.close()

    print("Starting Uvicorn server in background...")
    env = os.environ.copy()
    env["BASE_WALLET_ADDRESS"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
    env["X402_NETWORK"] = "eip155:84532"  # Sepolia
    env["MOCK_REDIS"] = "1"
    env["SQLITE_DB_PATH"] = db_path
    
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

        # Test 11: Dynamic Pricing Challenge Verification
        print("\nTest 11: Verifying dynamic pricing challenges for each tool...")
        pricing_test_cases = [
            ("get_market_regime", "100000", {"ticker": "NQ"}),
            ("get_0dte_verdict", "50000", {"ticker": "NQ"}),
            ("get_spx_gamma", "50000", {}),
            ("get_spy_gamma", "20000", {}),
            ("get_qqq_gex", "20000", {}),
        ]
        for tool_name, expected_price, args in pricing_test_cases:
            payload = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": args
                },
                "id": 100
            }
            res = client.post("http://127.0.0.1:8085/mcp/", json=payload)
            if res.status_code != 402:
                print(f"[-] Test 11 Failed for {tool_name}: expected 402, got {res.status_code}. Body: {res.text}")
                success = False
                continue
            header = res.headers.get("Payment-Required")
            if not header:
                print(f"[-] Test 11 Failed for {tool_name}: missing Payment-Required header")
                success = False
                continue
            try:
                decoded = base64.b64decode(header).decode("utf-8")
                data = json.loads(decoded)
                accepts = data.get("accepts")
                if isinstance(accepts, list):
                    option = accepts[0]
                else:
                    option = accepts
                amount = option.get("amount")
                if amount != expected_price:
                    print(f"[-] Test 11 Failed for {tool_name}: expected price '{expected_price}', got '{amount}'")
                    success = False
                else:
                    print(f"[+] Verified {tool_name} returns correct price: {amount}")
            except Exception as e:
                print(f"[-] Test 11 Failed for {tool_name} with exception: {e}")
                success = False

        # Test 12: Query /llms.txt (should return 200 OK and plaintext specs)
        print("\nTest 12: Querying /llms.txt...")
        res_llms = client.get("http://127.0.0.1:8085/llms.txt")
        print(f"Response status code: {res_llms.status_code}")
        if res_llms.status_code != 200:
            print(f"[-] Test 12 Failed: Expected 200 OK, got {res_llms.status_code}")
            success = False
        else:
            print(f"llms.txt Content Preview:\n{res_llms.text[:300]}...")
            if "Project Tollbooth" in res_llms.text and "USDC" in res_llms.text:
                print("[+] Test 12 Passed: /llms.txt returned correctly")
            else:
                print("[-] Test 12 Failed: /llms.txt does not contain expected spec content")
                success = False


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
                    res_json = parse_response_json(res_credit)
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
                    
                    # Assert zero_dte block is present and has qualitative fields
                    assert "zero_dte" in tool_data, "Missing zero_dte block"
                    assert tool_data["zero_dte"] is not None, "zero_dte block is None"
                    assert tool_data["zero_dte"].get("verdict") == "MODERATE_PIN_POTENTIAL", f"Unexpected zero_dte verdict: {tool_data['zero_dte'].get('verdict')}"
                    assert tool_data["zero_dte"].get("gex_share_pct") == 25.4, f"Unexpected zero_dte gex_share_pct: {tool_data['zero_dte'].get('gex_share_pct')}"
                    
                    # Assert no raw pricing/calibration numbers leak (spot is allowed as public)
                    for raw_key in ["call_wall", "put_wall", "gamma_flip", "netgex", "net_dex", "net_vex", "key_strikes", "levels", "exposures_usd", "volatility"]:
                        assert raw_key not in tool_data, f"Raw leak in top-level: {raw_key}"
                        assert raw_key not in tool_data.get("regime", {}), f"Raw leak in regime: {raw_key}"
                    
                    for raw_zdte_key in ["pin_magnet", "distance_to_magnet_pct", "pin_score", "expiration"]:
                        assert raw_zdte_key not in tool_data["zero_dte"], f"Raw leak in zero_dte block: {raw_zdte_key}"
                    
                    print("[+] Test 8 Passed: Call served on credits, verified sanitized qualitative payload compliance.")
                except Exception as parse_err:
                    print(f"[-] Test 8 Failed: Schema assertion error: {parse_err}. Body: {res_credit.text}")
                    success = False
            else:
                print(f"[-] Test 8 Failed: status={res_credit.status_code}, remaining header={remaining}")
                success = False

            # Test 9: tools/call with a funded credit key calling get_0dte_verdict
            print("\nTest 9: tools/call to get_0dte_verdict (funded credit key)...")
            rc.set(f"credits:{test_key}", 1)
            res_zdte = client.post(
                "http://127.0.0.1:8085/mcp/",
                json={"jsonrpc": "2.0", "method": "tools/call",
                      "params": {"name": "get_0dte_verdict", "arguments": {"ticker": "NQ"}}, "id": 10},
                headers={"Authorization": f"Bearer {test_key}"}
            )
            print(f"Response status code: {res_zdte.status_code}")
            if res_zdte.status_code == 200:
                try:
                    res_json = parse_response_json(res_zdte)
                    tool_result_text = res_json["result"]["content"][0]["text"]
                    tool_data = json.loads(tool_result_text)
                    
                    assert "zero_dte" in tool_data, "Missing zero_dte key in output"
                    assert tool_data["zero_dte"].get("verdict") == "MODERATE_PIN_POTENTIAL", f"Unexpected verdict: {tool_data}"
                    assert tool_data["zero_dte"].get("gex_share_pct") == 25.4, f"Unexpected gex_share_pct: {tool_data}"
                    print("[+] Test 9 Passed: verified get_0dte_verdict output structure.")
                except Exception as parse_err:
                    print(f"[-] Test 9 Failed: Schema assertion error: {parse_err}. Body: {res_zdte.text}")
                    success = False
            else:
                print(f"[-] Test 9 Failed: status={res_zdte.status_code}")
                success = False

            # Test 10: tools/call with a funded credit key calling get_qqq_gex
            print("\nTest 10: tools/call to get_qqq_gex (funded credit key)...")
            rc.set(f"credits:{test_key}", 1)
            res_qqq = client.post(
                "http://127.0.0.1:8085/mcp/",
                json={"jsonrpc": "2.0", "method": "tools/call",
                      "params": {"name": "get_qqq_gex", "arguments": {}}, "id": 11},
                headers={"Authorization": f"Bearer {test_key}"}
            )
            print(f"Response status code: {res_qqq.status_code}")
            if res_qqq.status_code == 200:
                try:
                    res_json = parse_response_json(res_qqq)
                    tool_result_text = res_json["result"]["content"][0]["text"]
                    tool_data = json.loads(tool_result_text)
                    
                    assert tool_data.get("ticker") == "QQQ", f"Unexpected resolved ticker: {tool_data}"
                    assert tool_data.get("verdict") == "BEARISH", f"Unexpected verdict: {tool_data}"
                    print("[+] Test 10 Passed: verified get_qqq_gex output.")
                except Exception as parse_err:
                    print(f"[-] Test 10 Failed: Schema assertion error: {parse_err}. Body: {res_qqq.text}")
                    success = False
            else:
                print(f"[-] Test 10 Failed: status={res_qqq.status_code}")
                success = False

            # Drain balance and confirm fall-through to 402
            rc.set(f"credits:{test_key}", 0)
            res_drained = client.post(
                "http://127.0.0.1:8085/mcp/",
                json={"jsonrpc": "2.0", "method": "tools/call",
                      "params": {"name": "get_market_regime", "arguments": {"ticker": "NQ"}}, "id": 12},
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
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except Exception:
                pass
        
    if success:
        print("\n[=== ALL TESTS PASSED ===]")
        sys.exit(0)
    else:
        print("\n[=== SOME TESTS FAILED ===]")
        sys.exit(1)

if __name__ == "__main__":
    run_test()
