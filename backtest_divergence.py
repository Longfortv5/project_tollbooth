"""
Divergence backtest — IG 1s ticks from QuestDB, walk-forward, no lookahead.

Run ON gb10 (QuestDB at localhost:9000):
    scp backtest_divergence.py longfort-gb10:~/ && ssh longfort-gb10 \
        "~/venv/bin/python backtest_divergence.py --exclude 16:30-17:10"

What it does:
  * Pulls 1s mid ticks for NQ, DAX, NKD since midnight UK (Europe/London).
  * Builds 5m/10m (optionally 30m/1h) OHLC bars; computes RSI(14) + W%R(14).
  * Detects CONFIRMED swing divergences bar-by-bar (same pivot/midpoint logic
    as shadow_runner), using only data available at each bar close.
  * Each signal becomes a timestamped trade simulated on the 1s tick stream:
    entry = first tick after signal bar close; exit = target / stop / timeout.
  * Exclusions: first 20 min after each market open (NKD 00:00, DAX 07:00,
    NQ 13:30 UTC) and any --exclude HH:MM-HH:MM windows (e.g. news).
  * Reports: every trade row + per symbol/TF summary, both "all signals"
    and "first-in" (later same-direction signals folded while a virtual
    position is open).

Targets (points): NQ/DAX 5m=30, 10m=38 (yours). NKD and 30m/1h are MY
ASSUMPTIONS pending your numbers — override in TARGETS below or via CLI.
Stops default to 1.5x target (--stop-mult). MAE/MFE reported per trade so
the right stop can be chosen from evidence.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

QDB = "http://localhost:9000/exec"
SYMBOLS = ["NQ", "DAX", "NKD"]   # override with --symbols

TARGETS = {  # points ("1m" from your shadow params; 30m/1h/NKD/ES are assumptions)
    "1m":  {"NQ": 15.0, "DAX": 8.0,  "NKD": 30.0,  "ES": 4.0},
    "5m":  {"NQ": 30.0, "DAX": 30.0, "NKD": 70.0,  "ES": 8.0},
    "10m": {"NQ": 38.0, "DAX": 38.0, "NKD": 90.0,  "ES": 10.0},
    "30m": {"NQ": 55.0, "DAX": 55.0, "NKD": 130.0, "ES": 15.0},
    "1h":  {"NQ": 80.0, "DAX": 80.0, "NKD": 180.0, "ES": 22.0},
}
TF_SECONDS = {"1m": 60, "5m": 300, "10m": 600, "30m": 1800, "1h": 3600}

MARKET_OPEN_UTC = {"NKD": "00:00", "DAX": "07:00", "NQ": "13:30"}
OPEN_EXCLUDE_MIN = 20


def qdb(sql: str) -> pd.DataFrame:
    url = f"{QDB}?query={urllib.parse.quote(sql)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.loads(r.read().decode())
    cols = [c["name"] for c in data.get("columns", [])]
    return pd.DataFrame(data.get("dataset", []), columns=cols)


def fetch_ticks(symbol: str, since_utc: datetime, until_utc: datetime) -> pd.DataFrame:
    # Downsample to 1-second last-price inside QuestDB (raw stream is ~300/s).
    sql = (f"SELECT ts, last(mid) AS mid FROM ig_ticks WHERE symbol='{symbol}' "
           f"AND ts >= '{since_utc.strftime('%Y-%m-%dT%H:%M:%S')}.000000Z' "
           f"AND ts < '{until_utc.strftime('%Y-%m-%dT%H:%M:%S')}.000000Z' "
           f"SAMPLE BY 1s")
    df = qdb(sql)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["mid"] = pd.to_numeric(df["mid"], errors="coerce")
    return df.dropna().reset_index(drop=True)


def make_bars(ticks: pd.DataFrame, tf: str) -> pd.DataFrame:
    s = ticks.set_index("ts")["mid"]
    o = s.resample(f"{TF_SECONDS[tf]}s", label="left", closed="left").ohlc().dropna()
    o.columns = ["open", "high", "low", "close"]
    return o


def rsi_series(close: pd.Series, period=14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-9))


def wpr_series(h, l, c, period=14) -> pd.Series:
    hh = h.rolling(period).max()
    ll = l.rolling(period).min()
    return -100 * (hh - c) / (hh - ll + 1e-9)


def detect_confirmed(df: pd.DataFrame, price_type: str, ind_col: str,
                     lookback=40) -> str:
    """shadow_runner's confirmed-divergence logic, verbatim semantics."""
    if len(df) < lookback + 5:
        return ""
    prices = (df["low"] if price_type == "low" else df["high"]).values
    ind = df[ind_col].values
    pivots = []
    start, end = len(df) - 2, max(len(df) - lookback - 2, 1)
    for i in range(start, end, -1):
        if price_type == "low":
            if prices[i] < prices[i - 1] and prices[i] < prices[i + 1]:
                pivots.append((i, prices[i], ind[i]))
        else:
            if prices[i] > prices[i - 1] and prices[i] > prices[i + 1]:
                pivots.append((i, prices[i], ind[i]))
    if len(pivots) < 2:
        return ""
    t2i, t2p, t2v = pivots[0]
    t1i, t1p, t1v = pivots[1]
    if abs(t2i - t1i) < 3:
        if len(pivots) >= 3:
            t1i, t1p, t1v = pivots[2]
        else:
            return ""
    if t2i != len(df) - 2:           # confirmed only
        return ""
    thresh = 3.0 if ind_col == "RSI" else 10.0
    if price_type == "low" and t2p < t1p and t2v > t1v:
        if (ind_col == "RSI" and t1v <= 35) or (ind_col == "W%R" and t1v <= -80):
            mid = ind[t1i + 1:t2i]
            if len(mid) and mid.max() > t1v + thresh and mid.max() > t2v + thresh:
                return "Bullish"
    if price_type == "high" and t2p > t1p and t2v < t1v:
        if (ind_col == "RSI" and t1v >= 65) or (ind_col == "W%R" and t1v >= -20):
            mid = ind[t1i + 1:t2i]
            if len(mid) and mid.min() < t1v - thresh and mid.min() < t2v - thresh:
                return "Bearish"
    return ""


def in_excluded(ts: pd.Timestamp, symbol: str, extra: list) -> str | None:
    """Open-time exclusions are GLOBAL: when any of the three markets opens,
    all three instruments sit out OPEN_EXCLUDE_MIN minutes."""
    hm = ts.strftime("%H:%M")
    for mkt, o in MARKET_OPEN_UTC.items():
        oh, om = map(int, o.split(":"))
        open_dt = ts.replace(hour=oh, minute=om, second=0, microsecond=0)
        if open_dt <= ts < open_dt + timedelta(minutes=OPEN_EXCLUDE_MIN):
            return f"{mkt}-open+{OPEN_EXCLUDE_MIN}m"
    for a, b in extra:
        if a <= hm < b:
            return f"excl {a}-{b}"
    return None


def simulate(ts_np, px_np, entry_ts, side, entry, target_pts, stop_pts,
             timeout_min):
    """Vectorized fill simulation on naive-UTC numpy arrays.
    Returns (outcome, exit_ts tz-aware, pnl, mfe, mae) or None."""
    entry_np = np.datetime64(entry_ts.tz_convert("UTC").tz_localize(None))
    deadline_np = entry_np + np.timedelta64(timeout_min, "m")
    i0 = int(np.searchsorted(ts_np, entry_np, side="right"))
    if i0 >= len(ts_np):
        return None
    i1 = int(np.searchsorted(ts_np, deadline_np, side="right"))
    seg = px_np[i0:i1]
    if seg.size == 0:
        return None
    tgt = entry + target_pts if side == "LONG" else entry - target_pts
    stp = entry - stop_pts if side == "LONG" else entry + stop_pts
    if side == "LONG":
        hit_t = np.flatnonzero(seg >= tgt)
        hit_s = np.flatnonzero(seg <= stp)
    else:
        hit_t = np.flatnonzero(seg <= tgt)
        hit_s = np.flatnonzero(seg >= stp)
    ti = int(hit_t[0]) if hit_t.size else None
    si = int(hit_s[0]) if hit_s.size else None
    if si is not None and (ti is None or si <= ti):   # tie -> conservative: stop
        end, outcome, pnl = si, "STOP", -stop_pts
    elif ti is not None:
        end, outcome, pnl = ti, "TARGET", target_pts
    else:
        end = seg.size - 1
        outcome = "TIMEOUT" if i1 < len(ts_np) else "EOD"
        pnl = (seg[end] - entry) if side == "LONG" else (entry - seg[end])
    upto = seg[:end + 1]
    fav = (upto - entry) if side == "LONG" else (entry - upto)
    mfe, mae = float(fav.max(initial=0.0)), float(fav.min(initial=0.0))
    exit_ts = pd.Timestamp(ts_np[i0 + end]).tz_localize("UTC")
    return (outcome, exit_ts, float(pnl), mfe, mae)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", default="5m,10m")
    ap.add_argument("--stop-mult", type=float, default=1.5)
    ap.add_argument("--timeout-min", type=int, default=120)
    ap.add_argument("--exclude", action="append", default=[],
                    help="UTC window HH:MM-HH:MM (repeatable), e.g. news")
    ap.add_argument("--since", default=None, help="ISO UTC override (window start)")
    ap.add_argument("--until", default=None, help="ISO UTC override (window end)")
    ap.add_argument("--out", default="divergence_backtest_trades.csv")
    ap.add_argument("--quiet", action="store_true", help="summaries only, no trade table")
    ap.add_argument("--symbols", default="NQ,DAX,NKD")
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    tfs = [t.strip() for t in args.tfs.split(",")]
    extra_excl = []
    for w in args.exclude:
        parts = w.split("-")
        if len(parts) != 2:
            sys.exit(f"Bad --exclude window '{w}' (expected HH:MM-HH:MM)")
        extra_excl.append((parts[0], parts[1]))

    uk_today = datetime.now(ZoneInfo("Europe/London")).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    else:
        since = uk_today.astimezone(timezone.utc)                    # 00:00 UK
    if args.until:
        until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
    else:
        until = uk_today.replace(hour=18).astimezone(timezone.utc)   # 18:00 UK

    # Pre-warm: fetch enough history BEFORE the window so indicators and the
    # divergence lookback are fully warmed at window start (50 bars of the
    # largest timeframe requested).
    warmup_start = since - timedelta(seconds=50 * max(TF_SECONDS[t] for t in tfs))

    print(f"Window {since.isoformat()} -> {until.isoformat()} "
          f"(warmup from {warmup_start.isoformat()}) | TFs {tfs} | "
          f"stop = {args.stop_mult}x target | timeout {args.timeout_min}m")
    if extra_excl:
        print(f"Excluding UTC windows: {extra_excl}")

    trades = []
    since_ts = pd.Timestamp(since)
    until_ts = pd.Timestamp(until)
    for sym in symbols:
        ticks = fetch_ticks(sym, warmup_start, until)
        if ticks.empty:
            print(f"[{sym}] no ticks — skipped")
            continue
        print(f"[{sym}] {len(ticks)} 1s ticks "
              f"{ticks['ts'].iloc[0]} -> {ticks['ts'].iloc[-1]}")
        ts_np = ticks["ts"].dt.tz_convert("UTC").dt.tz_localize(None).values
        px_np = ticks["mid"].to_numpy(dtype=float)
        open_until = {}          # first-in tracker: side -> exit time
        for tf in tfs:
            bars = make_bars(ticks, tf)
            if len(bars) < 50:
                continue
            bars["RSI"] = rsi_series(bars["close"])
            bars["W%R"] = wpr_series(bars["high"], bars["low"], bars["close"])
            tgt = TARGETS[tf][sym]
            stp = round(tgt * args.stop_mult, 1)
            for i in range(46, len(bars) + 1):
                win = bars.iloc[:i]
                bar_close_ts = win.index[-1] + pd.Timedelta(seconds=TF_SECONDS[tf])
                # Evaluate only inside the test window (warmup bars feed the
                # indicators but never generate trades themselves).
                if bar_close_ts < since_ts or bar_close_ts >= until_ts:
                    continue
                for ind in ("RSI", "W%R"):
                    for ptype, side_name in (("low", "LONG"), ("high", "SHORT")):
                        d = detect_confirmed(win, ptype, ind)
                        if not d:
                            continue
                        skip = in_excluded(bar_close_ts, sym, extra_excl)
                        entry = win["close"].iloc[-1]
                        res = simulate(ts_np, px_np, bar_close_ts, side_name,
                                       entry, tgt, stp, args.timeout_min)
                        if res is None:
                            continue
                        outcome, exit_ts, pnl, mfe, mae = res
                        key = (sym, side_name)
                        folded = (key in open_until
                                  and bar_close_ts < open_until[key])
                        if not folded and not skip:
                            open_until[key] = exit_ts
                        trades.append({
                            "symbol": sym, "tf": tf, "indicator": ind,
                            "side": side_name, "divergence": d,
                            "signal_ts": str(bar_close_ts),
                            "entry": round(float(entry), 2),
                            "target_pts": tgt, "stop_pts": stp,
                            "outcome": outcome, "pnl_pts": round(float(pnl), 1),
                            "mfe": round(float(mfe), 1),
                            "mae": round(float(mae), 1),
                            "excluded": skip or "",
                            "folded_first_in": folded,
                        })

    if not trades:
        print("No signals found.")
        sys.exit(0)

    df = pd.DataFrame(trades).sort_values("signal_ts")
    df.to_csv(args.out, index=False)
    print(f"\n{len(df)} signals -> {args.out}\n")
    pd.set_option("display.width", 200)
    if not args.quiet:
        print(df[["signal_ts", "symbol", "tf", "indicator", "side", "entry",
                  "outcome", "pnl_pts", "mfe", "mae", "excluded",
                  "folded_first_in"]].to_string(index=False))

    def summary(d, label):
        if d.empty:
            print(f"\n== {label}: no trades ==")
            return
        g = d.groupby(["symbol", "tf"]).agg(
            n=("pnl_pts", "size"),
            wins=("outcome", lambda s: (s == "TARGET").sum()),
            total_pts=("pnl_pts", "sum"),
            avg_pts=("pnl_pts", "mean"),
            med_mae=("mae", "median"),
            p90_mae=("mae", lambda s: s.quantile(0.10)),
        ).round(1)
        g["winrate%"] = (100 * g["wins"] / g["n"]).round(0)
        print(f"\n== {label} ==\n{g.to_string()}")

    valid = df[df["excluded"] == ""]
    summary(valid, "ALL SIGNALS (ex-excluded windows)")
    summary(valid[~valid["folded_first_in"]], "FIRST-IN ONLY")
    summary(df[df["excluded"] != ""], "EXCLUDED WINDOWS (for reference)")


if __name__ == "__main__":
    main()
