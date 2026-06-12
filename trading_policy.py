"""
Project Tollbooth / YU — trading policy, encoded from the 2026-06-08..11
backtest evidence (divergence_sweep_all.csv + trend-split analysis).

EVERY entry decision passes through evaluate_entry(). One choke point.

Evidence -> rules encoded here:
  * DAX 5m/10m: enabled (anchor: +180 pts, 75%, tight MAE over 4 days).
  * NKD 10m: enabled, shadow-collect (n=2, promising). NKD 5m: disabled
    (fat tails, spread-doomed).
  * NQ: BENCHED, not binned (abnormal week; retest on normal data).
  * ES: parked (flat at proportional geometry; no edge demonstrated).
  * 1m: DISPLAY-ONLY everywhere (4-day evidence; DAX 1m flagged for review).
  * NO directional/regime blocking of divergence: counter-trend is the
    signal's nature (+217 counter vs -44 with-trend over 4 days).
  * First-in: first qualifying signal fires; same-direction sisters within
    DEDUPE_WINDOW fold; opposite signal = exit prompt, never auto-flip.
  * Range circuit breaker: if today's range-so-far > BREAKER_MULT x the
    symbol's normal day range, bench the symbol until next session
    (NQ lost every >2%-range day; won the 1.35% day).
  * Manual event bench: human sets Redis key bench:{SYM} = ISO-date (benched
    through that date) or "permanent". Machine obeys, never decides.

Integration points for the droplet agent:
  1. shadow_runner.main(): call evaluate_entry() right where `decision` is
     finalized (before ShadowPosition is built). Pass the live Redis client.
  2. Demote the 1m block in check_symbol_swing_divergence to annotation-only
     (policy will also block it here as a backstop).
  3. Add NKD to the divergence path (currently DAX/NQ only) so NKD 10m
     collects shadow sample.
  4. Live executor stays disabled. This module gates SHADOW entries; the
     future order gateway must consult it again (defense in depth).
"""

from __future__ import annotations

from datetime import datetime, timezone, date

# ----------------------------------------------------------------- ledger
POLICY = {
    #            enabled  tradeable_tfs        target_pts per tf      stop_mult
    "DAX": {"enabled": True,  "tfs": {"5m", "10m"},
            "targets": {"5m": 30.0, "10m": 38.0}, "stop_mult": 1.5,
            "note": "anchor instrument (4-day evidence)"},
    "NKD": {"enabled": True,  "tfs": {"10m"},
            "targets": {"10m": 90.0}, "stop_mult": 1.5,
            "note": "10m shadow-collect; 5m disabled (tails+spread)"},
    "NQ":  {"enabled": False, "tfs": set(), "targets": {}, "stop_mult": 1.5,
            "note": "BENCHED 2026-06-11: abnormal vol week; retest later"},
    "ES":  {"enabled": False, "tfs": set(), "targets": {}, "stop_mult": 1.5,
            "note": "parked: no edge at proportional geometry"},
}

DISPLAY_ONLY_TFS = {"1m"}          # never tradeable, always visible
DEDUPE_WINDOW_MIN = 20             # same-direction fold window
OPPOSITE_SIGNAL_ACTION = "EXIT_PROMPT"
MAX_POSITIONS_PER_SYMBOL = 1
MAX_POSITIONS_TOTAL = 4

# Range circuit breaker. Baselines are PROVISIONAL typical full-day ranges
# (%) until the bars_1s archive accumulates 10-20 normal days; then replace
# with trailing medians.
NORMAL_DAY_RANGE_PCT = {"NQ": 1.2, "DAX": 1.0, "NKD": 1.2, "ES": 0.9}
BREAKER_MULT = 1.5


def range_breaker_tripped(symbol: str, session_high: float,
                          session_low: float, price: float) -> bool:
    """True when today's range-so-far already exceeds BREAKER_MULT x normal."""
    base = NORMAL_DAY_RANGE_PCT.get(symbol)
    if not base or not price:
        return False
    range_pct = 100.0 * (session_high - session_low) / price
    return range_pct > BREAKER_MULT * base


def manual_bench_until(redis_client, symbol: str):
    """Reads bench:{SYM}. Returns None (not benched), date, or 'permanent'."""
    if redis_client is None:
        return None
    try:
        val = redis_client.get(f"bench:{symbol}")
    except Exception:
        return None          # Redis down -> no manual bench (breaker still applies)
    if not val:
        return None
    val = val.decode() if isinstance(val, bytes) else str(val)
    if val.strip().lower() == "permanent":
        return "permanent"
    try:
        return date.fromisoformat(val.strip()[:10])
    except ValueError:
        return "permanent"   # unparseable -> fail safe: stay benched


def evaluate_entry(symbol: str, timeframe: str, side: str,
                   session_high: float, session_low: float, price: float,
                   open_positions_symbol: int, open_positions_total: int,
                   redis_client=None,
                   now_utc: datetime | None = None) -> dict:
    """The single entry gate. Returns
    {"allowed": bool, "reason": str, "target_pts": float|None, "stop_pts": float|None}
    Fail-closed: anything unknown is blocked with a reason.
    """
    now = now_utc or datetime.now(timezone.utc)

    def block(reason):
        return {"allowed": False, "reason": reason,
                "target_pts": None, "stop_pts": None}

    pol = POLICY.get(symbol)
    if pol is None:
        return block(f"{symbol}: not in policy ledger")
    if timeframe in DISPLAY_ONLY_TFS:
        return block(f"{timeframe} is display-only")
    if not pol["enabled"]:
        return block(f"{symbol}: disabled ({pol['note']})")
    if timeframe not in pol["tfs"]:
        return block(f"{symbol} {timeframe}: not an enabled timeframe")
    if side not in ("LONG", "SHORT"):
        return block(f"invalid side {side!r}")

    bench = manual_bench_until(redis_client, symbol)
    if bench == "permanent":
        return block(f"{symbol}: manually benched (permanent)")
    if isinstance(bench, date) and now.date() <= bench:
        return block(f"{symbol}: manually benched through {bench.isoformat()}")

    if range_breaker_tripped(symbol, session_high, session_low, price):
        return block(f"{symbol}: range breaker tripped "
                     f"(range-so-far > {BREAKER_MULT}x normal day)")

    if open_positions_symbol >= MAX_POSITIONS_PER_SYMBOL:
        return block(f"{symbol}: position cap (symbol) reached")
    if open_positions_total >= MAX_POSITIONS_TOTAL:
        return block("total position cap reached")

    target = pol["targets"][timeframe]
    return {"allowed": True, "reason": "ok",
            "target_pts": target,
            "stop_pts": round(target * pol["stop_mult"], 1)}
