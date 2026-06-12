"""
Project Tollbooth - Canonical regime payload schema (v1.0) and FlashAlpha mapper.

The value stored at Redis key `current_regime:{ticker}` MUST be the JSON
serialization of the structure produced by `map_flashalpha_to_regime()`.

Design rules:
- `timestamp` is epoch seconds UTC (required; main.py staleness check reads it).
- All exposure values are USD notional floats (e.g., -5.52e9 = -$5.520B).
- All vol values are percentage points (30.73 = 30.73%).
- `key_strikes` is capped at MAX_KEY_STRIKES to bound payload size (agents pay per call).
- Tickers are canonical ETF/index symbols; futures-style queries are resolved
  via TICKER_ALIASES before the Redis lookup.
"""

import time

SCHEMA_VERSION = "1.0"
MAX_KEY_STRIKES = 5

# Canonical launch symbols (Phase 1: index complex, Phase 2: commodities)
CANONICAL_TICKERS = {"QQQ", "SPY", "SPX", "IWM", "GLD", "USO", "SPCX"}

# Futures / colloquial aliases -> canonical symbol.
TICKER_ALIASES = {
    "NQ": "QQQ",
    "NDX": "QQQ",
    "ES": "SPY",
    "RTY": "IWM",
    "GOLD": "GLD",
    "GC": "GLD",
    "WTI": "USO",
    "CL": "USO",
    "OIL": "USO",
}


def resolve_ticker(ticker: str) -> str:
    """Resolves an alias to its canonical symbol (after main.py regex validation)."""
    t = ticker.strip().upper()
    return TICKER_ALIASES.get(t, t)


def _f(value, default=None):
    """Coerce to float, tolerating None / strings with $ , % B M suffixes."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    mult = 1.0
    if s.endswith(("B", "b")):
        mult, s = 1e9, s[:-1]
    elif s.endswith(("M", "m")):
        mult, s = 1e6, s[:-1]
    elif s.endswith(("K", "k")):
        mult, s = 1e3, s[:-1]
    try:
        return float(s) * mult
    except (TypeError, ValueError):
        return default

def _int(value, default=None):
    """Coerce to rounded integer."""
    v = _f(value, default)
    if v is None:
        return default
    return int(round(v))

def map_flashalpha_to_regime(snapshot: dict, ticker: str, timestamp: float | None = None) -> dict:
    """
    Maps a FlashAlpha detailed options snapshot (flat dict) to the canonical
    Tollbooth regime payload (schema v1.0).

    Expected snapshot keys (all optional; missing -> null in output):
      spot, gamma_flip, gamma_flip_eod, regime, zero_dte_gex_share,
      net_gex, net_dex, net_vex, net_chex,
      atm_iv, hv20, hv60, put_iv_25d, call_iv_25d, skew_25d, smile_ratio,
      call_wall, put_wall, max_pain,
      volatility_state, tilt, consensus,
      key_strikes: [{strike, net_gex, call_oi, put_oi}, ...]
    """
    canonical = resolve_ticker(ticker)
    spot = _f(snapshot.get("spot"))
    flip = _f(snapshot.get("gamma_flip"))

    # Derive gamma regime from net GEX sign if not provided explicitly
    net_gex = _f(snapshot.get("net_gex"))
    regime_raw = snapshot.get("regime")
    if regime_raw:
        gamma_regime = "negative" if "NEG" in str(regime_raw).upper() else "positive"
    elif net_gex is not None:
        gamma_regime = "negative" if net_gex < 0 else "positive"
    else:
        gamma_regime = None

    distance_to_flip_pct = None
    if spot and flip:
        distance_to_flip_pct = round((spot - flip) / flip * 100, 2)

    atm_iv = _f(snapshot.get("atm_iv"))
    hv20 = _f(snapshot.get("hv20"))
    vrp = _f(snapshot.get("vrp"))
    if vrp is None and atm_iv is not None and hv20 is not None:
        vrp = round(atm_iv - hv20, 2)

    put_iv = _f(snapshot.get("put_iv_25d"))
    call_iv = _f(snapshot.get("call_iv_25d"))
    skew = _f(snapshot.get("skew_25d"))
    if skew is None and put_iv is not None and call_iv is not None:
        skew = round(put_iv - call_iv, 2)

    key_strikes = []
    for item in (snapshot.get("key_strikes") or [])[:MAX_KEY_STRIKES]:
        if not isinstance(item, dict):
            continue
        key_strikes.append({
            "strike": _f(item.get("strike")),
            "net_gex_usd": _f(item.get("net_gex")),
            "call_oi": int(_f(item.get("call_oi"), 0) or 0),
            "put_oi": int(_f(item.get("put_oi"), 0) or 0),
        })

    def _r2(x):
        return round(x, 2) if isinstance(x, float) else x

    # Authoritative hedging-effect flag derived from gamma sign — never trust the
    # upstream `tilt` string for this (feeds have emitted "dampening" under
    # negative gamma, which is contradictory).
    hedging_effect = None
    if gamma_regime == "negative":
        hedging_effect = "amplifying"
    elif gamma_regime == "positive":
        hedging_effect = "dampening"

    # Self-declared completeness so buyers can see exactly what they're getting
    vol_fields = {"atm_iv": atm_iv, "hv20": hv20, "hv60": _f(snapshot.get("hv60")),
                  "put_iv_25d": put_iv, "call_iv_25d": call_iv,
                  "skew_25d": skew, "smile_ratio": _f(snapshot.get("smile_ratio"))}
    missing = []
    if all(v is None for v in vol_fields.values()):
        missing.append("volatility")
    if not key_strikes:
        missing.append("key_strikes")
    if _f(snapshot.get("zero_dte_gex_share")) is None:
        missing.append("zero_dte_gex_share_pct")

    # Construct zero_dte block
    zero_dte_expiration = snapshot.get("zero_dte_expiration")
    zero_dte_gex_share = _f(snapshot.get("zero_dte_gex_share"))
    pin_score = _int(snapshot.get("pin_score"))
    pin_magnet_strike = _f(snapshot.get("pin_magnet_strike"))
    pin_distance_pct = _f(snapshot.get("pin_distance_pct"))

    zero_dte = None
    if zero_dte_gex_share is not None or pin_score is not None or pin_magnet_strike is not None:
        zdte_verdict = "LOW_PIN_RISK"
        if pin_score is not None:
            if pin_score >= 70 and (pin_distance_pct is None or abs(pin_distance_pct) <= 0.5):
                zdte_verdict = "STRONG_PIN_POTENTIAL"
            elif pin_score >= 40 and (pin_distance_pct is None or abs(pin_distance_pct) <= 1.5):
                zdte_verdict = "MODERATE_PIN_POTENTIAL"
            elif pin_score < 30 or (pin_distance_pct is not None and abs(pin_distance_pct) > 2.0):
                zdte_verdict = "TRENDING_EXPANSION"
        
        zero_dte = {
            "expiration": zero_dte_expiration,
            "gex_share_pct": zero_dte_gex_share,
            "pin_score": pin_score,
            "pin_magnet": pin_magnet_strike,
            "distance_to_magnet_pct": pin_distance_pct,
            "verdict": zdte_verdict,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "ticker": canonical,
        "requested_as": ticker.strip().upper(),
        "timestamp": int(timestamp if timestamp is not None else time.time()),
        "source": "Longfort",
        "spot": spot,
        "regime": {
            "gamma": gamma_regime,                                  # "negative" | "positive"
            "hedging_effect": hedging_effect,                       # derived from gamma sign
            "volatility": snapshot.get("volatility_state"),         # e.g. "expansion"
            "tilt": snapshot.get("tilt"),                           # upstream string, informational
            "consensus": snapshot.get("consensus"),                 # e.g. "bearish_trend"
            "gamma_flip": _r2(flip),
            "distance_to_flip_pct": distance_to_flip_pct,
            "zero_dte_gex_share_pct": zero_dte_gex_share,
        },
        "exposures_usd": {
            "gex": _int(net_gex),
            "dex": _int(snapshot.get("net_dex")),
            "vex": _int(snapshot.get("net_vex")),
            "charm": _int(snapshot.get("net_chex")),
        },
        "volatility": {
            "atm_iv": atm_iv,
            "hv20": hv20,
            "hv60": _f(snapshot.get("hv60")),
            "vrp": vrp,
            "put_iv_25d": put_iv,
            "call_iv_25d": call_iv,
            "skew_25d": skew,
            "smile_ratio": _f(snapshot.get("smile_ratio")),
        },
        "levels": {
            "call_wall": _r2(_f(snapshot.get("call_wall"))),
            "put_wall": _r2(_f(snapshot.get("put_wall"))),
            "gamma_flip_eod": _r2(_f(snapshot.get("gamma_flip_eod"))),
            "max_pain": _r2(_f(snapshot.get("max_pain"))),
        },
        "key_strikes": key_strikes,
        "zero_dte": zero_dte,
        "data_quality": {
            "complete": not missing,
            "missing": missing,
        },
    }


def from_exposure_api(obj: dict) -> dict:
    """Adapter: converts the nested exposure-API shape
    ({"exposure_summary": {...}, "exposure_levels": {...}}) into the flat
    snapshot dict that map_flashalpha_to_regime() consumes.
    Use: map_flashalpha_to_regime(from_exposure_api(raw), ticker, ts)
    where ts should come from exposure_summary.as_of (exchange truth)."""
    summ = obj.get("exposure_summary") or {}
    lev = (obj.get("exposure_levels") or {}).get("levels") or {}
    exp = summ.get("exposures") or {}
    return {
        "spot": summ.get("underlying_price"),
        "gamma_flip": summ.get("gamma_flip"),
        "gamma_flip_eod": lev.get("gamma_flip"),
        "regime": summ.get("regime"),
        "net_gex": exp.get("net_gex"),
        "net_dex": exp.get("net_dex"),
        "net_vex": exp.get("net_vex"),
        "net_chex": exp.get("net_chex"),
        "call_wall": lev.get("call_wall") or lev.get("max_positive_gamma"),
        "put_wall": lev.get("put_wall") or lev.get("max_negative_gamma"),
        "zero_dte_magnet": lev.get("zero_dte_magnet"),
    }


def parse_as_of(obj: dict) -> float | None:
    """Epoch seconds from exposure_summary.as_of (handles 7-digit fractions)."""
    from datetime import datetime
    raw = (obj.get("exposure_summary") or {}).get("as_of")
    if not raw:
        return None
    s = str(raw).rstrip("Z")
    if "." in s:                       # trim to microseconds for fromisoformat
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(s + "+00:00").timestamp()
    except ValueError:
        return None


# Example canonical payload (what a paying agent receives), built from the
# 2026-06-09 QQQ FlashAlpha snapshot:
EXAMPLE_PAYLOAD = {
    "schema_version": "1.0",
    "ticker": "QQQ",
    "requested_as": "NQ",
    "timestamp": 1781287522,
    "source": "Longfort",
    "spot": 700.81,
    "regime": {
        "gamma": "negative",
        "hedging_effect": "amplifying",
        "volatility": "expansion",
        "tilt": "GEX negative",
        "consensus": "bearish_trend",
        "gamma_flip": 717.36,
        "distance_to_flip_pct": -2.31,
        "zero_dte_gex_share_pct": 25.4,
    },
    "exposures_usd": {"gex": -5.52e9, "dex": 1.9771e10, "vex": -4.704e9, "charm": 5.121e6},
    "volatility": {
        "atm_iv": 30.73, "hv20": 24.14, "hv60": 21.72, "vrp": 6.59,
        "put_iv_25d": 46.87, "call_iv_25d": 38.4, "skew_25d": 8.47, "smile_ratio": 1.221,
    },
    "levels": {"call_wall": 720.0, "put_wall": 700.0, "gamma_flip_eod": 717.13, "max_pain": 691.0},
    "key_strikes": [
        {"strike": 700.0, "net_gex_usd": -1.027e9, "call_oi": 108108, "put_oi": 286899},
        {"strike": 705.0, "net_gex_usd": -4.42355e8, "call_oi": 42211, "put_oi": 111965},
        {"strike": 680.0, "net_gex_usd": -3.8657e8, "call_oi": 67406, "put_oi": 205807},
        {"strike": 660.0, "net_gex_usd": -3.48996e8, "call_oi": 79264, "put_oi": 323185},
        {"strike": 650.0, "net_gex_usd": -3.32874e8, "call_oi": 72873, "put_oi": 258359},
    ],
    "zero_dte": {
        "expiration": "2026-06-09",
        "gex_share_pct": 25.4,
        "pin_score": 54,
        "pin_magnet": 700.0,
        "distance_to_magnet_pct": -0.12,
        "verdict": "MODERATE_PIN_POTENTIAL"
    },
    "data_quality": {"complete": True, "missing": []},
}
