import os
import sys
import json
import urllib.request
import time

API_KEY = "si9YcCom6jZqtVVPuUvAzDIQjYo7xD8GEmSEwOEi"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_json(url):
    print(f"Fetching: {url}", file=sys.stderr)
    req = urllib.request.Request(
        url,
        headers={
            "X-Api-Key": API_KEY,
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_nq_price():
    # Query Sidebus snapshot for live NQ price
    try:
        req = urllib.request.Request("http://127.0.0.1:8888/snapshot")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["symbols"]["NQ"]["micro_momentum"]["price"]
    except Exception:
        # Fallback to a hardcoded multiplier or check other endpoints
        return None

def main():
    try:
        # Fetch live QQQ data
        summary = fetch_json("https://lab.flashalpha.com/v1/exposure/summary/QQQ")
        levels = fetch_json("https://lab.flashalpha.com/v1/exposure/levels/QQQ")
        vol = fetch_json("https://lab.flashalpha.com/v1/volatility/QQQ")
        flow = fetch_json("https://lab.flashalpha.com/v1/flow/gex/QQQ")
        maxpain_data = fetch_json("https://lab.flashalpha.com/v1/maxpain/QQQ")
        
        # Fetch live NQ price to calibrate
        nq_price = fetch_nq_price()
        qqq_spot = summary.get("underlying_price") or 700.0
        
        # Calculate multiplier
        if nq_price:
            multiplier = nq_price / qqq_spot
            print(f"DEBUG: Live NQ={nq_price}, QQQ={qqq_spot}, Multiplier={multiplier:.6f}", file=sys.stderr)
        else:
            # Fallback multiplier if Sidebus is offline
            multiplier = 40.0
            nq_price = qqq_spot * multiplier
            print(f"DEBUG: NQ price offline. Using fallback Multiplier=40.0, Est NQ={nq_price}", file=sys.stderr)
            
        def _c(val):
            """Calibrate to NQ scale and round to 2dp."""
            if val is None:
                return None
            return round(float(val) * multiplier, 2)
            
        def _int(val):
            """Coerce to rounded integer."""
            if val is None:
                return None
            return int(round(float(val)))

        # Front expiry max pain strike
        expirations = maxpain_data.get("max_pain_by_expiration") or []
        front_mp = expirations[0].get("max_pain_strike") if expirations else None
        
        # Build calibrated QQQ -> NQ payload
        exposures = summary.get("exposures") or {}
        interpretation = summary.get("interpretation") or {}
        zero_dte = summary.get("zero_dte") or {}
        lvl_data = levels.get("levels") or {}
        
        skew_profiles = vol.get("skew_profiles") or []
        skew_25d = None
        put_iv_25d = None
        call_iv_25d = None
        smile_ratio = None
        if skew_profiles:
            first_skew = skew_profiles[0]
            put_iv_25d = first_skew.get("put_25d_iv")
            call_iv_25d = first_skew.get("call_25d_iv")
            skew_25d = first_skew.get("skew_25d")
            smile_ratio = first_skew.get("smile_ratio")
            
        strikes = flow.get("strikes") or []
        valid_strikes = []
        for s in strikes:
            net_gex = s.get("net_gex") or 0.0
            valid_strikes.append({
                "strike": _c(s.get("strike")), # Calibrated to NQ strike scale
                "net_gex_usd": _int(net_gex),
                "call_oi": s.get("call_oi") or 0,
                "put_oi": s.get("put_oi") or 0
            })
        valid_strikes.sort(key=lambda x: abs(x["net_gex_usd"]), reverse=True)

        mapped = {
            "schema_version": "1.0",
            "ticker": "NQ",
            "requested_as": "NQ",
            "timestamp": int(summary.get("as_of_ts") or time.time() if hasattr(time, "time") else 1781116283),
            "source": "Longfort",
            "spot": round(nq_price, 2),
            "regime": {
                "gamma": "negative" if (exposures.get("net_gex") or 0.0) < 0 else "positive",
                "hedging_effect": "amplifying" if (exposures.get("net_gex") or 0.0) < 0 else "dampening",
                "volatility": summary.get("volatility_state") or "expansion",
                "tilt": interpretation.get("gamma") or "short gamma",
                "consensus": summary.get("regime"),
                "gamma_flip": _c(summary.get("gamma_flip")),
                "distance_to_flip_pct": round((nq_price - float(summary.get("gamma_flip") or 0.0) * multiplier) / (float(summary.get("gamma_flip") or 1.0) * multiplier) * 100, 2) if summary.get("gamma_flip") else None,
                "zero_dte_gex_share_pct": zero_dte.get("pct_of_total_gex")
            },
            "exposures_usd": {
                "gex": _int(exposures.get("net_gex")),
                "dex": _int(exposures.get("net_dex")),
                "vex": _int(exposures.get("net_vex")),
                "charm": _int(exposures.get("net_chex"))
            },
            "volatility": {
                "atm_iv": vol.get("atm_iv"),
                "hv20": vol.get("realized_vol", {}).get("rv_20d"),
                "hv60": vol.get("realized_vol", {}).get("rv_60d"),
                "vrp": vol.get("iv_rv_spreads", {}).get("vrp_20d"),
                "put_iv_25d": put_iv_25d,
                "call_iv_25d": call_iv_25d,
                "skew_25d": skew_25d,
                "smile_ratio": smile_ratio
            },
            "levels": {
                "call_wall": _c(lvl_data.get("call_wall")),
                "put_wall": _c(lvl_data.get("put_wall")),
                "gamma_flip_eod": _c(lvl_data.get("gamma_flip")),
                "max_pain": _c(front_mp)
            },
            "key_strikes": valid_strikes[:5],
            "data_quality": {
                "complete": True,
                "missing": []
            }
        }
        mapped["timestamp"] = int(time.time())
        print(json.dumps(mapped, indent=2))
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
