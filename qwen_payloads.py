"""
QWEN payload schemas v1.0 — TWO distinct formats, TWO objectives.

FORMAT 1: yu_trade_verdict   (event-driven; built when a divergence signal fires)
FORMAT 2: desk_note          (cron; market synthesis, no trading authority)

Hard rules encoded here:
- Trading format carries IG prices ONLY. FlashAlpha contributes context
  (gamma sign, walls), never price levels for the traded instrument.
- Symbol mapping: NQ->QQQ (direct proxy), ES->SPX (direct proxy),
  NKD->SPX (WEAK proxy: gamma sign only, never levels),
  DAX->EWG (positioning proxy: gamma sign only, never levels).
- FA validity: stale after 20:00 UTC until next US open. When stale, all
  FA-derived fields are emitted with valid=false and QWEN is instructed
  to ignore them. YU then runs pure divergence+spatial mode.
- Entry trigger: ANY single divergence signal on 5m+ fires (no combined
  proof). 1m divergence is display-only (W%R value + price level).
- Cross-timeframe dedupe: same-direction divergence on multiple TFs within
  DEDUPE_WINDOW = one trade.
- Targets: never wall-to-wall. target = entry +/- min(K_SESSION * ATR(tf),
  0.35 * room_to_wall); skip trade if R:R < RR_FLOOR.
- ES: tradeable only in EOD window AND positive gamma (config, not prompt).
- QWEN role in Format 1: VETO GATE. It may BLOCK or REDUCE with evidence
  from the payload. It may never generate entries or upgrade size.
"""

from datetime import datetime, timezone, time as dtime

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Symbol & proxy configuration
# ---------------------------------------------------------------------------

PROXY_MAP = {
    # trade_symbol: (fa_symbol, relationship)
    "NQ":  ("QQQ", "direct"),       # levels usable when fresh (scaled)
    "ES":  ("SPX", "direct"),       # levels usable when fresh
    "NKD": ("SPX", "gamma_only"),   # NEVER use levels; gamma sign only
    "DAX": ("EWG", "gamma_only"),   # NEVER use levels; gamma sign only
}

TRADEABLE_TIMEFRAMES = {"5m", "10m", "30m", "1h"}   # 1m is display-only

# ENTRY POLICY: FIRST IN. The first qualifying signal fires IMMEDIATELY —
# no waiting for confluence or higher-TF confirmation. Subsequent
# same-direction signals within the window are folded into the open
# position's record (deduped_timeframes); they never add size or positions.
# An OPPOSITE-direction 5m+ signal while a position is open is an
# early-exit prompt (re-evaluate at market), never an automatic flip.
ENTRY_POLICY = "FIRST_IN"
DEDUPE_WINDOW_MIN = 20
OPPOSITE_SIGNAL_ACTION = "EXIT_PROMPT"

# Target policy
K_SESSION = {"US_OPEN": 2.5, "OFF_HOURS": 1.5}
ROOM_FRACTION_CAP = 0.35
RR_FLOOR = 1.2

# Regime -> strategy mode. Divergence FADING is mean-reversion: forbidden in
# negative gamma. In TREND mode, only signals matching trend_side are allowed.
REGIME_MODE = {
    "positive": "MEAN_REVERT",   # fade divergence at structure (zones)
    "negative": "TREND",         # with-trend signals only; wider/trailing targets
    None:       "SPATIAL_ONLY",  # FA stale: divergence + native structure only
}

# Walls/pivots are ZONES, never lines: level +/- WALL_ZONE_ATR * ATR(tf).
# Act on reaction (rejection + indicator turn), never on touch.
# A broken wall flips role only after acceptance: ACCEPTANCE_BARS closes beyond.
WALL_ZONE_ATR = 0.5
ACCEPTANCE_BARS = 2

# Counter-trend position caught in negative gamma: cut at half stop, don't ride.
COUNTER_TREND_CUT_R = 0.5


def trend_side(spot: float, flip: float | None, vwap_slope: float | None = None) -> str | None:
    """Trend direction in negative gamma: below flip -> SHORT side, above -> LONG.
    Falls back to vwap slope sign when flip unavailable."""
    if flip is not None:
        return "SHORT" if spot < flip else "LONG"
    if vwap_slope is not None:
        return "LONG" if vwap_slope > 0 else "SHORT"
    return None


def classify_day(pct_in_range: float, session_range: float, atr_daily: float) -> str:
    """Observable day-type: feeds the verdict payload so 'a day like Tuesday'
    is a field, not a feeling."""
    if session_range >= 1.3 * atr_daily:
        if pct_in_range >= 0.75:
            return "TREND_UP"
        if pct_in_range <= 0.25:
            return "TREND_DOWN"
    if session_range <= 0.6 * atr_daily:
        return "PIN"
    return "RANGE"

# ES special rule
ES_EOD_WINDOW_UTC = (dtime(18, 30), dtime(20, 0))     # tradeable window
ES_REQUIRES_GAMMA = "positive"

# FA validity: stale outside US options hours (13:30–20:00 UTC, Mon–Fri)
US_OPEN_UTC, US_CLOSE_UTC = dtime(13, 30), dtime(20, 0)


def fa_is_live(now_utc: datetime) -> bool:
    if now_utc.weekday() >= 5:
        return False
    return US_OPEN_UTC <= now_utc.time() <= US_CLOSE_UTC


def session_window(now_utc: datetime) -> str:
    return "US_OPEN" if fa_is_live(now_utc) else "OFF_HOURS"


# ---------------------------------------------------------------------------
# FORMAT 1: yu_trade_verdict — built ON EVERY 5m+ DIVERGENCE SIGNAL
# ---------------------------------------------------------------------------

YU_TRADE_VERDICT_EXAMPLE = {
    "format": "yu_trade_verdict",
    "schema_version": SCHEMA_VERSION,
    "generated_at": "2026-06-11T16:40:13Z",

    "session": {
        "window": "US_OPEN",            # US_OPEN | OFF_HOURS
        "fa_live": True,                # False => ALL fa_* fields invalid
        "fa_age_sec": 41,
        "target_multiplier": 2.5,       # K_SESSION applied
        "regime_mode": "TREND",         # MEAN_REVERT | TREND | SPATIAL_ONLY
        "trend_side": "SHORT",          # in TREND mode: only this side tradeable
        "day_type": "TREND_DOWN",       # classify_day(): TREND_UP/DOWN | RANGE | PIN
    },

    "instrument": {
        "trade_symbol": "NQ",
        "venue": "IG",
        "price": 28883.3,               # IG price ONLY. Never Rithmic, never FA spot.
        "price_age_ms": 50,
        "price_source": "IG",           # if Rithmic failover active: "RITHMIC_DELTA"
        "basis_offset": 0.0,            # rolling IG-Rithmic basis, informational
    },

    "signal": {                          # THE trigger. One signal is sufficient.
        "type": "WPR_DIVERGENCE",        # WPR_DIVERGENCE | RSI_DIVERGENCE
        "side": "bearish",
        "timeframe": "5m",               # guaranteed in TRADEABLE_TIMEFRAMES
        "wpr": -37.15,
        "rsi": 58.2,
        "price_at_signal": 28890.1,
        "detected_at": "2026-06-11T16:40:13Z",
        "deduped_timeframes": ["10m"],   # same-direction sister signals folded in
    },

    "display_only": {                    # 1m kept visible, never tradeable
        "wpr_1m": -22.4,
        "rsi_1m": 61.0,
        "divergence_1m": {"side": "bearish", "wpr": -22.4, "price_level": 28895.0},
    },

    "spatial": {                         # native structure — ALWAYS valid, all symbols
        "session_high": 28960.0,
        "session_low": 28790.0,
        "pct_in_range": 0.55,
        "prior_high": 28930.0,
        "prior_low": 28640.0,
        "pivot": 28815.0,
        "r1": 28990.0,
        "s1": 28700.0,
        "atr_tf": 38.5,                  # ATR of the signal timeframe, IG points
    },

    "fa_context": {                      # proxy-derived; honor 'valid' and 'relationship'
        "fa_symbol": "QQQ",
        "relationship": "direct",        # direct | gamma_only
        "valid": True,                   # False after 20:00 UTC / weekend
        "gamma": "negative",
        # Levels below are ALREADY rescaled to IG via level_rescale_ratio.
        # External Tollbooth schema v1.0 keeps exchange-true spot; the rescale
        # is YU-internal only. Walls are zones (level +/- 0.5*atr_tf), not lines.
        "level_rescale_ratio": 1.00072,  # ig_spot / exchange_spot, today's basis
        "gamma_flip": 71736.0,           # IG-scale ONLY if relationship=direct, else null
        "call_wall": 72000.0,            # null when relationship=gamma_only
        "put_wall": 70000.0,             # null when relationship=gamma_only
        "eod_pin_est": 70500.0,          # day's pin estimate, IG-scale, zone not point
        "dealer_regime_decision": "bearish_trend",
        "dealer_regime_score": -0.62,
        "pin_score": 0.31,
    },

    "risk": {                            # YU's proposal — QWEN judges, never improves
        "proposed_side": "SHORT",
        "proposed_entry": 28883.3,
        "proposed_stop": 28928.3,
        "proposed_target": 28795.0,      # min(K*ATR, 0.35*room) — never wall-to-wall
        "rr": 1.96,
        "room_to_wall": 183.3,
        "open_positions_symbol": 0,
        "open_positions_total": 1,
        "max_positions": 4,
    },
}

YU_VERDICT_PROMPT = """You are a risk veto gate for a single proposed futures trade.
Judge ONLY from the JSON payload. Hard rules:
- You may BLOCK or REDUCE. You may NEVER generate a trade, raise size, or extend targets.
- If session.fa_live is false or fa_context.valid is false, IGNORE every fa_context field.
- If fa_context.relationship is "gamma_only", use ONLY fa_context.gamma (sign); all level
  fields there are null and walls do NOT exist for this instrument.
- REGIME MODE (revised 2026-06-11 from 4-day backtest evidence: counter-trend
  divergence is the signal's NATURE and its edge — never block on direction):
  * Do NOT block a divergence proposal for being counter-trend. Backtested
    counter-trend trades outperformed with-trend trades.
  * session.regime_mode == "TREND" (negative gamma): REDUCE size and require
    risk.proposed_target to be volatility-scaled (target >= 1.5 * spatial.atr_tf
    is suspect on a high-range day — flag geometry, not direction).
  * session.regime_mode == "SPATIAL_ONLY": judge purely on spatial.* and signal.*.
- WALLS ARE ZONES, not lines: a wall occupies level +/- 0.5 * spatial.atr_tf.
  A touch proves nothing. BLOCK fades INTO a zone in TREND mode. A broken wall
  has flipped role only if price closed beyond it for 2+ bars (acceptance);
  otherwise treat it as intact.
- Use spatial.* for structure. Do not invent structures absent from the payload.
- BLOCK if: proposal fades a valid wall zone in TREND mode, signal direction fights
  trend_side in TREND mode, price/data is stale, risk.rr < 1.2, or
  open_positions_total >= max_positions.
- REDUCE if: pct_in_range is extreme against the trade, or day_type contradicts the
  proposal (e.g., RANGE day breakout trade).
Respond ONLY with JSON: {"verdict":"ALLOW|REDUCE|BLOCK","confidence":0.0-1.0,
"reasons":["...max 3, each citing a payload field"],"max_hold_minutes":int}"""


# ---------------------------------------------------------------------------
# FORMAT 2: desk_note — cron synthesis (no trading authority)
# ---------------------------------------------------------------------------

DESK_NOTE_EXAMPLE = {
    "format": "desk_note",
    "schema_version": SCHEMA_VERSION,
    "generated_at": "2026-06-11T16:45:00Z",
    "fa_live": True,
    "symbols": {
        "SPX": {
            "snapshot_age_sec": 41,
            "spot": 7305.16,
            "regime": "negative_gamma",
            "netgex": -49.9e9, "net_dex": 650.6e9, "net_vex": 56.1e9,
            "gamma_flip": 7400.78, "call_wall": 7500.0, "put_wall": 7000.0,
            "vanna_wall": 7450.0,
            "volatility": {"atm_iv": 20.76, "vrp": 7.23, "skew_25d": 3.89},
            "dealer_regime_decision": "bearish_trend",
            "dealer_regime_score": -0.62,
            "pin": {"pin_score": 0.31, "magnet_strike": 7300.0},
            "oi_change": {"calls": 120000, "puts": 310000},
        },
        # QQQ / DAX(EWG) / NKD(SPX-proxy, gamma sign only) follow same shape
    },
    "indicator_summary": {               # NEW: the desk note finally sees your signal
        "NQ":  {"wpr_5m": -37.2, "rsi_5m": 58.2, "active_divergences": ["5m bearish"]},
        "DAX": {"wpr_5m": -41.0, "rsi_5m": 52.1, "active_divergences": []},
    },
}

DESK_NOTE_PROMPT = """You are a quantitative desk analyst. Two sentences maximum.
Synthesize regime from the JSON state only: converging magnets, regime flips, and
divergence signals listed in indicator_summary. Ignore any symbol whose snapshot_age_sec
exceeds 900 or where fa_live is false. Do not mention structures (sweeps, retests,
reversals) unless a field in the payload directly evidences them. Plain prose."""


# ---------------------------------------------------------------------------
# Builder skeletons (integration: fill data access, keep contracts intact)
# ---------------------------------------------------------------------------

def compute_target(entry: float, side: str, atr_tf: float, room_to_wall: float,
                   window: str) -> float | None:
    """Session-aware capped target. Returns None if R:R floor unreachable."""
    k = K_SESSION[window]
    dist = min(k * atr_tf, ROOM_FRACTION_CAP * room_to_wall)
    target = entry + dist if side == "LONG" else entry - dist
    return target


def build_yu_trade_verdict(signal: dict, ig_quote: dict, spatial: dict,
                           fa_row: dict | None, positions: dict,
                           now_utc: datetime | None = None) -> dict:
    """Assemble Format 1. Callers guarantee ig_quote is IG-sourced."""
    now = now_utc or datetime.now(timezone.utc)
    window = session_window(now)
    sym = signal["symbol"]
    fa_sym, relationship = PROXY_MAP[sym]
    live = fa_is_live(now) and fa_row is not None

    gamma = (fa_row or {}).get("regime_gamma") if live else None
    mode = REGIME_MODE.get(gamma, "SPATIAL_ONLY")
    t_side = None
    if mode == "TREND":
        t_side = trend_side(ig_quote.get("price"),
                            (fa_row or {}).get("gamma_flip_ig_scale"))

    fa_context = {
        "fa_symbol": fa_sym, "relationship": relationship, "valid": live,
        "gamma": (fa_row or {}).get("regime_gamma"),
        "gamma_flip": (fa_row or {}).get("gamma_flip") if relationship == "direct" and live else None,
        "call_wall": (fa_row or {}).get("call_wall") if relationship == "direct" and live else None,
        "put_wall": (fa_row or {}).get("put_wall") if relationship == "direct" and live else None,
        "dealer_regime_decision": (fa_row or {}).get("dealer_regime_decision") if live else None,
        "dealer_regime_score": (fa_row or {}).get("dealer_regime_score") if live else None,
        "pin_score": (fa_row or {}).get("pin_score") if live else None,
    }

    return {
        "format": "yu_trade_verdict",
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "session": {"window": window, "fa_live": live,
                    "fa_age_sec": (fa_row or {}).get("age_sec"),
                    "target_multiplier": K_SESSION[window],
                    "regime_mode": mode,
                    "trend_side": t_side,
                    "day_type": spatial.pop("day_type", None)},
        "instrument": ig_quote,          # {trade_symbol, venue:"IG", price, price_age_ms, price_source, basis_offset}
        "signal": signal,
        "display_only": spatial.pop("display_only", {}),
        "spatial": spatial,
        "fa_context": fa_context,
        "risk": positions,               # caller computes proposal via compute_target()
    }
