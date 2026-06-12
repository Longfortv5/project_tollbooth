"""
Regime-split analysis v2 — uses LIVE-COMPUTABLE state at signal time, not
day-direction hindsight (v1 tested that; it failed).

Two candidate filters tested against the sweep results:

  1. TRAILING-1H TREND at signal time: direction of the 60 minutes BEFORE
     each signal (what a live gate would actually see). Trades are bucketed
     with / counter / neutral (move below NEUTRAL_PCT of price = no trend).

  2. EXPANSION-CHOP (AVOID) DAYS: per day+symbol, efficiency ratio
     = |net move| / total 5m path, plus session range. Low efficiency with a
     big range = violent two-way day (the June-10 signature). Both metrics
     are printed so the threshold is chosen from evidence, then a default
     flag (eff < 0.25 and range% > 1.0) is applied.

Run ON gb10, same dir as divergence_sweep_all.csv:
    ~/venv/bin/python analyze_trend_split.py
"""

import json
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

NEUTRAL_PCT = 0.0007      # 1h |move|/price below this -> NEUTRAL (no trend)
AVOID_EFF = 0.25          # efficiency below this AND
AVOID_RANGE_PCT = 1.0     # range% above this -> AVOID day


def qdb(sql):
    url = "http://localhost:9000/exec?query=" + urllib.parse.quote(sql)
    return json.loads(urllib.request.urlopen(url, timeout=30).read())["dataset"]


df = pd.read_csv("divergence_sweep_all.csv")
df = df[(df["excluded"].isna()) | (df["excluded"] == "")]
df = df[~df["folded_first_in"]].copy()
df["signal_ts"] = pd.to_datetime(df["signal_ts"], utc=True)

# ---------------------------------------------------------------- 1h trend
def trailing_1h(row):
    t1 = row["signal_ts"]
    t0 = t1 - pd.Timedelta(minutes=60)
    rows = qdb(
        f"SELECT first(mid), last(mid) FROM ig_ticks WHERE symbol='{row['symbol']}' "
        f"AND ts>='{t0.strftime('%Y-%m-%dT%H:%M:%S')}.000000Z' "
        f"AND ts<'{t1.strftime('%Y-%m-%dT%H:%M:%S')}.000000Z'")
    if not rows or rows[0][0] is None:
        return "NEUTRAL"
    o, c = rows[0]
    if abs(c - o) / c < NEUTRAL_PCT:
        return "NEUTRAL"
    return "UP" if c > o else "DOWN"


df["trend_1h"] = df.apply(trailing_1h, axis=1)
df["bucket_1h"] = np.where(
    df["trend_1h"] == "NEUTRAL", "neutral",
    np.where(((df["side"] == "LONG") & (df["trend_1h"] == "UP")) |
             ((df["side"] == "SHORT") & (df["trend_1h"] == "DOWN")),
             "with", "counter"))

# ------------------------------------------------------------- day metrics
day_metrics = {}
for (day, sym), _ in df.groupby(["day", "symbol"]):
    rows = qdb(
        f"SELECT ts, last(mid) m FROM ig_ticks WHERE symbol='{sym}' "
        f"AND ts>='{day}T07:00:00.000000Z' AND ts<'{day}T17:00:00.000000Z' "
        f"SAMPLE BY 5m")
    px = np.array([r[1] for r in rows if r[1] is not None], dtype=float)
    if px.size < 10:
        day_metrics[(day, sym)] = (np.nan, np.nan, False)
        continue
    net = abs(px[-1] - px[0])
    path = np.abs(np.diff(px)).sum()
    eff = net / path if path > 0 else 0.0
    range_pct = 100 * (px.max() - px.min()) / px[-1]
    avoid = (eff < AVOID_EFF) and (range_pct > AVOID_RANGE_PCT)
    day_metrics[(day, sym)] = (round(eff, 3), round(range_pct, 2), avoid)

df["eff"] = df.apply(lambda r: day_metrics[(r["day"], r["symbol"])][0], axis=1)
df["range_pct"] = df.apply(lambda r: day_metrics[(r["day"], r["symbol"])][1], axis=1)
df["avoid_day"] = df.apply(lambda r: day_metrics[(r["day"], r["symbol"])][2], axis=1)

pd.set_option("display.width", 200)

print("=== Day metrics (efficiency = |net|/path; low eff + big range = chop expansion) ===")
dm = (pd.DataFrame([(d, s, *v) for (d, s), v in sorted(day_metrics.items())],
                   columns=["day", "symbol", "eff", "range_pct", "avoid_flag"]))
pnl_day = df.groupby(["day", "symbol"])["pnl_pts"].sum().rename("pnl").reset_index()
print(dm.merge(pnl_day, on=["day", "symbol"], how="left").to_string(index=False))

print("\n=== Split by trailing-1h trend at signal time (live-computable) ===")
print(df.groupby("bucket_1h")["pnl_pts"].agg(["count", "sum", "mean"]).round(1))
print("\n--- per symbol/tf ---")
print(df.groupby(["symbol", "tf", "bucket_1h"])["pnl_pts"]
        .agg(["count", "sum", "mean"]).round(1))

print("\n=== Split by AVOID-day flag ===")
print(df.groupby("avoid_day")["pnl_pts"].agg(["count", "sum", "mean"]).round(1))

print("\n=== Hypothetical filters (first-in PnL per day) ===")
base = df.groupby("day")["pnl_pts"].sum().rename("baseline")
f1 = df[df["bucket_1h"] != "counter"].groupby("day")["pnl_pts"].sum().rename("block_counter_1h")
f2 = df[~df["avoid_day"]].groupby("day")["pnl_pts"].sum().rename("block_avoid_days")
f3 = df[(df["bucket_1h"] != "counter") & (~df["avoid_day"])
        ].groupby("day")["pnl_pts"].sum().rename("both")
out = pd.concat([base, f1, f2, f3], axis=1).fillna(0).round(1)
out.loc["TOTAL"] = out.sum()
print(out.to_string())
