"""
Project Tollbooth - Pre-publish payload validator.

Run before writing to Redis. Two layers:
  1. Internal consistency: schema fields, recomputable derived values, plausibility.
  2. Drift vs previous snapshot: flags metrics moving more than tolerance between runs.

Usage:
    python validate_regime.py payload.json              # consistency only
    python validate_regime.py payload.json prev.json    # consistency + drift

Exit code 0 = publishable, 1 = errors (do not publish). Warnings don't block.
"""

import json
import sys
import time

# Max allowed relative change between consecutive snapshots (fraction).
# Calibrated for ~1-15 min cadence; widen for slower pipelines.
DRIFT_TOLERANCES = {
    "spot": 0.03,
    "regime.gamma_flip": 0.02,
    "exposures_usd.gex": 0.50,
    "exposures_usd.dex": 0.30,
    "exposures_usd.vex": 0.60,
    "exposures_usd.charm": 3.00,    # noisy by nature, but 10x jumps are bugs
    "levels.call_wall": 0.05,
    "levels.put_wall": 0.05,
    "levels.max_pain": 0.05,        # the metric with the worst track record
    "volatility.atm_iv": 0.25,
}

MAX_PAIN_SPOT_BAND = 0.12  # max_pain further than 12% from spot is suspect


def _get(d, dotted):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def validate_consistency(p: dict) -> tuple[list, list]:
    errors, warnings = [], []

    # Required structure
    for field in ["schema_version", "ticker", "timestamp", "spot",
                  "regime", "exposures_usd", "volatility", "levels"]:
        if p.get(field) is None:
            errors.append(f"Missing required field: {field}")
    if errors:
        return errors, warnings

    if p["schema_version"] != "1.0":
        warnings.append(f"Unexpected schema_version: {p['schema_version']}")

    # Staleness at publish time
    age = time.time() - p["timestamp"]
    if age > 300:
        warnings.append(f"Payload already {int(age)}s old at validation time")
    if age < -60:
        errors.append(f"Timestamp is {int(-age)}s in the future")

    spot = p["spot"]
    reg = p["regime"]
    gex = _get(p, "exposures_usd.gex")

    # Gamma sign vs GEX sign
    if gex is not None and reg.get("gamma"):
        expected = "negative" if gex < 0 else "positive"
        if reg["gamma"] != expected:
            errors.append(f"regime.gamma '{reg['gamma']}' contradicts GEX sign ({gex:+.3e})")

    # hedging_effect vs gamma
    he = reg.get("hedging_effect")
    if reg.get("gamma") == "negative" and he not in (None, "amplifying"):
        errors.append(f"hedging_effect '{he}' contradicts negative gamma")
    if reg.get("gamma") == "positive" and he not in (None, "dampening"):
        errors.append(f"hedging_effect '{he}' contradicts positive gamma")

    # Recompute distance_to_flip_pct
    flip = reg.get("gamma_flip")
    dist = reg.get("distance_to_flip_pct")
    if spot and flip and dist is not None:
        expected_dist = round((spot - flip) / flip * 100, 2)
        if abs(expected_dist - dist) > 0.02:
            errors.append(f"distance_to_flip_pct {dist} != recomputed {expected_dist}")

    # Gamma regime vs spot/flip relation
    if spot and flip and reg.get("gamma"):
        side = "negative" if spot < flip else "positive"
        if reg["gamma"] != side:
            warnings.append(f"gamma '{reg['gamma']}' inconsistent with spot {spot} vs flip {flip}")

    # Recompute VRP and skew
    vol = p["volatility"]
    if vol.get("atm_iv") is not None and vol.get("hv20") is not None and vol.get("vrp") is not None:
        expected_vrp = round(vol["atm_iv"] - vol["hv20"], 2)
        if abs(expected_vrp - vol["vrp"]) > 0.02:
            errors.append(f"vrp {vol['vrp']} != atm_iv - hv20 = {expected_vrp}")
    if all(vol.get(k) is not None for k in ("put_iv_25d", "call_iv_25d", "skew_25d")):
        expected_skew = round(vol["put_iv_25d"] - vol["call_iv_25d"], 2)
        if abs(expected_skew - vol["skew_25d"]) > 0.02:
            errors.append(f"skew_25d {vol['skew_25d']} != put - call = {expected_skew}")

    # Levels plausibility
    lev = p["levels"]
    cw, pw, mp = lev.get("call_wall"), lev.get("put_wall"), lev.get("max_pain")
    if cw is not None and pw is not None and cw <= pw:
        errors.append(f"call_wall {cw} <= put_wall {pw}")
    if mp is not None and spot:
        if abs(mp - spot) / spot > MAX_PAIN_SPOT_BAND:
            warnings.append(
                f"max_pain {mp} is {abs(mp - spot) / spot:.1%} from spot {spot} "
                f"(> {MAX_PAIN_SPOT_BAND:.0%}) — check expiry selection"
            )

    # Key strikes sanity
    for ks in (p.get("key_strikes") or []):
        s = ks.get("strike")
        if s and spot and abs(s - spot) / spot > 0.25:
            warnings.append(f"key strike {s} is >25% from spot {spot}")
        if gex is not None and ks.get("net_gex_usd") is not None:
            if abs(ks["net_gex_usd"]) > abs(gex):
                errors.append(f"strike {s} GEX exceeds total net GEX")

    # data_quality honesty
    dq = p.get("data_quality") or {}
    vol_all_null = all(v is None for v in vol.values())
    if dq.get("complete") and (vol_all_null or not p.get("key_strikes")):
        errors.append("data_quality.complete=true but volatility or key_strikes is empty")

    return errors, warnings


def validate_drift(current: dict, previous: dict) -> list:
    warnings = []
    if current.get("ticker") != previous.get("ticker"):
        return [f"Ticker mismatch: {current.get('ticker')} vs {previous.get('ticker')}"]
    dt = current.get("timestamp", 0) - previous.get("timestamp", 0)
    if dt <= 0:
        return [f"Non-increasing timestamp (dt={dt}s)"]

    for path, tol in DRIFT_TOLERANCES.items():
        a, b = _get(previous, path), _get(current, path)
        if a in (None, 0) or b is None:
            continue
        change = abs(b - a) / abs(a)
        if change > tol:
            warnings.append(
                f"DRIFT {path}: {a:+.6g} -> {b:+.6g} ({change:.0%} in {int(dt)}s, tol {tol:.0%})"
            )
    return warnings


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        current = json.load(f)

    errors, warnings = validate_consistency(current)

    if len(sys.argv) > 2:
        with open(sys.argv[2]) as f:
            previous = json.load(f)
        warnings += validate_drift(current, previous)

    for w in warnings:
        print(f"[WARN]  {w}")
    for e in errors:
        print(f"[ERROR] {e}")

    if errors:
        print(f"\nRESULT: DO NOT PUBLISH ({len(errors)} errors, {len(warnings)} warnings)")
        sys.exit(1)
    print(f"\nRESULT: OK to publish ({len(warnings)} warnings)")
    sys.exit(0)


if __name__ == "__main__":
    main()
