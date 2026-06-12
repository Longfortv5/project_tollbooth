"""
Terrain map — the single shared "where price has been / is / is heading" model.

The signal generator and YU BOTH read terrain:{SYM} from Redis, so they can
never disagree about market structure. One source of truth for the legs.

Pipeline:
  1. LegTracker (single-emit Directional-Change zigzag) turns a tick stream into
     confirmed swing PIVOTS — high, low, high, low... A leg is the move between
     two consecutive pivots. Emits ONCE, on flip confirmation, never per-tick.
  2. From the last N pivots we classify the MAJOR trend:
       lower-highs AND lower-lows -> "down"
       higher-highs AND higher-lows -> "up"
       otherwise -> "range"
  3. Staircase test: are successive same-direction legs progressing past prior
     pivots (trend) or stalling short of them (exhausting)?
  4. Entry plan (the thing you described), trend-aligned only:
       DOWN trend: when a micro UP-leg confirms a LOWER HIGH (fails to reach the
         prior swing high), SHORT the reversal. Target = toward the previous
         swing LOW but fall short (TARGET_REACH of the distance). Stop above the
         lower high + buffer. Nearest wall above acts as the magnet ceiling that
         caps how far the up-leg "should" run — a leg pushing past it invalidates.
       UP trend: mirror (long the higher-low reversal).
       RANGE / counter-trend: no plan (this week's evidence: don't fade trends).

Publish terrain:{SYM} to Redis (raw socket, zero deps). YU reads .plan; the
signal generator reads .pivots/.trend so both share the same terrain.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

# --- tuning (per-symbol overridable) ---------------------------------------
REVERSAL_PCT = 0.0018     # 0.18% retracement confirms a leg flip (DC threshold)
MIN_LEG_PCT = 0.0010      # ignore legs shorter than 0.10%
PIVOT_HISTORY = 8         # how many recent pivots to keep / reason over
TARGET_REACH = 0.85       # target falls short of the prior pivot (85% of the way)
STOP_BUFFER_PTS = 40.0    # stop beyond the reversal pivot
WALL_MAGNET_TOL = 0.0015  # a leg within 0.15% of a wall is "at the magnet"


@dataclass
class Pivot:
    price: float
    kind: str          # "high" | "low"
    ts: str


class LegTracker:
    """Single-emit Directional-Change zigzag. Confirms a pivot only when price
    retraces REVERSAL_PCT from the leg extreme; emits that pivot exactly once."""

    def __init__(self, reversal_pct=REVERSAL_PCT):
        self.reversal_pct = reversal_pct
        self.direction = 0            # 0 unknown, +1 up-leg, -1 down-leg
        self.leg_start = None         # price where current leg began (prior pivot)
        self.extreme = None           # furthest point of current leg
        self.pivots: list[Pivot] = []

    def update(self, price: float, ts: str) -> Pivot | None:
        if self.leg_start is None:
            self.leg_start = price
            self.extreme = price
            self.direction = 0
            return None

        # establish initial direction once price moves enough
        if self.direction == 0:
            if price >= self.leg_start * (1 + MIN_LEG_PCT):
                self.direction = +1; self.extreme = price
            elif price <= self.leg_start * (1 - MIN_LEG_PCT):
                self.direction = -1; self.extreme = price
            return None

        # up-leg: confirm a HIGH pivot when price retraces down by reversal_pct
        if self.direction > 0:
            if price > self.extreme:
                self.extreme = price
                return None
            if price <= self.extreme * (1 - self.reversal_pct):
                piv = Pivot(round(self.extreme, 2), "high", ts)
                self._push(piv)
                self.leg_start = self.extreme   # new down-leg starts from the high
                self.direction = -1
                self.extreme = price
                return piv
            return None

        # down-leg: confirm a LOW pivot when price retraces up by reversal_pct
        if price < self.extreme:
            self.extreme = price
            return None
        if price >= self.extreme * (1 + self.reversal_pct):
            piv = Pivot(round(self.extreme, 2), "low", ts)
            self._push(piv)
            self.leg_start = self.extreme
            self.direction = +1
            self.extreme = price
            return piv
        return None

    def _push(self, piv: Pivot):
        self.pivots.append(piv)
        if len(self.pivots) > PIVOT_HISTORY:
            self.pivots = self.pivots[-PIVOT_HISTORY:]


def classify_trend(pivots: list[Pivot]) -> str:
    highs = [p.price for p in pivots if p.kind == "high"][-3:]
    lows = [p.price for p in pivots if p.kind == "low"][-3:]
    if len(highs) >= 2 and len(lows) >= 2:
        lower_highs = all(highs[i] < highs[i - 1] for i in range(1, len(highs)))
        lower_lows = all(lows[i] < lows[i - 1] for i in range(1, len(lows)))
        higher_highs = all(highs[i] > highs[i - 1] for i in range(1, len(highs)))
        higher_lows = all(lows[i] > lows[i - 1] for i in range(1, len(lows)))
        if lower_highs and lower_lows:
            return "down"
        if higher_highs and higher_lows:
            return "up"
    return "range"


def prior_pivot(pivots: list[Pivot], kind: str) -> Pivot | None:
    for p in reversed(pivots):
        if p.kind == kind:
            return p
    return None


def build_terrain(symbol: str, price: float, tracker: LegTracker,
                  walls: dict | None = None) -> dict:
    """Assemble the shared terrain map + (trend-aligned) entry plan.
    `walls` is the magnet context, e.g. {"call_wall":..,"put_wall":..,"gamma_flip":..}
    already on the instrument's price scale."""
    ts = datetime.now(timezone.utc).isoformat()
    pivots = tracker.pivots
    trend = classify_trend(pivots)
    walls = walls or {}

    cur_dir = "up" if tracker.direction > 0 else "down" if tracker.direction < 0 else "flat"
    leg_pts = (tracker.extreme - tracker.leg_start) if tracker.extreme and tracker.leg_start else 0.0

    def nearest_wall_above(px):
        cands = [w for w in (walls.get("call_wall"), walls.get("gamma_flip")) if w and w > px]
        return min(cands) if cands else None

    def nearest_wall_below(px):
        cands = [w for w in (walls.get("put_wall"), walls.get("gamma_flip")) if w and w < px]
        return max(cands) if cands else None

    plan = {"actionable": False, "reason": "no trend-aligned setup"}

    # DOWN trend: short the lower-high reversal of a micro up-leg
    if trend == "down" and tracker.direction > 0:
        last_high = prior_pivot(pivots, "high")     # the prior swing high
        last_low = prior_pivot(pivots, "low")       # target reference (prev low)
        if last_high and last_low:
            lower_high = tracker.extreme < last_high.price      # failed to reach prior high
            wall = nearest_wall_above(price)
            past_wall = wall is not None and tracker.extreme > wall
            if lower_high and not past_wall:
                dist = tracker.extreme - last_low.price
                plan = {
                    "actionable": True, "side": "SHORT",
                    "reason": f"down-trend, micro up-leg made lower high "
                              f"({tracker.extreme:.1f} < prior high {last_high.price:.1f})",
                    "entry_zone": round(tracker.extreme, 1),
                    "stop": round(tracker.extreme + STOP_BUFFER_PTS, 1),
                    "target": round(tracker.extreme - dist * TARGET_REACH, 1),
                    "target_ref": f"toward prior low {last_low.price:.1f}, "
                                  f"{int(TARGET_REACH*100)}% of the way",
                    "expected_leg_pts": round(dist * TARGET_REACH, 1),
                    "magnet_wall": wall,
                }

    # UP trend: mirror — long the higher-low reversal of a micro down-leg
    if trend == "up" and tracker.direction < 0:
        last_low = prior_pivot(pivots, "low")
        last_high = prior_pivot(pivots, "high")
        if last_low and last_high:
            higher_low = tracker.extreme > last_low.price
            wall = nearest_wall_below(price)
            past_wall = wall is not None and tracker.extreme < wall
            if higher_low and not past_wall:
                dist = last_high.price - tracker.extreme
                plan = {
                    "actionable": True, "side": "LONG",
                    "reason": f"up-trend, micro down-leg made higher low "
                              f"({tracker.extreme:.1f} > prior low {last_low.price:.1f})",
                    "entry_zone": round(tracker.extreme, 1),
                    "stop": round(tracker.extreme - STOP_BUFFER_PTS, 1),
                    "target": round(tracker.extreme + dist * TARGET_REACH, 1),
                    "target_ref": f"toward prior high {last_high.price:.1f}, "
                                  f"{int(TARGET_REACH*100)}% of the way",
                    "expected_leg_pts": round(dist * TARGET_REACH, 1),
                    "magnet_wall": wall,
                }

    return {
        "symbol": symbol, "updated_at": ts, "price": round(price, 2),
        "trend": trend,
        "current_leg": {"direction": cur_dir,
                        "start": round(tracker.leg_start, 2) if tracker.leg_start else None,
                        "extreme": round(tracker.extreme, 2) if tracker.extreme else None,
                        "length_pts": round(leg_pts, 1)},
        "pivots": [asdict(p) for p in pivots],
        "walls": walls,
        "plan": plan,
    }


def publish_terrain(record: dict, host="localhost", port=6379):
    key = f"terrain:{record['symbol']}"
    value = json.dumps(record)
    payload = (f"*3\r\n$3\r\nSET\r\n${len(key)}\r\n{key}\r\n"
               f"${len(value.encode())}\r\n{value}\r\n").encode()
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(payload); resp = s.recv(64); s.close()
    if not resp.startswith(b"+OK"):
        raise RuntimeError(f"redis SET failed: {resp!r}")
