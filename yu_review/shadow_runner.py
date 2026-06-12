import os
import sys
import time
import json
import logging
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
sys.path.append("/home/longfort/.gemini/antigravity/py_engine")
from regime_selector import RegimeSelector
from yu_core import compute_walls, should_enter, compute_rsi, compute_wpr, SwingWalls, EntryDecision
from swing_pivots import SwingPivotTracker
import pricing_provider
from spatial_detectors import (
    detect_liquidity_sweep,
    detect_wick_rejection,
    detect_failure_to_break,
    detect_breakout_retest
)


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("shadow_runner")

DB_PATH = "/home/longfort/.gemini/antigravity/py_engine/hud_state.db"
SYMBOL_TO_EPIC = {
    "NQ": "IX.D.NASDAQ.CASH.IP",
    "DAX": "IX.D.DAX.DAILY.IP",
    "NKD": "IX.D.NIKKEI.DAILY.IP"
}
DATABENTO_SYMBOLS = {
    "NQ": "NQ.FUT",
    "NKD": "NKD.FUT"
}

def load_env():
    for env_path in [
        "/home/longfort/.config/longfort/jack.secrets.env",
        "/home/longfort/.config/longfort-secrets/telegram.env",
        "/home/longfort/.gemini/antigravity/scratch/.env"
    ]:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("export "):
                            line = line[7:].strip()
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            k = parts[0].strip()
                            v = parts[1].strip().strip('"').strip("'")
                            if k not in os.environ:
                                os.environ[k] = v
            except Exception as e:
                logger.error(f"Failed to load env from {env_path}: {e}")

load_env()

def query_db(sql: str) -> list[dict]:
    url = f"http://localhost:9000/exec?query={urllib.parse.quote(sql)}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            cols = [c['name'] for c in data.get('columns', [])]
            dataset = data.get('dataset', [])
            return [dict(zip(cols, row)) for row in dataset]
    except Exception as e:
        logger.error(f"QuestDB query failed: {e}")
    return []

def execute_db(sql: str) -> bool:
    url = f"http://localhost:9000/exec?query={urllib.parse.quote(sql)}"
    try:
        resp = requests.get(url, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"QuestDB execute failed: {e}")
    return False

def send_notification(message: str):
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id:
        try:
            url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            payload = {"chat_id": tg_chat_id, "text": message}
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code >= 300:
                logger.error(f"Telegram notification failed (status={resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"Telegram exception: {e}")

    slack_url = os.environ.get("SLACK_WEBHOOK") or os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            payload = {"text": message}
            resp = requests.post(slack_url, json=payload, timeout=8)
            if resp.status_code >= 300:
                logger.error(f"Slack notification failed (status={resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"Slack exception: {e}")

def allows_direction(regime: str, side: str) -> bool:
    if regime == "AVOID":
        return False
    if side == "LONG":
        allowed = {"MEAN_REVERT", "MEAN_REVERT_BULLISH", "RANGE", "NEUTRAL", "CHOP", "BULLISH", "TREND", "PIN"}
    else:
        allowed = {"MEAN_REVERT", "MEAN_REVERT_BEARISH", "RANGE", "NEUTRAL", "CHOP", "BEARISH", "TREND", "PIN"}
    return regime in allowed

class ClosedBar:
    def __init__(self, ts, open_val, high, low, close):
        self.ts = ts
        self.open = float(open_val)
        self.high = float(high)
        self.low = float(low)
        self.close = float(close)

class ShadowPosition:
    def __init__(self, virtual_id, symbol, side, entry_ts, entry_price, stop_price, target_price,
                 regime_at_entry, ag_response, walls_snapshot, rsi_at_entry, wpr_at_entry, room_pts,
                 exit_ts=None, exit_price=None, exit_reason=None, pnl_pts=0.0, bars_held=0, size=1.0):
        self.virtual_id = virtual_id
        self.symbol = symbol
        self.side = side
        self.entry_ts = entry_ts
        self.entry_price = float(entry_price)
        self.stop_price = float(stop_price)
        self.target_price = float(target_price)
        self.regime_at_entry = regime_at_entry
        self.ag_response = ag_response
        self.walls_snapshot = walls_snapshot
        self.rsi_at_entry = float(rsi_at_entry)
        self.wpr_at_entry = float(wpr_at_entry)
        self.room_pts = float(room_pts)
        self.exit_ts = exit_ts
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.pnl_pts = float(pnl_pts)
        self.bars_held = int(bars_held)
        self.size = float(size)

def load_open_shadow_positions() -> dict[str, list[ShadowPosition]]:
    sql = (
        "SELECT virtual_id, symbol, side, entry_ts, entry_price, stop_price, target_price, "
        "regime_at_entry, ag_response, walls_snapshot, rsi_at_entry, wpr_at_entry, room_pts, bars_held "
        "FROM shadow_positions WHERE exit_ts IS NULL"
    )
    rows = query_db(sql)
    open_positions = {}
    for r in rows:
        ts_val = r.get("entry_ts")
        if isinstance(ts_val, str):
            ts = datetime.fromisoformat(ts_val.replace('Z', '+00:00'))
        else:
            ts = datetime.now(timezone.utc)

        pos = ShadowPosition(
            virtual_id=r.get("virtual_id"),
            symbol=r.get("symbol"),
            side=r.get("side"),
            entry_ts=ts,
            entry_price=r.get("entry_price"),
            stop_price=r.get("stop_price"),
            target_price=r.get("target_price"),
            regime_at_entry=r.get("regime_at_entry"),
            ag_response=r.get("ag_response"),
            walls_snapshot=r.get("walls_snapshot", "{}"),
            rsi_at_entry=r.get("rsi_at_entry", 50.0),
            wpr_at_entry=r.get("wpr_at_entry", -50.0),
            room_pts=r.get("room_pts", 0.0),
            bars_held=r.get("bars_held", 0)
        )
        open_positions.setdefault(pos.symbol, []).append(pos)
    return open_positions

def open_shadow_position(pos: ShadowPosition):
    now_str = pos.entry_ts.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    walls_escaped = pos.walls_snapshot.replace("'", "''")
    ag_resp_escaped = pos.ag_response.replace("'", "''")
    sql = (
        f"INSERT INTO shadow_positions (virtual_id, symbol, side, entry_ts, entry_price, stop_price, target_price, "
        f"regime_at_entry, ag_response, walls_snapshot, rsi_at_entry, wpr_at_entry, room_pts, bars_held) "
        f"VALUES ('{pos.virtual_id}', '{pos.symbol}', '{pos.side}', '{now_str}', {pos.entry_price}, {pos.stop_price}, "
        f"{pos.target_price}, '{pos.regime_at_entry}', '{ag_resp_escaped}', '{walls_escaped}', {pos.rsi_at_entry}, "
        f"{pos.wpr_at_entry}, {pos.room_pts}, {pos.bars_held})"
    )
    execute_db(sql)

def close_shadow_position(pos: ShadowPosition):
    exit_ts_str = pos.exit_ts.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    sql = (
        f"UPDATE shadow_positions SET exit_ts='{exit_ts_str}', exit_price={pos.exit_price}, "
        f"exit_reason='{pos.exit_reason}', pnl_pts={pos.pnl_pts}, bars_held={pos.bars_held} "
        f"WHERE virtual_id='{pos.virtual_id}'"
    )
    execute_db(sql)

def log_event(symbol: str, event_type: str, detail: str):
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    detail_escaped = detail.replace("'", "''")
    sql = (
        f"INSERT INTO shadow_events (ts, symbol, event_type, detail) "
        f"VALUES ('{now_str}', '{symbol}', '{event_type}', '{detail_escaped}')"
    )
    execute_db(sql)

def publish_live_opportunity(pos: ShadowPosition, walls):
    try:
        now_utc = datetime.now(timezone.utc)
        now_str = now_utc.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        expected_risk_pts = abs(pos.entry_price - pos.stop_price)
        expected_reward_pts = abs(pos.target_price - pos.entry_price)
        rr = expected_reward_pts / expected_risk_pts if expected_risk_pts > 0 else 1.5

        payload = {
            "virtual_id": pos.virtual_id,
            "direction": pos.side.lower(),
            "strength": 1.0,
            "pattern_modifier": 1.0,
            "rr": rr,
            "rr_ratio": rr,
            "instrument_open": True,
            "warmup_active": False,
            "blackout_reasons": "",
            "session": pos.regime_at_entry,
            "wall_id": f"{pos.symbol}_wall_{int(pos.entry_price)}",
            "wall_price": pos.entry_price,
            "expected_risk_pts": expected_risk_pts,
            "expected_reward_pts": expected_reward_pts,
            "trigger_pivot_level": walls.pivot,
            "anchor_wall_near": walls.session_low if pos.side == "LONG" else walls.session_high,
            "anchor_wall_far": walls.session_high if pos.side == "LONG" else walls.session_low,
            "size": getattr(pos, "size", 1.0),
        }
        payload_str = json.dumps(payload).replace("'", "''")

        tags_val = 'shadow_live_sniper' if pos.regime_at_entry == 'SNIPER' else 'shadow_live'
        sql = (
            f"INSERT INTO chart_events (ts, symbol, event_type, level, payload, session, "
            f"dc_floor_mult, blackout_reasons, instrument_open, warmup_active, pattern_modifier, pattern_tags) "
            f"VALUES ('{now_str}', '{pos.symbol}', 'opportunity', {pos.entry_price}, '{payload_str}', "
            f"'{pos.regime_at_entry}', 1.0, '', true, false, 1.0, '{tags_val}')"
        )
        logger.info(f"Publishing live execution opportunity to QuestDB: {pos.symbol} {pos.side} at {pos.entry_price}")
        if execute_db(sql):
            logger.info("Successfully published opportunity to QuestDB.")
        else:
            logger.error("Failed to publish opportunity to QuestDB (execute_db returned False).")
    except Exception as e:
        logger.error(f"Error publishing opportunity to QuestDB: {e}", exc_info=True)

class IGClient:
    def __init__(self, api_key, username, password, base_url="https://api.ig.com"):
        self.api_key = api_key
        self.username = username
        self.password = password
        self.base_url = base_url
        self.cst = None
        self.x_sec = None

    def login(self):
        url = f"{self.base_url}/gateway/deal/session"
        headers = {
            "X-IG-API-KEY": self.api_key,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "VERSION": "2"
        }
        payload = {"identifier": self.username, "password": self.password}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                self.cst = resp.headers.get("CST")
                self.x_sec = resp.headers.get("X-SECURITY-TOKEN")
                logger.info("IG REST login successful.")
                return True
            else:
                logger.error(f"IG REST login failed (status={resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"IG REST login exception: {e}")
        return False

    def get_prices(self, epic, resolution="MINUTE", max_bars=60):
        if not self.cst or not self.x_sec:
            if not self.login():
                return None

        url = f"{self.base_url}/gateway/deal/prices/{epic}?resolution={resolution}&max={max_bars}"
        headers = {
            "X-IG-API-KEY": self.api_key,
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.x_sec,
            "Accept": "application/json",
            "VERSION": "3"
        }
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("prices", [])
            elif resp.status_code == 401:
                logger.info("IG REST token expired, re-logging in...")
                if self.login():
                    headers["CST"] = self.cst
                    headers["X-SECURITY-TOKEN"] = self.x_sec
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        return resp.json().get("prices", [])
            logger.error(f"IG REST get_prices failed (status={resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"IG REST get_prices exception: {e}")
        return None

latest_databento_bars = {}
databento_last_updated = {}
instrument_id_to_symbol = {}
databento_healthy = {"NQ": False, "NKD": False}

def get_front_month_symbol(symbol: str, dt: datetime) -> str:
    year_digit = str(dt.year % 10)
    month = dt.month
    day = dt.day

    if month < 3 or (month == 3 and day < 12):
        month_char = "H"
    elif month < 6 or (month == 6 and day < 12):
        month_char = "M"
    elif month < 9 or (month == 9 and day < 12):
        month_char = "U"
    elif month < 12 or (month == 12 and day < 11):
        month_char = "Z"
    else:
        month_char = "H"
        year_digit = str((dt.year + 1) % 10)

    return f"{symbol}{month_char}{year_digit}"

def on_databento_record(record):
    try:
        if hasattr(record, "stype_out_symbol"):
            sym_out = getattr(record, "stype_out_symbol", None)
            if sym_out:
                instrument_id_to_symbol[record.instrument_id] = sym_out
        elif record.rtype == 22:
            sym_out = getattr(record, "stype_out_symbol", None)
            if sym_out:
                instrument_id_to_symbol[record.instrument_id] = sym_out

        elif record.rtype == 17 or hasattr(record, "pretty_close"):
            sym_raw = instrument_id_to_symbol.get(record.instrument_id)
            if not sym_raw:
                return

            now_utc = datetime.now(timezone.utc)
            expected_nq = get_front_month_symbol("NQ", now_utc)
            expected_nkd = get_front_month_symbol("NKD", now_utc)

            symbol = None
            if sym_raw == expected_nq:
                symbol = "NQ"
            elif sym_raw == expected_nkd:
                symbol = "NKD"

            if symbol:
                ts = datetime.fromtimestamp(record.ts_event / 1e9, tz=timezone.utc)
                bar = ClosedBar(
                    ts=ts,
                    open_val=record.pretty_open,
                    high=record.pretty_high,
                    low=record.pretty_low,
                    close=record.pretty_close
                )
                latest_databento_bars[symbol] = bar
                databento_last_updated[symbol] = datetime.now(timezone.utc)
                databento_healthy[symbol] = True
    except Exception as e:
        logger.error(f"Error in Databento callback: {e}")

def run_databento_listener(api_key):
    import databento as db
    logger.info("Initializing Databento Live Client...")
    client = db.Live(key=api_key)
    try:
        client.subscribe(
            dataset="GLBX.MDP3",
            schema="ohlcv-1m",
            symbols=["NQ.FUT", "NKD.FUT"],
            stype_in="parent"
        )
        client.add_callback(on_databento_record)
        client.start()
        logger.info("Databento Live stream started.")
        return client
    except Exception as e:
        logger.error(f"Failed to start Databento Live stream: {e}")
        return None

def check_exit(pos: ShadowPosition, bar) -> str | None:
    if pos.side == "LONG":
        if bar.low <= pos.stop_price:
            return "STOP"
        elif bar.high >= pos.target_price:
            return "TARGET"
    elif pos.side == "SHORT":
        if bar.high >= pos.stop_price:
            return "STOP"
        elif bar.low <= pos.target_price:
            return "TARGET"
    return None

def format_open(pos: ShadowPosition, walls) -> str:
    side_emoji = "🟢" if pos.side == "LONG" else "🔴"
    price_diff_stop = abs(pos.entry_price - pos.stop_price)
    price_diff_tgt = abs(pos.target_price - pos.entry_price)
    ts_str = pos.entry_ts.strftime('%H:%M:%S')
    return (
        f"{side_emoji} SHADOW {pos.symbol} {pos.side}\n"
        f"  Entry: {pos.entry_price:,.0f} @ {ts_str} UTC\n"
        f"  Stop:  {pos.stop_price:,.0f} (-{price_diff_stop:.0f})\n"
        f"  Tgt:   {pos.target_price:,.0f} (+{price_diff_tgt:.0f})\n"
        f"  Regime: {pos.regime_at_entry} ({pos.ag_response})\n"
        f"  Walls: H {walls.session_high:,.0f} / L {walls.session_low:,.0f} / Pivot {walls.pivot:,.0f}\n"
        f"  RSI: {pos.rsi_at_entry:.0f}  W%R: {pos.wpr_at_entry:.0f}  Room: {pos.room_pts:.0f}pts"
    )

def format_close(pos: ShadowPosition) -> str:
    win_emoji = "🔴"
    outcome = pos.exit_reason
    pnl_sign = "+" if pos.pnl_pts >= 0 else ""
    return (
        f"{win_emoji} SHADOW {pos.symbol} CLOSE — {outcome}\n"
        f"  Entry: {pos.entry_price:,.0f} → Exit: {pos.exit_price:,.0f}\n"
        f"  PnL: {pnl_sign}{pos.pnl_pts:,.1f} pts\n"
        f"  Held: {pos.bars_held} bars ({pos.bars_held} min)"
    )

def is_sniper_active_now() -> bool:
    try:
        path = "/home/longfort/.gemini/antigravity/scratch/sniper_activation.json"
        if not os.path.exists(path):
            return False
        with open(path, "r") as f:
            data = json.load(f)
        if not data.get("active"):
            return False
        expires_str = data.get("expires_at")
        if not expires_str:
            return False
        expires = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < expires
    except Exception:
        return False

def run_sniper_logic(symbol: str, price: float, bars_60: list, open_positions: dict):
    pos_list = open_positions.setdefault(symbol, [])
    if pos_list:
        pos = pos_list[0]
        pos.bars_held += 1
        exit_reason = None
        if pos.side == "LONG":
            if price <= pos.stop_price:
                exit_reason = "STOP"
            elif price >= pos.target_price:
                exit_reason = "TARGET"
        elif pos.side == "SHORT":
            if price >= pos.stop_price:
                exit_reason = "STOP"
            elif price <= pos.target_price:
                exit_reason = "TARGET"
        if not exit_reason:
            sql_wpr = f"SELECT wpr FROM indicator_state WHERE symbol='{symbol}' AND timeframe='10s' ORDER BY ts DESC LIMIT 1"
            res_wpr = query_db(sql_wpr)
            wpr_val = float(res_wpr[0]['wpr']) if res_wpr else -50.0
            if pos.side == "LONG" and wpr_val >= -21.0:
                exit_reason = "SNIPER_WPR_EXIT"
            elif pos.side == "SHORT" and wpr_val <= -82.0:
                exit_reason = "SNIPER_WPR_EXIT"
        if exit_reason:
            pos.exit_ts = datetime.now(timezone.utc)
            pos.exit_price = price
            pos.exit_reason = exit_reason
            pos.pnl_pts = (pos.exit_price - pos.entry_price) if pos.side == "LONG" else (pos.entry_price - pos.exit_price)
            close_shadow_position(pos)
            log_event(symbol, "CLOSE", f"{pos.virtual_id} {exit_reason} pnl={pos.pnl_pts:.1f}")
            send_notification(format_close(pos))
            pos_list.remove(pos)
            logger.info(f"[{symbol}] Closed virtual sniper position {pos.virtual_id}: {exit_reason} pnl={pos.pnl_pts:.1f}")

    pos_list = open_positions.setdefault(symbol, [])
    if not pos_list:
        sql_wpr = f"SELECT wpr FROM indicator_state WHERE symbol='{symbol}' AND timeframe='10s' ORDER BY ts DESC LIMIT 1"
        res_wpr = query_db(sql_wpr)
        wpr_val = float(res_wpr[0]['wpr']) if res_wpr else -50.0
        side = None
        stop_price = 0.0
        target_price = 0.0
        if wpr_val < -82.0:
            side = "LONG"
            stop_price = price - 45.0
            target_price = price + 6.0
        elif wpr_val > -21.0:
            side = "SHORT"
            stop_price = price + 45.0
            target_price = price - 6.0
        if side:
            virtual_id = f"V_{symbol}_SNIPER_{int(datetime.now(timezone.utc).timestamp())}"
            walls = compute_walls(symbol, datetime.now(timezone.utc), bars_60)
            pos = ShadowPosition(
                virtual_id=virtual_id,
                symbol=symbol,
                side=side,
                entry_ts=datetime.now(timezone.utc),
                entry_price=price,
                stop_price=stop_price,
                target_price=target_price,
                regime_at_entry="SNIPER",
                ag_response="SNIPER_ACTIVE",
                walls_snapshot=json.dumps({
                    "session_high": walls.session_high,
                    "session_low": walls.session_low,
                    "prior_high": walls.prior_high,
                    "prior_low": walls.prior_low,
                    "pivot": walls.pivot,
                    "r1": walls.r1,
                    "s1": walls.s1
                }),
                rsi_at_entry=50.0,
                wpr_at_entry=wpr_val,
                room_pts=6.0,
                bars_held=0
            )
            open_shadow_position(pos)
            pos_list.append(pos)
            open_positions[symbol] = pos_list
            log_event(symbol, "OPEN", f"{virtual_id} {side} entry={price} stop={stop_price} target={target_price}")
            send_notification(format_open(pos, walls))
            logger.info(f"[{symbol}] Opened virtual sniper position: {pos.virtual_id}")
            if os.environ.get("LIVE_EXECUTION", "false").lower() == "true":
                publish_live_opportunity(pos, walls)

def rsi_series(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    gain = up.ewm(alpha=1 / period, adjust=False).mean()
    loss = down.ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

def williams_r_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    highest_high = high.rolling(window=period).max()
    lowest_low = low.rolling(window=period).min()
    return -100 * (highest_high - close) / (highest_high - lowest_low + 1e-9)

def detect_swing_divergence(df: pd.DataFrame, price_type: str, ind_col: str, lookback: int = 40, check_conf_only: bool = True) -> str:
    if len(df) < lookback + 5:
        return ""
        
    prices = df['low'].values if price_type == 'low' else df['high'].values
    indicator = df[ind_col].values
    
    pivots = []
    start_idx = len(df) - 2
    end_idx = len(df) - lookback - 2
    if end_idx < 1:
        end_idx = 1
        
    for i in range(start_idx, end_idx, -1):
        if price_type == 'low':
            if prices[i] < prices[i-1] and prices[i] < prices[i+1]:
                pivots.append((i, prices[i], indicator[i]))
        else:
            if prices[i] > prices[i-1] and prices[i] > prices[i+1]:
                pivots.append((i, prices[i], indicator[i]))
                
    if len(pivots) < 2:
        return ""
        
    t2_idx, t2_price, t2_ind = pivots[0]
    t1_idx, t1_price, t1_ind = pivots[1]
    
    if abs(t2_idx - t1_idx) < 3:
        if len(pivots) >= 3:
            t1_idx, t1_price, t1_ind = pivots[2]
        else:
            return ""
            
    if check_conf_only and (t2_idx != len(df) - 2):
        return ""
        
    if price_type == 'low':
        if t2_price < t1_price and t2_ind > t1_ind:
            oversold = False
            if ind_col == 'RSI' and t1_ind <= 35:
                oversold = True
            elif ind_col == 'W%R' and t1_ind <= -80:
                oversold = True
                
            if oversold:
                mid_ind = indicator[t1_idx+1 : t2_idx]
                if len(mid_ind) > 0:
                    max_mid = np.max(mid_ind)
                    thresh = 3.0 if ind_col == 'RSI' else 10.0
                    if max_mid > t1_ind + thresh and max_mid > t2_ind + thresh:
                        return "Bullish"
    else:
        if t2_price > t1_price and t2_ind < t1_ind:
            overbought = False
            if ind_col == 'RSI' and t1_ind >= 65:
                overbought = True
            elif ind_col == 'W%R' and t1_ind >= -20:
                overbought = True
                
            if overbought:
                mid_ind = indicator[t1_idx+1 : t2_idx]
                if len(mid_ind) > 0:
                    min_mid = np.min(mid_ind)
                    thresh = 3.0 if ind_col == 'RSI' else 10.0
                    if min_mid < t1_ind - thresh and min_mid < t2_ind - thresh:
                        return "Bearish"
                        
    return ""

def fetch_recent_bars_from_db(symbol: str, tf: str):
    db_symbol = "DAX" if symbol == "DAX" else "NQ"
    if tf == "1min":
        sample_by = "1m"
        range_clause = "dateadd('h', -12, now())"
    elif tf == "5min":
        sample_by = "5m"
        range_clause = "dateadd('d', -2, now())"
    else:
        return pd.DataFrame()
        
    sql = (
        f"select ts, first(mid) as open, max(mid) as high, min(mid) as low, last(mid) as close "
        f"from ig_ticks "
        f"where symbol='{db_symbol}' and ts > {range_clause} "
        f"sample by {sample_by} align to calendar"
    )
    rows = query_db(sql)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()
    df["ts"] = pd.to_datetime(df["ts"])
    df.set_index("ts", inplace=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def check_symbol_swing_divergence(symbol: str, current_price: float) -> EntryDecision | None:
    if symbol not in ("DAX", "NQ"):
        return None

    if symbol == "DAX":
        tf5_target, tf5_stop, tf5_size = 30.0, 60.0, 1.0
        tf1_target, tf1_stop, tf1_size = 8.0, 20.0, 0.5
    else:
        tf5_target, tf5_stop, tf5_size = 40.0, 80.0, 1.0
        tf1_target, tf1_stop, tf1_size = 15.0, 30.0, 1.0

    df_5m = fetch_recent_bars_from_db(symbol, "5min")
    if not df_5m.empty and len(df_5m) >= 45:
        df_5m["RSI"] = rsi_series(df_5m["close"])
        df_5m["W%R"] = williams_r_series(df_5m["high"], df_5m["low"], df_5m["close"])
        
        if detect_swing_divergence(df_5m, 'low', 'RSI', check_conf_only=True) == "Bullish":
            return EntryDecision(
                side="LONG",
                entry=current_price,
                stop=current_price - tf5_stop,
                target=current_price + tf5_target,
                reason=f"{symbol} 5m RSI Swing Bullish divergence confirmed",
                sizing_factor=tf5_size
            )
        if detect_swing_divergence(df_5m, 'high', 'RSI', check_conf_only=True) == "Bearish":
            return EntryDecision(
                side="SHORT",
                entry=current_price,
                stop=current_price + tf5_stop,
                target=current_price - tf5_target,
                reason=f"{symbol} 5m RSI Swing Bearish divergence confirmed",
                sizing_factor=tf5_size
            )
        if detect_swing_divergence(df_5m, 'low', 'W%R', check_conf_only=True) == "Bullish":
            return EntryDecision(
                side="LONG",
                entry=current_price,
                stop=current_price - tf5_stop,
                target=current_price + tf5_target,
                reason=f"{symbol} 5m W%R Swing Bullish divergence confirmed",
                sizing_factor=tf5_size
            )
        if detect_swing_divergence(df_5m, 'high', 'W%R', check_conf_only=True) == "Bearish":
            return EntryDecision(
                side="SHORT",
                entry=current_price,
                stop=current_price + tf5_stop,
                target=current_price - tf5_target,
                reason=f"{symbol} 5m W%R Swing Bearish divergence confirmed",
                sizing_factor=tf5_size
            )

    df_1m = fetch_recent_bars_from_db(symbol, "1min")
    if not df_1m.empty and len(df_1m) >= 45:
        df_1m["RSI"] = rsi_series(df_1m["close"])
        df_1m["W%R"] = williams_r_series(df_1m["high"], df_1m["low"], df_1m["close"])
        
        if detect_swing_divergence(df_1m, 'low', 'RSI', check_conf_only=False) == "Bullish":
            return EntryDecision(
                side="LONG",
                entry=current_price,
                stop=current_price - tf1_stop,
                target=current_price + tf1_target,
                reason=f"{symbol} 1m RSI Swing Bullish divergence active",
                sizing_factor=tf1_size
            )
        if detect_swing_divergence(df_1m, 'high', 'RSI', check_conf_only=False) == "Bearish":
            return EntryDecision(
                side="SHORT",
                entry=current_price,
                stop=current_price + tf1_stop,
                target=current_price - tf1_target,
                reason=f"{symbol} 1m RSI Swing Bearish divergence active",
                sizing_factor=tf1_size
            )
        if detect_swing_divergence(df_1m, 'low', 'W%R', check_conf_only=False) == "Bullish":
            return EntryDecision(
                side="LONG",
                entry=current_price,
                stop=current_price - tf1_stop,
                target=current_price + tf1_target,
                reason=f"{symbol} 1m W%R Swing Bullish divergence active",
                sizing_factor=tf1_size
            )
        if detect_swing_divergence(df_1m, 'high', 'W%R', check_conf_only=False) == "Bearish":
            return EntryDecision(
                side="SHORT",
                entry=current_price,
                stop=current_price + tf1_stop,
                target=current_price - tf1_target,
                reason=f"{symbol} 1m W%R Swing Bearish divergence active",
                sizing_factor=tf1_size
            )
            
    return None

def main():
    logger.info("Starting Shadow Intraday Runner...")

    regime_selector = RegimeSelector(use_mock=False)

    bar_history = {}
    current_bar_ticks = {s: [] for s in ["NQ", "DAX", "NKD"]}
    last_bar_minute = {s: None for s in ["NQ", "DAX", "NKD"]}
    pivot_trackers = {s: SwingPivotTracker(symbol=s) for s in ["NQ", "DAX", "NKD"]}

    for symbol in ["NQ", "DAX", "NKD"]:
        sql = (
            f"SELECT ts, first(mid) as open, max(mid) as high, min(mid) as low, last(mid) as close "
            f"FROM ig_ticks WHERE symbol='{symbol}' "
            f"SAMPLE BY 1m FILL(prev) "
            f"ORDER BY ts DESC LIMIT 480"
        )
        rows = query_db(sql)
        if rows:
            rows.reverse()
            for r in rows:
                ts_str = r['ts']
                if not ts_str.endswith('Z') and not '+' in ts_str:
                    ts_str += 'Z'
                r['ts'] = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            bar_history[symbol] = rows
            logger.info(f"[{symbol}] Bootstrapped bar history with {len(rows)} bars from QuestDB.")
            pivot_trackers[symbol].update_bars(rows)
        else:
            bar_history[symbol] = []
            logger.warning(f"[{symbol}] No bar history found in QuestDB at startup.")

    last_processed_ts = {s: None for s in ["NQ", "DAX", "NKD"]}
    open_positions = load_open_shadow_positions()
    logger.info(f"Loaded {sum(len(v) for v in open_positions.values())} open positions.")

    while True:
        now = datetime.now(timezone.utc)
        current_min = now.replace(second=0, microsecond=0)

        for symbol in ["NQ", "DAX", "NKD"]:
            try:
                price = pricing_provider.get_latest_price(symbol)
                if price is None:
                    continue

                pivot_trackers[symbol].update_price(price)

                if last_bar_minute[symbol] is None:
                    last_bar_minute[symbol] = current_min

                bar = None

                if current_min > last_bar_minute[symbol]:
                    ticks = current_bar_ticks[symbol]
                    if not ticks:
                        ticks = [price]

                    bar = ClosedBar(
                        ts=last_bar_minute[symbol] + timedelta(minutes=1),
                        open_val=ticks[0],
                        high=max(ticks),
                        low=min(ticks),
                        close=ticks[-1]
                    )

                    bar_history[symbol].append({
                        'ts': bar.ts,
                        'open': bar.open,
                        'high': bar.high,
                        'low': bar.low,
                        'close': bar.close
                    })
                    bar_history[symbol] = bar_history[symbol][-480:]

                    pivot_trackers[symbol].update_bars([{
                        'ts': bar.ts,
                        'open': bar.open,
                        'high': bar.high,
                        'low': bar.low,
                        'close': bar.close
                    }])

                    current_bar_ticks[symbol] = [price]
                    last_bar_minute[symbol] = current_min
                    logger.info(f"[{symbol}] Closed 1m bar at {bar.ts.isoformat()} close={bar.close:.2f} (pricing_provider)")
                else:
                    current_bar_ticks[symbol].append(price)

                if symbol == "NQ" and is_sniper_active_now():
                    run_sniper_logic(symbol, price, bar_history[symbol], open_positions)
                    continue

                if bar is None:
                    continue

                bars_60 = bar_history[symbol]
                if len(bars_60) < 15:
                    continue

                pos_list = open_positions.setdefault(symbol, [])
                for pos in list(pos_list):
                    pos.bars_held += 1
                    exit_reason = check_exit(pos, bar)
                    exit_price = None

                    if exit_reason:
                        exit_price = pos.target_price if exit_reason == "TARGET" else pos.stop_price
                    elif pos.bars_held >= 60 and pos.regime_at_entry not in ("SWING_5M", "SWING_1M"):
                        exit_reason = "TIME"
                        exit_price = bar.close
                    else:
                        support, resistance = pivot_trackers[symbol].get_nearest_walls(bar.close)
                        walls = SwingWalls(support, resistance)
                        walls.session_high = max(b['high'] for b in bars_60)
                        walls.session_low = min(b['low'] for b in bars_60)
                        walls.pivot = (walls.session_high + walls.session_low + bar.close) / 3.0
                        walls.r1 = resistance
                        walls.s1 = support
                        walls.prior_high = walls.session_high
                        walls.prior_low = walls.session_low

                        session_open = bars_60[0]['open']
                        regime, _ = regime_selector.get_regime(
                            symbol=symbol,
                            now_utc=now,
                            price=bar.close,
                            session_open=session_open,
                            session_low=walls.session_low,
                            session_high=walls.session_high,
                            pivot=walls.pivot,
                            recent_bars=bars_60
                        )
                        if pos.regime_at_entry in ("SWING_5M", "SWING_1M"):
                            pass
                        elif regime != pos.regime_at_entry:
                            if not allows_direction(regime, pos.side):
                                exit_reason = "REGIME_FLIP"
                                exit_price = bar.close

                    if exit_reason:
                        pos.exit_ts = bar.ts
                        pos.exit_price = exit_price
                        pos.exit_reason = exit_reason
                        pos.pnl_pts = (pos.exit_price - pos.entry_price) if pos.side == "LONG" else (pos.entry_price - pos.exit_price)

                        close_shadow_position(pos)
                        log_event(symbol, "CLOSE", f"{pos.virtual_id} {exit_reason} pnl={pos.pnl_pts:.1f}")
                        send_notification(format_close(pos))
                        pos_list.remove(pos)
                        logger.info(f"[{symbol}] Closed virtual position {pos.virtual_id}: {exit_reason} pnl={pos.pnl_pts:.1f}")

                max_cap = 2 if symbol in ("DAX", "NQ") else 1
                if len(pos_list) >= max_cap:
                    continue

                support, resistance = pivot_trackers[symbol].get_nearest_walls(bar.close)
                walls = SwingWalls(support, resistance)
                walls.session_high = max(b['high'] for b in bars_60)
                walls.session_low = min(b['low'] for b in bars_60)
                walls.pivot = (walls.session_high + walls.session_low + bar.close) / 3.0
                walls.r1 = resistance
                walls.s1 = support
                walls.prior_high = walls.session_high
                walls.prior_low = walls.session_low

                session_open = bars_60[0]['open']

                regime, source_reg = regime_selector.get_regime(
                    symbol=symbol,
                    now_utc=now,
                    price=bar.close,
                    session_open=session_open,
                    session_low=walls.session_low,
                    session_high=walls.session_high,
                    pivot=walls.pivot,
                    recent_bars=bars_60
                )

                closes = [float(b['close']) for b in bars_60]
                highs = [float(b['high']) for b in bars_60]
                lows = [float(b['low']) for b in bars_60]

                sql_rsi5 = f"SELECT rsi, rsi_direction FROM indicator_state WHERE symbol='{symbol}' AND timeframe='5m' ORDER BY ts DESC LIMIT 1"
                sql_rsi15 = f"SELECT rsi_direction FROM indicator_state WHERE symbol='{symbol}' AND timeframe='15m' ORDER BY ts DESC LIMIT 1"
                res_rsi5 = query_db(sql_rsi5)
                res_rsi15 = query_db(sql_rsi15)

                rsi_5m_val = float(res_rsi5[0]['rsi']) if (res_rsi5 and res_rsi5[0]['rsi'] is not None) else 50.0
                rsi_5m_dir = res_rsi5[0]['rsi_direction'] if res_rsi5 else "FLAT"
                rsi_15m_dir = res_rsi15[0]['rsi_direction'] if res_rsi15 else "FLAT"

                rsi_val = rsi_5m_val

                sql_wpr = f"SELECT wpr FROM indicator_state WHERE symbol='{symbol}' AND timeframe='10s' ORDER BY ts DESC LIMIT 1"
                res_wpr = query_db(sql_wpr)
                wpr_val = float(res_wpr[0]['wpr']) if res_wpr else -50.0

                decision = None
                if symbol in ("DAX", "NQ"):
                    swing_count = sum(1 for p in pos_list if p.regime_at_entry in ("SWING_5M", "SWING_1M"))
                    if swing_count < 1:
                        decision = check_symbol_swing_divergence(symbol, bar.close)

                if decision is None and symbol != "DAX":
                    regular_count = sum(1 for p in pos_list if p.regime_at_entry not in ("SWING_5M", "SWING_1M"))
                    if regular_count < 1:
                        decision = should_enter(symbol, bar.close, walls, regime, [rsi_5m_dir, rsi_15m_dir], wpr_val)

                        if decision is None:
                            def create_spatial_decision(side: str, reason: str) -> EntryDecision:
                                if side == "LONG":
                                    stop = bar.close - 45.0 if symbol == "NQ" else bar.close - bar.close * 0.012
                                    target = bar.close + 90.0 if symbol == "NQ" else bar.close + bar.close * 0.024
                                else:
                                    stop = bar.close + 45.0 if symbol == "NQ" else bar.close + bar.close * 0.012
                                    target = bar.close - 90.0 if symbol == "NQ" else bar.close - bar.close * 0.024
                                return EntryDecision(
                                    side=side,
                                    entry=bar.close,
                                    stop=stop,
                                    target=target,
                                    reason=reason,
                                    sizing_factor=1.0
                                )

                            sweep_res = detect_liquidity_sweep(bars_60)
                            if sweep_res:
                                decision = create_spatial_decision(sweep_res['direction_bias'], f"Spatial pattern: {sweep_res['signal']}")

                            if decision is None:
                                wick_long = detect_wick_rejection(bars_60, level=support, side='lower')
                                if wick_long:
                                    decision = create_spatial_decision("LONG", f"Spatial pattern: {wick_long['signal']}")
                                else:
                                    wick_short = detect_wick_rejection(bars_60, level=resistance, side='upper')
                                    if wick_short:
                                        decision = create_spatial_decision("SHORT", f"Spatial pattern: {wick_short['signal']}")

                            if decision is None:
                                fail_long = detect_failure_to_break(bars_60, swing_level=support, side='low')
                                if fail_long:
                                    decision = create_spatial_decision("LONG", f"Spatial pattern: {fail_long['signal']}")
                                else:
                                    fail_short = detect_failure_to_break(bars_60, swing_level=resistance, side='high')
                                    if fail_short:
                                        decision = create_spatial_decision("SHORT", f"Spatial pattern: {fail_short['signal']}")

                            if decision is None:
                                retest_long = detect_breakout_retest(bars_60, broken_wall=resistance, direction='LONG')
                                if retest_long:
                                    decision = create_spatial_decision("LONG", f"Spatial pattern: {retest_long['signal']}")
                                else:
                                    retest_short = detect_breakout_retest(bars_60, broken_wall=support, direction='SHORT')
                                    if retest_short:
                                        decision = create_spatial_decision("SHORT", f"Spatial pattern: {retest_short['signal']}")

                if decision:
                    if any(p.side != decision.side for p in pos_list):
                        logger.info(f"[{symbol}] Skipping {decision.side} decision because an opposing position is currently open.")
                        decision = None

                if decision:
                    virtual_id = f"V_{symbol}_{int(bar.ts.timestamp())}"
                    room_pts = (walls.resistance - bar.close) if decision.side == "LONG" else (bar.close - walls.support)

                    regime_val = regime
                    source_val = source_reg
                    if symbol in ("DAX", "NQ") and "Swing" in decision.reason:
                        if "5m" in decision.reason:
                            regime_val = "SWING_5M"
                            source_val = "SWING_5M_DIVERGENCE"
                        else:
                            regime_val = "SWING_1M"
                            source_val = "SWING_1M_DIVERGENCE"

                    pos = ShadowPosition(
                        virtual_id=virtual_id,
                        symbol=symbol,
                        side=decision.side,
                        entry_ts=bar.ts,
                        entry_price=decision.entry,
                        stop_price=decision.stop,
                        target_price=decision.target,
                        regime_at_entry=regime_val,
                        ag_response=source_val,
                        walls_snapshot=json.dumps({
                            "session_high": walls.session_high,
                            "session_low": walls.session_low,
                            "prior_high": walls.prior_high,
                            "prior_low": walls.prior_low,
                            "pivot": walls.pivot,
                            "r1": walls.r1,
                            "s1": walls.s1
                        }),
                        rsi_at_entry=rsi_val,
                        wpr_at_entry=wpr_val,
                        room_pts=room_pts,
                        bars_held=0,
                        size=decision.sizing_factor
                    )
                    open_shadow_position(pos)
                    pos_list.append(pos)
                    open_positions[symbol] = pos_list

                    log_event(symbol, "OPEN", f"{virtual_id} {decision.side} entry={decision.entry} stop={decision.stop} target={decision.target}")
                    send_notification(format_open(pos, walls))
                    logger.info(f"[{symbol}] Opened virtual position: {pos.virtual_id}")

                    if os.environ.get("LIVE_EXECUTION", "false").lower() == "true":
                        publish_live_opportunity(pos, walls)
                else:
                    log_event(symbol, "REJECT", f"price={bar.close:.0f} regime={regime} rsi={rsi_val:.1f} wpr={wpr_val:.1f}")

            except Exception as e:
                logger.error(f"Error in shadow loop for {symbol}: {e}", exc_info=True)

        time.sleep(1)

if __name__ == "__main__":
    main()
