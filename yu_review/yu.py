"""
YU - Main Orchestrator and Controller Daemon for the Python Trading Bot.
Coordinates telemetry streams, options ingestion, spatial geometry updates,
wall calibration, and sovereign quantitative model arbitrations.
"""
import os
import sys
import time
import signal
import sqlite3
import subprocess
import threading
import json
import datetime
import requests
import yaml
import asyncio
import fa_state_reader
import pricing_provider
import urllib.parse
import collections
from collections import deque
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live

from fa_pollers import snapshot, flow_live, flow_curves, pin_risk, zero_dte, \
    flow_signals, levels, maxpain, volatility, adv_volatility, vrp, narrative, \
    surface, account, health

POLLER_REGISTRY = {
    "snapshot": snapshot.poll,
    "stock_summary": snapshot.poll,
    "flow_live": flow_live.poll,
    "flow_curves": flow_curves.poll,
    "flow_pin_risk": pin_risk.poll,
    "exposure_zero_dte": zero_dte.poll,
    "flow_signals": flow_signals.poll,
    "flow_signals_summary": flow_signals.poll_summary,
    "exposure_levels": levels.poll,
    "maxpain": maxpain.poll,
    "volatility": volatility.poll,
    "adv_volatility": adv_volatility.poll,
    "vrp": vrp.poll,
    "exposure_narrative": narrative.poll,
    "surface": surface.poll,
    "account": account.poll,
    "health": health.poll,
}

config_lock = threading.Lock()
current_config = None

def load_poller_config(path="/home/longfort/.config/longfort/fa_polling.yaml"):
    global current_config
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
        with config_lock:
            current_config = cfg
        log_msg("SCHEDULER", f"Config loaded successfully from {path}")
    except Exception as e:
        log_msg("SCHEDULER-ERROR", f"Failed to load config from {path}: {e}")

def handle_sighup(signum, frame):
    log_msg("SYSTEM", "SIGHUP received. Reloading scheduler configuration...")
    load_poller_config()

signal.signal(signal.SIGHUP, handle_sighup)

ENGINE_DIR = "/home/longfort/.gemini/antigravity/py_engine"
DB_PATH = os.path.join(ENGINE_DIR, "hud_state.db")
PYTHON_BIN = "/home/longfort/venv/bin/python3"

active_processes = {}
shutdown_event = threading.Event()

log_feed = deque(maxlen=20)
interactive_hud = False
live_hud = None
hud_layout = None
us_proxy_data = {}
global_dgx_data = {}
nq_calibrated_data = {}
global_indicators = {
    sym: {
        "price": None,
        "10s_wpr": None,
        "1m_rsi": None,
        "1m_rsi_dir": "FLAT",
        "5m_rsi": None,
        "5m_rsi_dir": "FLAT",
        "15m_rsi": None,
        "15m_rsi_dir": "FLAT"
    }
    for sym in ["NQ", "DAX", "NKD", "ES"]
}

def log_msg(prefix, msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] [{prefix}] {msg}"
    log_feed.append(formatted)
    
    if interactive_hud and hud_layout:
        try:
            logs_text = Text("\n".join(list(log_feed)))
            hud_layout["body"].update(Panel(logs_text, title="[Execution & Alert Feed]"))
        except Exception:
            pass
    else:
        print(formatted, flush=True)

def handle_shutdown(signum, frame):
    log_msg("SYSTEM", f"Shutdown signal ({signum}) received. Propagating to all subsystems...")
    shutdown_event.set()
    
    if live_hud:
        try:
            live_hud.stop()
        except Exception:
            pass
            
    for name, proc in list(active_processes.items()):
        if proc.poll() is None:
            log_msg("SYSTEM", f"Terminating process: {name}...")
            proc.terminate()
            
    for name, proc in list(active_processes.items()):
        try:
            proc.wait(timeout=5)
            log_msg("SYSTEM", f"Process {name} exited successfully.")
        except subprocess.TimeoutExpired:
            log_msg("SYSTEM", f"Process {name} did not exit in time. Killing it...")
            proc.kill()
            
    log_msg("SYSTEM", "All subsystems cleaned up. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

def resample_bars(prices_1s, seconds_per_bar, num_bars):
    total_needed = num_bars * seconds_per_bar
    if len(prices_1s) < total_needed:
        return None, None, None
    
    highs = []
    lows = []
    closes = []
    for i in range(num_bars):
        start_idx = len(prices_1s) - (num_bars - i) * seconds_per_bar
        end_idx = start_idx + seconds_per_bar
        slice_p = prices_1s[start_idx:end_idx]
        highs.append(max(slice_p))
        lows.append(min(slice_p))
        closes.append(slice_p[-1])
    return highs, lows, closes

def resample_to_ohlc(history, timeframe_secs, num_bars):
    if not history:
        return [], [], []
        
    buckets = collections.defaultdict(list)
    for ts, price in history:
        bucket_ts = int(ts.timestamp()) // timeframe_secs * timeframe_secs
        buckets[bucket_ts].append(price)
        
    sorted_ts = sorted(buckets.keys())
    if not sorted_ts:
        return [], [], []
        
    start_ts = sorted_ts[0]
    end_ts = sorted_ts[-1]
    
    current_ts = start_ts
    all_buckets = []
    last_close = buckets[start_ts][-1]
    
    while current_ts <= end_ts:
        if current_ts in buckets:
            prices = buckets[current_ts]
            bar_high = max(prices)
            bar_low = min(prices)
            bar_close = prices[-1]
            last_close = bar_close
        else:
            bar_high = last_close
            bar_low = last_close
            bar_close = last_close
            
        all_buckets.append((bar_high, bar_low, bar_close))
        current_ts += timeframe_secs
        
    target_bars = all_buckets[-num_bars:]
    if len(target_bars) < num_bars:
        first_bar = target_bars[0] if target_bars else (last_close, last_close, last_close)
        padding = [first_bar] * (num_bars - len(target_bars))
        target_bars = padding + target_bars
        
    highs = [b[0] for b in target_bars]
    lows = [b[1] for b in target_bars]
    closes = [b[2] for b in target_bars]
    
    return highs, lows, closes

def compute_rsi_from_closes(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def compute_rsi_series_from_closes(closes, period=14):
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    rsi_series = []
    for i in range(period, len(closes)):
        rsi_val = compute_rsi_from_closes(closes[:i+1], period)
        rsi_series.append(rsi_val)
    return rsi_series

def compute_sma_series(values, period=9):
    if len(values) < period:
        return [sum(values) / len(values) if values else 50.0] * len(values)
    sma_series = []
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        sma_series.append(sum(window) / period)
    return sma_series

def insert_indicator_state(symbol, timeframe, rsi, rsi_direction, wpr):
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    sql = (
        f"INSERT INTO indicator_state (ts, symbol, timeframe, rsi, rsi_direction, wpr) "
        f"VALUES ('{now_str}', '{symbol}', '{timeframe}', {rsi}, '{rsi_direction}', {wpr})"
    )
    url = f"http://localhost:9000/exec?query={urllib.parse.quote(sql)}"
    try:
        requests.get(url, timeout=1.0)
    except Exception:
        pass

def indicator_daemon_loop():
    log_msg("INDICATOR", "Starting live IndicatorDaemon loop inside YU...")
    
    symbols = ["NQ", "DAX", "NKD", "ES"]
    histories = {sym: collections.deque(maxlen=40000) for sym in symbols}
    
    for sym in symbols:
        try:
            sql = f"SELECT ts, last(mid) as close FROM ig_ticks WHERE symbol='{sym}' AND ts > dateadd('h', -11, now()) SAMPLE BY 1s FILL(prev)"
            url = f"http://localhost:9000/exec?query={requests.utils.quote(sql)}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                dataset = data.get('dataset', [])
                for row in dataset:
                    try:
                        ts = datetime.datetime.fromisoformat(row[0].replace('Z', '+00:00'))
                    except Exception:
                        ts = datetime.datetime.now(datetime.timezone.utc)
                    histories[sym].append((ts, float(row[1])))
                log_msg("INDICATOR", f"Warmed up {sym} history with {len(histories[sym])} 1s bars from QuestDB.")
        except Exception as e:
            log_msg("INDICATOR-ERROR", f"Failed to warm up indicator history for {sym}: {e}")
            
    prev_values = {
        sym: {
            "10s_wpr": None,
            "1m_rsi": None,
            "5m_rsi": None,
            "15m_rsi": None
        }
        for sym in symbols
    }
    
    while not shutdown_event.is_set():
        start_time = time.time()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        
        for sym in symbols:
            try:
                price = pricing_provider.get_latest_price(sym)
                if price is not None:
                    global_indicators[sym]["price"] = price
                    histories[sym].append((now_utc, price))
                    
                    prices_1s = [p for ts, p in histories[sym]]
                    
                    if len(histories[sym]) >= 140:
                        highs, lows, closes = resample_to_ohlc(histories[sym], 10, 14)
                        if closes:
                            hh = max(highs)
                            ll = min(lows)
                            curr_close = closes[-1]
                            denom = hh - ll
                            wpr = ((hh - curr_close) / denom * -100.0) if denom != 0 else -50.0
                            
                            global_indicators[sym]["10s_wpr"] = wpr
                            if prev_values[sym]["10s_wpr"] is None or abs(wpr - prev_values[sym]["10s_wpr"]) > 1e-4:
                                insert_indicator_state(sym, "10s", 0.0, "FLAT", wpr)
                                prev_values[sym]["10s_wpr"] = wpr
                                
                    if len(histories[sym]) >= 2100:
                        _, _, closes = resample_to_ohlc(histories[sym], 60, 35)
                        if closes:
                            rsi = compute_rsi_from_closes(closes, 14)
                            global_indicators[sym]["1m_rsi"] = rsi
                            if prev_values[sym]["1m_rsi"] is None or abs(rsi - prev_values[sym]["1m_rsi"]) > 1e-4:
                                direction = "FLAT"
                                if prev_values[sym]["1m_rsi"] is not None:
                                    diff = rsi - prev_values[sym]["1m_rsi"]
                                    direction = "RISING" if diff > 1e-4 else ("FALLING" if diff < -1e-4 else "FLAT")
                                global_indicators[sym]["1m_rsi_dir"] = direction
                                insert_indicator_state(sym, "1m", rsi, direction, 0.0)
                                prev_values[sym]["1m_rsi"] = rsi
                                
                    if len(histories[sym]) >= 10800:
                        _, _, closes = resample_to_ohlc(histories[sym], 300, 36)
                        if closes:
                            closes = closes[:-1]
                            rsi_series = compute_rsi_series_from_closes(closes, 14)
                            sma_series = compute_sma_series(rsi_series, 9)
                            rsi = rsi_series[-1]
                            rsi_sma = sma_series[-1]
                            global_indicators[sym]["5m_rsi"] = rsi
                            direction = "RISING" if rsi > rsi_sma else "FALLING"
                            if prev_values[sym]["5m_rsi"] is None or abs(rsi - prev_values[sym]["5m_rsi"]) > 1e-4:
                                global_indicators[sym]["5m_rsi_dir"] = direction
                                insert_indicator_state(sym, "5m", rsi, direction, 0.0)
                                prev_values[sym]["5m_rsi"] = rsi
                                
                    if len(histories[sym]) >= 32400:
                        _, _, closes = resample_to_ohlc(histories[sym], 900, 36)
                        if closes:
                            closes = closes[:-1]
                            rsi_series = compute_rsi_series_from_closes(closes, 14)
                            sma_series = compute_sma_series(rsi_series, 9)
                            rsi = rsi_series[-1]
                            rsi_sma = sma_series[-1]
                            global_indicators[sym]["15m_rsi"] = rsi
                            direction = "RISING" if rsi > rsi_sma else "FALLING"
                            if prev_values[sym]["15m_rsi"] is None or abs(rsi - prev_values[sym]["15m_rsi"]) > 1e-4:
                                global_indicators[sym]["15m_rsi_dir"] = direction
                                insert_indicator_state(sym, "15m", rsi, direction, 0.0)
                                prev_values[sym]["15m_rsi"] = rsi
                                
            except Exception as e:
                pass
                
        elapsed = time.time() - start_time
        sleep_time = max(0.1, 1.0 - elapsed)
        time.sleep(sleep_time)

def run_pivots_startup_check():
    """Runs fetch_ig_pivots.py once on startup to ensure daily pivots are in place."""
    script_path = os.path.join(ENGINE_DIR, "fetch_ig_pivots.py")
    log_msg("STARTUP", "Running daily pivots check...")
    try:
        res = subprocess.run([PYTHON_BIN, script_path], capture_output=True, text=True, timeout=30)
        for line in res.stdout.splitlines():
            if line.strip():
                log_msg("PIVOTS", line)
        if res.returncode != 0:
            log_msg("PIVOTS-ERROR", f"Pivots check failed (code {res.returncode}): {res.stderr}")
    except Exception as e:
        log_msg("PIVOTS-ERROR", f"Failed to execute pivots check: {e}")

def monitor_telemetry_stream():
    """Spawns and monitors the telemetry quote stream (ig_handler.py stream) with backoff."""
    script_path = os.path.join(ENGINE_DIR, "ig_handler.py")
    restart_delay = 5
    
    while not shutdown_event.is_set():
        log_msg("TELEMETRY", "Starting quote telemetry stream...")
        start_time = time.time()
        try:
            proc = subprocess.Popen(
                [PYTHON_BIN, script_path, "stream"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=ENGINE_DIR
            )
            active_processes["telemetry_stream"] = proc
            
            for line in iter(proc.stdout.readline, ""):
                if shutdown_event.is_set():
                    break
                stripped = line.strip()
                if stripped:
                    if "[STREAM]" in stripped or "[STREAM_ERROR]" in stripped:
                        log_msg("TELEMETRY", stripped)
            
            proc.stdout.close()
            return_code = proc.wait()
            log_msg("TELEMETRY", f"Telemetry stream exited with code: {return_code}")
            
            if time.time() - start_time > 60:
                restart_delay = 5
            else:
                restart_delay = min(restart_delay * 2, 60)
                
        except Exception as e:
            log_msg("TELEMETRY-ERROR", f"Exception in telemetry stream: {e}")
            restart_delay = min(restart_delay * 2, 60)
            
        if shutdown_event.is_set():
            break
            
        log_msg("TELEMETRY", f"Telemetry stream disconnected. Restarting in {restart_delay} seconds...")
        for _ in range(restart_delay):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def monitor_rithmic_stream():
    """Spawns and monitors the Rithmic L2 stream (rithmic_handler.py stream) with backoff."""
    script_path = os.path.join(ENGINE_DIR, "rithmic_handler.py")
    restart_delay = 5
    
    while not shutdown_event.is_set():
        log_msg("RITHMIC", "Starting Rithmic L2 stream...")
        start_time = time.time()
        try:
            proc = subprocess.Popen(
                [PYTHON_BIN, script_path, "stream"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=ENGINE_DIR
            )
            active_processes["rithmic_stream"] = proc
            
            for line in iter(proc.stdout.readline, ""):
                if shutdown_event.is_set():
                    break
                stripped = line.strip()
                if stripped:
                    log_msg("RITHMIC", stripped)
            
            proc.stdout.close()
            return_code = proc.wait()
            log_msg("RITHMIC", f"Rithmic stream exited with code: {return_code}")
            
            if time.time() - start_time > 60:
                restart_delay = 5
            else:
                restart_delay = min(restart_delay * 2, 60)
                
        except Exception as e:
            log_msg("RITHMIC-ERROR", f"Exception in Rithmic stream: {e}")
            restart_delay = min(restart_delay * 2, 60)
            
        if shutdown_event.is_set():
            break
            
        log_msg("RITHMIC", f"Rithmic stream disconnected. Restarting in {restart_delay} seconds...")
        for _ in range(restart_delay):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def log_historical_coverage():
    """Probe historical API at boot - log what's available for each symbol."""
    try:
        from fa_client import FAClient
        hist = FAClient(mode="historical")
        for sym in ["SPY", "SPX", "QQQ", "EWG"]:
            try:
                cov = hist._request("/v1/tickers", {"symbol": sym})
                log_msg("FA_HIST_COV", f"{sym}: {cov.get('coverage', 'no_coverage')}")
            except Exception as e:
                log_msg("FA_HIST_COV", f"{sym}: not_backfilled ({e})")
    except Exception as e:
        log_msg("FA_HIST_COV", f"probe failed: {e}")

def fa_scheduler_loop():
    """Sequential poller scheduling thread."""
    log_msg("SCHEDULER", "Flash Alpha poller scheduler thread started.")
    last_run = {}
    
    load_poller_config()
    
    while not shutdown_event.is_set():
        with config_lock:
            config = current_config
            
        if not config:
            for _ in range(10):
                if shutdown_event.is_set():
                    return
                time.sleep(0.5)
            continue
            
        now = time.time()
        pollers = config.get("pollers", [])
        
        for p in pollers:
            if shutdown_event.is_set():
                break
                
            name = p.get("name")
            endpoint = p.get("endpoint")
            symbols = p.get("symbols", [])
            cadence = p.get("cadence_sec", 60)
            
            poll_func = POLLER_REGISTRY.get(endpoint) or POLLER_REGISTRY.get(name)
            if not poll_func:
                log_msg("SCHEDULER-ERROR", f"Unknown endpoint or poller name: {endpoint or name}")
                continue
                
            for sym in symbols:
                if shutdown_event.is_set():
                    break
                    
                key = (name, sym)
                last_time = last_run.get(key, 0.0)
                
                if now - last_time >= cadence:
                    try:
                        log_msg("SCHEDULER", f"Polling {endpoint or name} for {sym}...")
                        row_count = poll_func(sym)
                        log_msg("SCHEDULER", f"Successfully polled {endpoint or name} for {sym}. Rows: {row_count}")
                    except Exception as e:
                        log_msg("SCHEDULER-ERROR", f"Poller failed: {endpoint or name} for {sym}: {e}")
                    finally:
                        last_run[key] = time.time()
                        
        for _ in range(2):
            if shutdown_event.is_set():
                break
            time.sleep(0.5)

def generate_hud():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=8),
        Layout(name="market_status", size=9),
        Layout(name="body")
    )
    layout["header"].split_row(
        Layout(name="us_proxy", ratio=2),
        Layout(name="nq_calibrated", ratio=2),
        Layout(name="global_dgx", ratio=1)
    )
    
    us_table = Table(title="[US PROXY (FLASH ALPHA)]", expand=True)
    us_table.add_column("Symbol", style="cyan")
    us_table.add_column("Gamma Regime", style="magenta")
    us_table.add_column("Flip Point", style="green")
    us_table.add_column("Call Wall", style="red")
    us_table.add_column("Put Wall", style="blue")
    us_table.add_column("Net GEX ($B)", style="white")
    us_table.add_column("Pin Score", style="yellow")
    us_table.add_column("VIX", style="cyan")
    us_table.add_column("Net Prem ($M)", style="magenta")
    us_table.add_column("D.Regime", style="yellow")
    us_table.add_column("OI Chg C/P", style="cyan")
    
    for sym in ["SPX", "QQQ"]:
        data = us_proxy_data.get(sym, {})
        regime = data.get("regime", "UNKNOWN")
        flip = f"{data.get('flip'):.2f}" if data.get("flip") is not None else "N/A"
        c_wall = f"{data.get('call_wall'):.2f}" if data.get("call_wall") is not None else "N/A"
        p_wall = f"{data.get('put_wall'):.2f}" if data.get("put_wall") is not None else "N/A"
        netgex = f"{data.get('netgex') / 1e9:.2f}B" if data.get("netgex") is not None else "N/A"
        pin_score = str(data.get("pin_score", "N/A"))
        vix = f"{data.get('vix'):.2f}" if data.get("vix") is not None else "N/A"
        
        net_prem_val = data.get("net_premium")
        if net_prem_val is not None:
            sign = "+" if net_prem_val >= 0 else ""
            net_prem = f"{sign}{net_prem_val / 1e6:.1f}M"
        else:
            net_prem = "N/A"
            
        reg_dec = data.get("regime_decision")
        reg_score = data.get("regime_score")
        if reg_dec is not None and reg_score is not None:
            reg_strat = f"{reg_dec} ({int(reg_score)})"
        else:
            reg_strat = "N/A"
            
        c_oi_chg = data.get("total_call_oi_change")
        p_oi_chg = data.get("total_put_oi_change")
        if c_oi_chg is not None and p_oi_chg is not None:
            sign_c = "+" if c_oi_chg >= 0 else ""
            sign_p = "+" if p_oi_chg >= 0 else ""
            oi_chg = f"{sign_c}{c_oi_chg/1000:.0f}k/{sign_p}{p_oi_chg/1000:.0f}k"
        else:
            oi_chg = "N/A"
            
        us_table.add_row(sym, regime, flip, c_wall, p_wall, netgex, pin_score, vix, net_prem, reg_strat, oi_chg)
        
    layout["header"]["us_proxy"].update(Panel(us_table))
    
    nq_table = Table(title="[NQ CALIBRATED LEVELS & REGIME]", expand=True)
    nq_table.add_column("Level / Metric", style="cyan")
    nq_table.add_column("Value", style="green")
    
    dec = nq_calibrated_data.get("decision")
    score = nq_calibrated_data.get("score")
    conf = nq_calibrated_data.get("confidence")
    if dec is not None and score is not None:
        reg_strat_val = f"{dec} ({int(score)} / {conf:.2f})"
    else:
        reg_strat_val = "N/A"
        
    nq_table.add_row("Dealer Regime Strategy", reg_strat_val)
    
    flip = nq_calibrated_data.get("flip")
    flip_str = f"{flip:.2f}" if flip is not None else "N/A"
    nq_table.add_row("Gamma Flip Point", flip_str)
    
    cw = nq_calibrated_data.get("call_wall")
    cw_str = f"{cw:.2f}" if cw is not None else "N/A"
    nq_table.add_row("Call Wall (Gamma Resistance)", cw_str)
    
    pw = nq_calibrated_data.get("put_wall")
    pw_str = f"{pw:.2f}" if pw is not None else "N/A"
    nq_table.add_row("Put Wall (Gamma Support)", pw_str)
    
    vw = nq_calibrated_data.get("vanna_wall")
    vw_str = f"{vw:.2f}" if vw is not None else "N/A"
    nq_table.add_row("Vanna Wall", vw_str)
    
    layout["header"]["nq_calibrated"].update(Panel(nq_table))
    
    glob_table = Table(title="[GLOBAL SYSTEM BIAS]", expand=True)
    glob_table.add_column("Metric", style="cyan")
    glob_table.add_column("Value", style="green")
    
    glob_table.add_row("Sovereign Bias", global_dgx_data.get("bias", "HOLD"))
    glob_table.add_row("Active Trades", str(global_dgx_data.get("trades", 0)))
    
    nq_price = pricing_provider.get_latest_price("NQ")
    es_price = pricing_provider.get_latest_price("ES")
    glob_table.add_row("NQ Spot", f"{nq_price:.2f}" if nq_price else "N/A")
    glob_table.add_row("ES Spot", f"{es_price:.2f}" if es_price else "N/A")
    
    layout["header"]["global_dgx"].update(Panel(glob_table))
    
    mkt_table = Table(title="[LIVE ASSETS & INDICATORS]", expand=True)
    mkt_table.add_column("Symbol", style="cyan")
    mkt_table.add_column("Last Price", style="green")
    mkt_table.add_column("10s W%R", style="yellow")
    mkt_table.add_column("1m RSI", style="magenta")
    mkt_table.add_column("5m RSI", style="magenta")
    mkt_table.add_column("15m RSI", style="magenta")
    
    for sym in ["NQ", "ES", "DAX", "NKD"]:
        ind = global_indicators.get(sym, {})
        price_val = ind.get("price")
        price_str = f"{price_val:.2f}" if price_val is not None else "N/A"
        
        wpr_val = ind.get("10s_wpr")
        wpr_str = f"{wpr_val:.2f}" if wpr_val is not None else "N/A"
        
        def format_rsi(rsi_val, rsi_dir):
            if rsi_val is None:
                return "N/A"
            arr = ""
            if rsi_dir == "RISING":
                arr = " ▲"
            elif rsi_dir == "FALLING":
                arr = " ▼"
            return f"{rsi_val:.2f}{arr}"
            
        rsi1 = format_rsi(ind.get("1m_rsi"), ind.get("1m_rsi_dir"))
        rsi5 = format_rsi(ind.get("5m_rsi"), ind.get("5m_rsi_dir"))
        rsi15 = format_rsi(ind.get("15m_rsi"), ind.get("15m_rsi_dir"))
        
        mkt_table.add_row(sym, price_str, wpr_str, rsi1, rsi5, rsi15)
        
    layout["market_status"].update(Panel(mkt_table))
    
    logs_text = Text("\n".join(list(log_feed)))
    layout["body"].update(Panel(logs_text, title="[Execution & Alert Feed]"))
    
    return layout

def call_gemini_analyst(state_payload: dict) -> str:
    gemini_key = os.environ.get("GEMINI_API_KEY")
    prompt = (
        "You are a quantitative desk analyst. Provide a 2-sentence max regime synthesis "
        "based ONLY on the provided JSON state. Highlight converging magnets or regime flips.\n\n"
        f"JSON State:\n{json.dumps(state_payload, indent=2)}"
    )
    if not gemini_key:
        return "Gemini API key not found in environment."
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
        res = requests.post(
            f"{url}?key={gemini_key}",
            json={"contents": [{"parts":[{"text": prompt}]}]},
            timeout=20
        )
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        else:
            return f"Gemini status {res.status_code}: {res.text}"
    except Exception as e:
        import traceback
        return f"Gemini exception: {str(e)} - {traceback.format_exc()}"

def call_qwen_analyst(state_payload: dict) -> str:
    prompt = (
        "You are a quantitative desk analyst. Provide a 2-sentence max regime synthesis "
        "based ONLY on the provided JSON state. Highlight converging magnets or regime flips. "
        "Take into account spatial structures such as Wyckoff sweeps, breakout retests, failures to break, and micro-reversals if present in the state.\n\n"
        f"JSON State:\n{json.dumps(state_payload, indent=2)}"
    )
    try:
        url = "http://localhost:8000/v1/chat/completions"
        payload = {
            "model": "qwen3-coder-30b-a3b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 100
        }
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"].strip()
        else:
            return f"Qwen3 status {res.status_code}: {res.text}"
    except Exception as e:
        import traceback
        return f"Qwen3 exception: {str(e)} - {traceback.format_exc()}"

async def refresh_header_data_loop():
    global us_proxy_data, global_dgx_data, nq_calibrated_data
    while not shutdown_event.is_set():
        try:
            spx_snap = await fa_state_reader.fetch_latest_row("fa_snapshots", "SPX")
            qqq_snap = await fa_state_reader.fetch_latest_row("fa_snapshots", "QQQ")
            nq_snap = await fa_state_reader.fetch_latest_row("fa_snapshots", "NQ")
            spx_zdte = await fa_state_reader.fetch_latest_row("fa_zero_dte", "SPX")
            qqq_zdte = await fa_state_reader.fetch_latest_row("fa_zero_dte", "QQQ")
            
            us_proxy_data = {
                "SPX": {
                    "regime": spx_snap.get("regime") if spx_snap else "UNKNOWN",
                    "flip": spx_snap.get("gamma_flip") if spx_snap else None,
                    "pin_score": spx_zdte.get("pin_score") if spx_zdte else "N/A",
                    "call_wall": spx_snap.get("call_wall") if spx_snap else None,
                    "put_wall": spx_snap.get("put_wall") if spx_snap else None,
                    "netgex": spx_snap.get("netgex") if spx_snap else None,
                    "vix": spx_snap.get("vix") if spx_snap else None,
                    "net_premium": spx_snap.get("net_dealer_premium") if spx_snap else None,
                    "regime_decision": spx_snap.get("dealer_regime_decision") if spx_snap else None,
                    "regime_score": spx_snap.get("dealer_regime_score") if spx_snap else None,
                    "total_call_oi_change": spx_snap.get("total_call_oi_change") if spx_snap else None,
                    "total_put_oi_change": spx_snap.get("total_put_oi_change") if spx_snap else None,
                },
                "QQQ": {
                    "regime": qqq_snap.get("regime") if qqq_snap else "UNKNOWN",
                    "flip": qqq_snap.get("gamma_flip") if qqq_snap else None,
                    "pin_score": qqq_zdte.get("pin_score") if qqq_zdte else "N/A",
                    "call_wall": qqq_snap.get("call_wall") if qqq_snap else None,
                    "put_wall": qqq_snap.get("put_wall") if qqq_snap else None,
                    "netgex": qqq_snap.get("netgex") if qqq_snap else None,
                    "vix": qqq_snap.get("vix") if qqq_snap else None,
                    "net_premium": qqq_snap.get("net_dealer_premium") if qqq_snap else None,
                    "regime_decision": qqq_snap.get("dealer_regime_decision") if qqq_snap else None,
                    "regime_score": qqq_snap.get("dealer_regime_score") if qqq_snap else None,
                    "total_call_oi_change": qqq_snap.get("total_call_oi_change") if qqq_snap else None,
                    "total_put_oi_change": qqq_snap.get("total_put_oi_change") if qqq_snap else None,
                }
            }
            
            nq_calibrated_data = {
                "decision": qqq_snap.get("dealer_regime_decision") if qqq_snap else None,
                "score": qqq_snap.get("dealer_regime_score") if qqq_snap else None,
                "confidence": qqq_snap.get("dealer_regime_confidence") if qqq_snap else None,
                "flip": nq_snap.get("gamma_flip") if nq_snap else None,
                "call_wall": nq_snap.get("call_wall") if nq_snap else None,
                "put_wall": nq_snap.get("put_wall") if nq_snap else None,
                "vanna_wall": nq_snap.get("vanna_wall") if nq_snap else None
            }
            
            bias, trade_count = get_current_system_bias()
            global_dgx_data = {
                "bias": bias,
                "trades": trade_count
            }
            
            if interactive_hud and hud_layout:
                new_layout = generate_hud()
                hud_layout["header"]["us_proxy"].update(new_layout["header"]["us_proxy"])
                hud_layout["header"]["nq_calibrated"].update(new_layout["header"]["nq_calibrated"])
                hud_layout["header"]["global_dgx"].update(new_layout["header"]["global_dgx"])
                hud_layout["market_status"].update(new_layout["market_status"])
                
        except Exception:
            pass
            
        for _ in range(2):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)

async def cron_desk_notes_loop():
    loop_start_time = time.time()
    await asyncio.sleep(5)
    while not shutdown_event.is_set():
        try:
            symbols = ["SPX", "QQQ", "DAX", "NKD"]
            payload = {}
            for sym in symbols:
                snap = await fa_state_reader.fetch_latest_row("fa_snapshots", sym)
                zdte = await fa_state_reader.fetch_latest_row("fa_zero_dte", sym)
                pin = await fa_state_reader.fetch_latest_row("fa_pin_risk", sym)
                filtered_snap = {}
                if snap:
                    snap_keys = [
                        "spot", "regime", "netgex", "net_dex", "net_vex", "net_chex",
                        "gamma_flip", "call_wall", "put_wall", "vanna_wall",
                        "net_dealer_premium", "dealer_regime_decision",
                        "dealer_regime_score", "dealer_regime_confidence",
                        "total_call_oi_change", "total_put_oi_change", "zero_dte_magnet"
                    ]
                    filtered_snap = {k: snap[k] for k in snap_keys if k in snap}
                filtered_zdte = {}
                if zdte:
                    zdte_keys = ["pin_score"]
                    filtered_zdte = {k: zdte[k] for k in zdte_keys if k in zdte}
                filtered_pin = {}
                if pin:
                    pin_keys = ["live_pin_risk", "magnet_strike", "oi_score", "gamma_score"]
                    filtered_pin = {k: pin[k] for k in pin_keys if k in pin}
                payload[sym] = {
                    "snapshot": filtered_snap,
                    "zero_dte": filtered_zdte,
                    "pin_risk": filtered_pin
                }
            gemini_response = await asyncio.to_thread(call_gemini_analyst, payload)
            qwen_response = await asyncio.to_thread(call_qwen_analyst, payload)
            log_msg("DESK NOTE", gemini_response)
            log_msg("SHADOW NOTE", qwen_response)
            record = {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "gemini_response": gemini_response,
                "qwen3_response": qwen_response,
                "state_payload": payload
            }
            line = json.dumps(record) + "\n"
            for path in [
                "/home/longfort/.gemini/antigravity/scratch/qwen3_judgements.jsonl",
                "/home/longfort/.gemini/antigravity/py_engine/qwen3_judgements.jsonl"
            ]:
                try:
                    with open(path, "a") as f:
                        f.write(line)
                except Exception:
                    pass
        except Exception as e:
            log_msg("DESK-NOTE-ERROR", f"Failed to generate desk note: {e}")
        elapsed = time.time() - loop_start_time
        interval = 300 if elapsed < 3600 else 1800
        for _ in range(interval):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)

async def cron_pin_gate_loop():
    from pin_gate_scheduler import PinGateScheduler
    scheduler = PinGateScheduler()
    await asyncio.sleep(5)
    while not shutdown_event.is_set():
        try:
            decisions = await asyncio.to_thread(scheduler.tick)
            for d in decisions:
                log_msg("PINGATE", f"{d.symbol}: allow={d.allow} reason={d.reason}")
        except Exception as e:
            log_msg("PINGATE-ERROR", f"Failed in pin gate tick: {e}")
            
        for _ in range(60):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)

async def shutdown_event_wait():
    while not shutdown_event.is_set():
        await asyncio.sleep(0.5)

def run_asyncio_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(refresh_header_data_loop())
    loop.create_task(cron_desk_notes_loop())
    loop.create_task(cron_pin_gate_loop())
    loop.run_until_complete(shutdown_event_wait())

def periodic_geometry_and_calibration_loop():
    """Interval task: updates spatial geometry and option walls every 15 seconds."""
    geom_script = os.path.join(ENGINE_DIR, "spatial_geometry.py")
    calib_script = os.path.join(ENGINE_DIR, "calibrate_nq.py")
    
    while not shutdown_event.is_set():
        try:
            subprocess.run([PYTHON_BIN, geom_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
            subprocess.run([PYTHON_BIN, calib_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
        except Exception as e:
            log_msg("GEOMETRY-ERROR", f"Geometry/Calibration loop failed: {e}")
            
        for _ in range(15):
            if shutdown_event.is_set():
                return
            time.sleep(1)

def get_current_system_bias():
    """Queries the SQLite database to get the active bias and position count."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT current_bias FROM system_lock WHERE lock_id = 1")
        bias_row = cursor.fetchone()
        
        cursor.execute("SELECT COUNT(*) FROM active_trades")
        trade_count = cursor.fetchone()[0]
        
        conn.close()
        bias = bias_row[0] if bias_row else "NEUTRAL"
        return bias, trade_count
    except Exception:
        return "UNKNOWN", 0

def periodic_decision_brain_loop():
    """Interval task: executes the LLM brain decisions every 10 seconds."""
    script_path = os.path.join(ENGINE_DIR, "arbiter_brain.py")
    time.sleep(5)
    
    while not shutdown_event.is_set():
        try:
            res = subprocess.run([PYTHON_BIN, script_path], capture_output=True, text=True, timeout=35)
            if res.returncode == 0:
                bias, positions = get_current_system_bias()
                log_msg("BRAIN", f"Arbiter run complete. Bias: {bias} | Active Positions: {positions}")
            else:
                log_msg("BRAIN-ERROR", f"Arbiter failed (code {res.returncode}): {res.stderr.strip()}")
        except Exception as e:
            log_msg("BRAIN-ERROR", f"Exception in arbiter brain loop: {e}")
            
        for _ in range(10):
            if shutdown_event.is_set():
                return
            time.sleep(1)

def periodic_db_cleanup_loop():
    """Runs database maintenance (deletes ticks > 24 hours, VACUUM) once a day."""
    log_msg("CLEANUP", "DB cleanup scheduler initialized (24-hour interval).")
    time.sleep(28800)
    
    while not shutdown_event.is_set():
        log_msg("CLEANUP", "Executing database maintenance...")
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            cursor = conn.cursor()
            
            cursor.execute("DELETE FROM ig_ticks WHERE timestamp < datetime('now', '-24 hours')")
            deleted_ticks = cursor.rowcount
            
            cursor.execute("DELETE FROM l2_orderbook WHERE timestamp < datetime('now', '-24 hours')")
            deleted_l2 = cursor.rowcount
            
            conn.commit()
            conn.execute("VACUUM")
            conn.close()
            
            log_msg("CLEANUP", f"Database maintenance complete. Deleted {deleted_ticks} ticks and {deleted_l2} L2 quotes. Vacuumed successfully.")
        except Exception as e:
            log_msg("CLEANUP-ERROR", f"Database maintenance failed: {e}")
            
        for _ in range(86400):
            if shutdown_event.is_set():
                return
            time.sleep(1)

def periodic_health_check_loop():
    """Calculates and writes health stats every 5 seconds."""
    log_path = "/home/longfort/.gemini/antigravity/py_engine/logs/health.log"
    log_msg("HEALTH", "Health supervisor initialized (5-second interval).")
    
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if not os.path.exists(log_path):
        try:
            with open(log_path, "w") as f:
                f.write("timestamp,ig_age_sec,rithmic_age_sec,in_freeze_state,freeze_count_today,last_basis,questdb_reachable,sqlite_reachable\n")
        except Exception:
            pass

    while not shutdown_event.is_set():
        try:
            sqlite_reachable = False
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                conn.close()
                sqlite_reachable = True
            except Exception:
                pass
                
            questdb_reachable = False
            try:
                r = requests.get("http://localhost:9000/exp", params={'query': 'select 1'}, timeout=1.0)
                if r.status_code == 200:
                    questdb_reachable = True
            except Exception:
                pass
                
            ig_age_sec = -1.0
            try:
                ig_path = "/dev/shm/yu_ig_price_nq"
                if os.path.exists(ig_path):
                    with open(ig_path, "r") as f:
                        ig_data = json.load(f)
                        ts_str = ig_data["timestamp"]
                        if '.' in ts_str:
                            parts = ts_str.split('.')
                            if len(parts) == 2:
                                frac = parts[1].replace('Z', '')
                                if len(frac) > 6:
                                    ts_str = parts[0] + '.' + frac[:6] + 'Z'
                        ig_ts = datetime.datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        ig_age_sec = (now_utc - ig_ts).total_seconds()
            except Exception:
                pass
                
            rithmic_age_sec = -1.0
            try:
                r_path = "/dev/shm/yu_rithmic_price_nq"
                if os.path.exists(r_path):
                    with open(r_path, "r") as f:
                        r_data = json.load(f)
                        ts_str = r_data["timestamp"]
                        if '.' in ts_str:
                            parts = ts_str.split('.')
                            if len(parts) == 2:
                                frac = parts[1].replace('Z', '')
                                if len(frac) > 6:
                                    ts_str = parts[0] + '.' + frac[:6] + 'Z'
                        r_ts = datetime.datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        rithmic_age_sec = (now_utc - r_ts).total_seconds()
            except Exception:
                pass
                
            in_freeze_state = False
            freeze_count_today = 0
            try:
                state_path = "/dev/shm/yu_state_nq"
                if os.path.exists(state_path):
                    with open(state_path, "r") as f:
                        state_data = json.load(f)
                        in_freeze_state = bool(state_data.get("in_freeze_state", False))
                        freeze_count_today = int(state_data.get("freeze_count_today", 0))
            except Exception:
                pass
                
            last_basis = -9999.0
            try:
                basis_path = "/home/longfort/.gemini/antigravity/py_engine/logs/basis.log"
                if os.path.exists(basis_path):
                    with open(basis_path, "rb") as f:
                        f.seek(-min(os.path.getsize(basis_path), 1024), 2)
                        lines = f.readlines()
                        if lines:
                            last_line = lines[-1].decode('utf-8')
                            if "basis=" in last_line:
                                last_basis = float(last_line.split("basis=")[1].split()[0])
            except Exception:
                pass
                
            ts_now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            row = f"{ts_now},{ig_age_sec:.2f},{rithmic_age_sec:.2f},{in_freeze_state},{freeze_count_today},{last_basis:.4f},{questdb_reachable},{sqlite_reachable}\n"
            with open(log_path, "a") as f:
                f.write(row)
                
        except Exception as e:
            log_msg("HEALTH-ERROR", f"Health check loop failed: {e}")
            
        for _ in range(5):
            if shutdown_event.is_set():
                return
            time.sleep(1)

def main():
    print("=========================================================", flush=True)
    print("                 YU - TRADING BOT DAEMON                 ", flush=True)
    print("=========================================================", flush=True)
    log_msg("SYSTEM", "Initializing YU orchestrator...")
    
    log_msg("STARTUP", "Cleaning up stale RAM cache/state files...")
    for symbol in ["nq", "es", "dax", "nkd", "ftse"]:
        for prefix in ["yu_state", "yu_ig_price", "yu_rithmic_price"]:
            shm_path = f"/dev/shm/{prefix}_{symbol}"
            if os.path.exists(shm_path):
                try:
                    os.remove(shm_path)
                except Exception:
                    pass
                    
    run_pivots_startup_check()
    
    log_historical_coverage()
    
    telemetry_thread = threading.Thread(target=monitor_telemetry_stream, name="TelemetryThread", daemon=True)
    telemetry_thread.start()
    
    rithmic_thread = threading.Thread(target=monitor_rithmic_stream, name="RithmicThread", daemon=True)
    rithmic_thread.start()
    
    indicator_thread = threading.Thread(target=indicator_daemon_loop, name="IndicatorDaemonThread", daemon=True)
    indicator_thread.start()
    
    options_thread = threading.Thread(target=fa_scheduler_loop, name="OptionsThread", daemon=True)
    options_thread.start()
    
    geometry_thread = threading.Thread(target=periodic_geometry_and_calibration_loop, name="GeometryThread", daemon=True)
    geometry_thread.start()
    
    brain_thread = threading.Thread(target=periodic_decision_brain_loop, name="BrainThread", daemon=True)
    brain_thread.start()
    
    cleanup_thread = threading.Thread(target=periodic_db_cleanup_loop, name="CleanupThread", daemon=True)
    cleanup_thread.start()
    
    health_thread = threading.Thread(target=periodic_health_check_loop, name="HealthThread", daemon=True)
    health_thread.start()
    
    asyncio_thread = threading.Thread(target=run_asyncio_loop, name="AsyncioThread", daemon=True)
    asyncio_thread.start()
    
    global interactive_hud, live_hud, hud_layout
    interactive_hud = sys.stdout.isatty()
    if interactive_hud:
        hud_layout = generate_hud()
        live_hud = Live(hud_layout, auto_refresh=True, refresh_per_second=4)
        live_hud.start()
    
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    finally:
        if live_hud:
            try:
                live_hud.stop()
            except Exception:
                pass

if __name__ == "__main__":
    main()
