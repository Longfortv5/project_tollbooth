"""
Multi-day divergence sweep — runs backtest_divergence.py once per trading day
(00:00 -> 18:00 UK each day), aggregates all trades, prints per-day and
overall summaries.

Run ON gb10 (same dir as backtest_divergence.py):
    ~/venv/bin/python sweep_divergence.py --start 2026-06-08 --end 2026-06-11 \
        --tfs 5m,10m --exclude 16:30-17:10:2026-06-11

Note --exclude here takes an optional :YYYY-MM-DD suffix so a news window
(e.g. the tweet) only applies to its own day.
"""

import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

UK = ZoneInfo("Europe/London")


def uk_window(d: date):
    s = datetime(d.year, d.month, d.day, 0, 0, tzinfo=UK)
    e = datetime(d.year, d.month, d.day, 18, 0, tzinfo=UK)
    to_utc = lambda x: x.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")
    return to_utc(s), to_utc(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--tfs", default="5m,10m")
    ap.add_argument("--symbols", default="NQ,DAX,NKD")
    ap.add_argument("--stop-mult", type=float, default=1.5)
    ap.add_argument("--timeout-min", type=int, default=120)
    ap.add_argument("--exclude", action="append", default=[],
                    help="HH:MM-HH:MM or HH:MM-HH:MM:YYYY-MM-DD (day-specific)")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    d0 = date.fromisoformat(args.start)
    d1 = date.fromisoformat(args.end)
    frames = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:                       # skip weekends
            since, until = uk_window(d)
            out = f"div_{d.isoformat()}.csv"
            cmd = [args.python, "backtest_divergence.py",
                   "--since", since, "--until", until,
                   "--tfs", args.tfs, "--symbols", args.symbols,
                   "--stop-mult", str(args.stop_mult),
                   "--timeout-min", str(args.timeout_min),
                   "--out", out, "--quiet"]
            for ex in args.exclude:
                # day suffix is the LAST colon-separated token iff it's a date
                head, sep, tail = ex.rpartition(":")
                if sep and len(tail) == 10 and tail.count("-") == 2:
                    if tail != d.isoformat():
                        continue
                    cmd += ["--exclude", head]
                else:
                    cmd += ["--exclude", ex]
            print(f"\n##### {d.isoformat()} #####")
            r = subprocess.run(cmd, capture_output=True, text=True)
            # show only the summary part of each day's output
            tail = r.stdout.split("== ALL SIGNALS", 1)
            print(("== ALL SIGNALS" + tail[1]) if len(tail) == 2 else r.stdout[-500:])
            if r.returncode == 0:
                try:
                    day_df = pd.read_csv(out)
                    day_df["day"] = d.isoformat()
                    frames.append(day_df)
                except Exception:
                    pass
            else:
                print(r.stderr[-500:])
        d += timedelta(days=1)

    if not frames:
        print("\nNo trades across sweep.")
        return
    allf = pd.concat(frames, ignore_index=True)
    allf.to_csv("divergence_sweep_all.csv", index=False)
    valid = allf[allf["excluded"].isna() | (allf["excluded"] == "")]
    fi = valid[~valid["folded_first_in"]]

    def summarize(d, label):
        g = d.groupby(["symbol", "tf"]).agg(
            n=("pnl_pts", "size"),
            wins=("outcome", lambda s: (s == "TARGET").sum()),
            total_pts=("pnl_pts", "sum"),
            avg_pts=("pnl_pts", "mean"),
            med_mae=("mae", "median"),
            p90_mae=("mae", lambda s: s.quantile(0.10)),
        ).round(1)
        g["winrate%"] = (100 * g["wins"] / g["n"]).round(0)
        print(f"\n===== {label} =====\n{g.to_string()}")

    print(f"\n{'#'*60}\nSWEEP TOTAL: {len(allf)} signals over "
          f"{allf['day'].nunique()} days -> divergence_sweep_all.csv")
    summarize(valid, "ALL SIGNALS (all days, ex-excluded)")
    summarize(fi, "FIRST-IN ONLY (all days)")
    per_day = fi.groupby("day")["pnl_pts"].agg(["count", "sum"]).round(1)
    print(f"\n===== FIRST-IN per day =====\n{per_day.to_string()}")


if __name__ == "__main__":
    main()
