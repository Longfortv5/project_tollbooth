import asyncio
import os
import sys

# Set mock env variables
os.environ["BASE_WALLET_ADDRESS"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
os.environ["MOCK_REDIS"] = "1"

# Add project directory to path
sys.path.append(os.path.abspath("/Users/huseyinsanli/.gemini/antigravity/scratch/project_tollbooth"))

from main import get_market_regime, bypass_gating_var

async def run_checks():
    print("=== Direct Tollbooth Verification ===")
    
    # 1. Test Admin (Qwen) Request mode
    print("\n[Admin Mode Test] Querying 'NQ' with bypass_gating_var = True:")
    bypass_gating_var.set(True)
    res_admin = await get_market_regime("NQ")
    print(f"Ticker: {res_admin.get('ticker')}")
    print(f"Requested As: {res_admin.get('requested_as')}")
    print(f"Spot: {res_admin.get('spot')}")
    print(f"Is Sanitized: {'verdict' in res_admin and 'levels' not in res_admin}")
    print(f"Full Keys: {list(res_admin.keys())}")
    
    # 2. Test External Agent Request mode
    print("\n[External Mode Test] Querying 'NQ' with bypass_gating_var = False:")
    bypass_gating_var.set(False)
    res_external = await get_market_regime("NQ")
    print(f"Ticker: {res_external.get('ticker')}")
    print(f"Requested As: {res_external.get('requested_as')}")
    print(f"Spot: {res_external.get('spot')}")
    print(f"Is Sanitized: {'verdict' in res_external and 'levels' not in res_external}")
    print(f"Full Keys: {list(res_external.keys())}")
    print(f"Verdict: {res_external.get('verdict')}")
    print(f"Walls Status: {res_external.get('walls_status')}")
    print(f"Reason Brief: {res_external.get('reason_brief')}")

if __name__ == "__main__":
    asyncio.run(run_checks())
