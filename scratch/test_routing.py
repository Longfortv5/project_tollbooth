import httpx
import json

# Local Tollbooth gateway URL
BASE_URL = "http://127.0.0.1:8008"

def parse_mcp_response(resp) -> dict:
    content_type = resp.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        data_lines = [l[5:].strip() for l in resp.text.splitlines() if l.startswith("data:")]
        if not data_lines:
            return {}
        return json.loads(data_lines[-1])
    try:
        return resp.json()
    except Exception:
        return {"error": f"Failed to parse JSON: {resp.text}"}

def test_tools_list(server_name: str):
    url = f"{BASE_URL}/mcp/{server_name}"
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "params": {},
        "id": 1
    }
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    
    print(f"\n--- Testing tools/list for: {server_name} ({url}) ---")
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=5.0)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            result = parse_mcp_response(resp)
            tools = result.get("result", {}).get("tools", [])
            print(f"Number of tools returned: {len(tools)}")
            for t in tools:
                print(f"  - Tool: {t['name']}")
                print(f"    Description: {t['description']}")
                try:
                    enum_vals = t['inputSchema']['properties']['ticker'].get('enum')
                    print(f"    Ticker Enum restriction: {enum_vals}")
                except KeyError:
                    pass
        else:
            print(resp.text)
    except Exception as e:
        print(f"Error: {e}")

def test_tools_call(server_name: str, tool_name: str, query_ticker: str):
    url = f"{BASE_URL}/mcp/{server_name}"
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {
                "ticker": query_ticker
            }
        },
        "id": 2
    }
    
    print(f"\n--- Testing tools/call for: {server_name} -> {tool_name} with ticker: {query_ticker} ---")
    try:
        # We will use the admin bypass key to bypass the actual payment step, but still check if ticker restriction is enforced
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer tb_longfort_admin_bypass_key"
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=5.0)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            res_data = parse_mcp_response(resp)
            if "error" in res_data:
                print(f"Error: {res_data['error']}")
            else:
                result = res_data.get("result", {})
                content = result.get("content", [])
                if content:
                    print(f"Result Content: {content[0].get('text')}")
                else:
                    print(res_data)
        else:
            print(resp.text)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_tools_list("spx-regime-selector")
    test_tools_list("qqq-regime-selector")
    test_tools_list("spacex-options")
    
    # Test ticker call enforcement (should succeed since query ticker matches server ticker)
    test_tools_call("spx-regime-selector", "get_market_regime", "SPX")
    
    # Test ticker call enforcement (should fail since query ticker QQQ does not match SPX server ticker)
    test_tools_call("spx-regime-selector", "get_market_regime", "QQQ")
