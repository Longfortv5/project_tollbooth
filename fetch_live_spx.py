import os
import sys
import json
import urllib.request

# Add project directory to path
sys.path.append(os.path.abspath("/Users/huseyinsanli/.gemini/antigravity/scratch/project_tollbooth"))

from regime_schema import map_flashalpha_to_regime

API_KEY = "si9YcCom6jZqtVVPuUvAzDIQjYo7xD8GEmSEwOEi"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "X-Api-Key": API_KEY,
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    try:
        print("Fetching exposure summary...", file=sys.stderr)
        summary = fetch_json("https://lab.flashalpha.com/v1/exposure/summary/SPX")
        
        print("Fetching exposure levels...", file=sys.stderr)
        levels = fetch_json("https://lab.flashalpha.com/v1/exposure/levels/SPX")
        
        print("Fetching volatility data...", file=sys.stderr)
        vol = fetch_json("https://lab.flashalpha.com/v1/volatility/SPX")
        
        print("Fetching GEX flow by strike...", file=sys.stderr)
        flow = fetch_json("https://lab.flashalpha.com/v1/flow/gex/SPX")
        
        print("Fetching max pain data...", file=sys.stderr)
        maxpain_data = fetch_json("https://lab.flashalpha.com/v1/maxpain/SPX")
        
        # Merge all into flat snapshot dictionary
        snap = {}
        
        # 1. Summary details
        snap["spot"] = summary.get("underlying_price")
        snap["gamma_flip"] = summary.get("gamma_flip")
        snap["regime"] = summary.get("regime")
        
        exposures = summary.get("exposures") or {}
        snap["net_gex"] = exposures.get("net_gex")
        snap["net_dex"] = exposures.get("net_dex")
        snap["net_vex"] = exposures.get("net_vex")
        snap["net_chex"] = exposures.get("net_chex")
        
        interpretation = summary.get("interpretation") or {}
        snap["volatility_state"] = summary.get("volatility_state") or "expansion" # fallback
        snap["tilt"] = interpretation.get("gamma") or "short gamma"
        snap["consensus"] = summary.get("regime")
        
        zero_dte = summary.get("zero_dte") or {}
        snap["zero_dte_gex_share"] = zero_dte.get("pct_of_total_gex")
        
        # 2. Levels details
        lvl_data = levels.get("levels") or {}
        snap["call_wall"] = lvl_data.get("call_wall")
        snap["put_wall"] = lvl_data.get("put_wall")
        snap["gamma_flip_eod"] = lvl_data.get("gamma_flip")
        
        # Pin max pain explicitly to the front (nearest DTE) expiry
        expirations = maxpain_data.get("max_pain_by_expiration") or []
        snap["max_pain"] = expirations[0].get("max_pain_strike") if expirations else None
        
        # 3. Volatility details
        snap["atm_iv"] = vol.get("atm_iv")
        realized = vol.get("realized_vol") or {}
        snap["hv20"] = realized.get("rv_20d")
        snap["hv60"] = realized.get("rv_60d")
        
        spreads = vol.get("iv_rv_spreads") or {}
        snap["vrp"] = spreads.get("vrp_20d")
        
        skew_profiles = vol.get("skew_profiles") or []
        if skew_profiles:
            first_skew = skew_profiles[0]
            snap["put_iv_25d"] = first_skew.get("put_25d_iv")
            snap["call_iv_25d"] = first_skew.get("call_25d_iv")
            snap["skew_25d"] = first_skew.get("skew_25d")
            snap["smile_ratio"] = first_skew.get("smile_ratio")
            
        # 4. Strikes (Key Strikes GEX)
        strikes = flow.get("strikes") or []
        # Sort strikes by absolute net_gex value descending to find key concentration areas
        valid_strikes = []
        for s in strikes:
            net_gex = s.get("net_gex") or 0.0
            valid_strikes.append({
                "strike": s.get("strike"),
                "net_gex": net_gex,
                "call_oi": s.get("call_oi") or 0,
                "put_oi": s.get("put_oi") or 0
            })
        valid_strikes.sort(key=lambda x: abs(x["net_gex"]), reverse=True)
        snap["key_strikes"] = valid_strikes[:5]
        
        # Map flat snapshot to canonical regime payload
        mapped = map_flashalpha_to_regime(snap, "SPX")
        
        # Print final formatted JSON
        print(json.dumps(mapped, indent=2))
        
    except Exception as e:
        print(f"Error building payload: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
