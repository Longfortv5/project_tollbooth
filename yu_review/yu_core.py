import logging
import requests
import urllib.parse
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger('yu_core')

MIN_ROOM = {
    "NQ":  60.0,
    "DAX": 40.0,
    "NKD": 200.0,
}

MIN_DISTANCE = {
    "NQ":  20.0,
    "DAX": 15.0,
    "NKD": 80.0,
}

STOP_BUFFER = {
    "NQ":  10.0,
    "DAX": 8.0,
    "NKD": 30.0,
}

class Walls:
    def __init__(self, session_high, session_low, prior_high, prior_low, pivot, r1, s1):
        self.session_high = session_high
        self.session_low = session_low
        self.prior_high = prior_high
        self.prior_low = prior_low
        self.pivot = pivot
        self.r1 = r1
        self.s1 = s1

class SwingWalls:
    def __init__(self, support: float, resistance: float):
        self.support = support
        self.resistance = resistance

class EntryDecision:
    def __init__(self, side: str, entry: float, stop: float, target: float, reason: str, sizing_factor: float = 1.0):
        self.side = side
        self.entry = entry
        self.stop = stop
        self.target = target
        self.reason = reason
        self.sizing_factor = sizing_factor

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "reason": self.reason,
            "sizing_factor": self.sizing_factor
        }

def query_db(sql: str) -> list[dict]:
    url = f"http://localhost:9000/exec?query={urllib.parse.quote(sql)}"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            cols = [c['name'] for c in data.get('columns', [])]
            dataset = data.get('dataset', [])
            return [dict(zip(cols, row)) for row in dataset]
    except Exception as e:
        logger.error(f"QuestDB query failed in yu_core: {e}")
    return []

def get_pivots_from_sqlite(symbol: str) -> tuple[float, float] | None:
    db_path = "/home/longfort/.gemini/antigravity/py_engine/hud_state.db"
    if not os.path.exists(db_path):
        return None
    try:
        db_uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT r1, s1 FROM fa_snapshots WHERE symbol=? AND r1 IS NOT NULL ORDER BY snapshot_id DESC LIMIT 1",
            (symbol.upper(),)
        )
        row = cursor.fetchone()
        conn.close()
        if row and row[0] is not None and row[1] is not None:
            return float(row[0]), float(row[1])
    except Exception as e:
        logger.error(f"Failed to fetch pivots from SQLite for {symbol}: {e}")
    return None

def get_prior_day_hlc(symbol: str, now_utc: datetime, bars_1m: list) -> tuple[float, float, float]:
    today = now_utc.date()
    start_of_today = f"{today.isoformat()}T00:00:00.000000Z"
    
    pivots = get_pivots_from_sqlite(symbol)
    if pivots:
        r1, s1 = pivots
        pivot = (r1 + s1) / 2.0
        logger.info(f"Loaded live pivots from SQLite for {symbol}: r1={r1}, s1={s1}, pivot={pivot}")
        return r1, s1, pivot

    sql_last_date = (
        f"SELECT ts FROM backtest_bars "
        f"WHERE symbol='{symbol}' AND ts < '{start_of_today}' "
        f"ORDER BY ts DESC LIMIT 1"
    )
    res_last = query_db(sql_last_date)
    if res_last:
        last_ts_str = res_last[0]['ts']
        last_date = datetime.fromisoformat(last_ts_str.replace('Z', '+00:00')).date()
        start_str = f"{last_date.isoformat()}T00:00:00.000000Z"
        end_str = f"{last_date.isoformat()}T23:59:59.999999Z"
        sql_hl = (
            f"SELECT max(nq_high) as prior_high, min(nq_low) as prior_low "
            f"FROM backtest_bars WHERE symbol='{symbol}' AND ts BETWEEN '{start_str}' AND '{end_str}'"
        )
        sql_c = (
            f"SELECT nq_close FROM backtest_bars "
            f"WHERE symbol='{symbol}' AND ts BETWEEN '{start_str}' AND '{end_str}' "
            f"ORDER BY ts DESC LIMIT 1"
        )
        res_hl = query_db(sql_hl)
        res_c = query_db(sql_c)
        if res_hl and res_c and res_hl[0]['prior_high'] is not None and res_c[0]['nq_close'] is not None:
            return float(res_hl[0]['prior_high']), float(res_hl[0]['prior_low']), float(res_c[0]['nq_close'])

    prior_bars = []
    for b in bars_1m:
        ts = b.get('ts') if isinstance(b, dict) else getattr(b, 'ts')
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if ts.date() < today:
            prior_bars.append(b)
            
    if prior_bars:
        def get_high(b):
            return b.get('nq_high', b.get('high', 0)) if isinstance(b, dict) else getattr(b, 'high', getattr(b, 'nq_high', 0))
        def get_low(b):
            return b.get('nq_low', b.get('low', 0)) if isinstance(b, dict) else getattr(b, 'low', getattr(b, 'nq_low', 0))
        def get_close(b):
            return b.get('nq_close', b.get('close', 0)) if isinstance(b, dict) else getattr(b, 'close', getattr(b, 'nq_close', 0))
            
        prior_high = max(get_high(b) for b in prior_bars)
        prior_low = min(get_low(b) for b in prior_bars)
        prior_bars_sorted = sorted(prior_bars, key=lambda x: x.get('ts') if isinstance(x, dict) else getattr(x, 'ts'))
        prior_close = get_close(prior_bars_sorted[-1])
        return prior_high, prior_low, prior_close

    last_bar = bars_1m[-1] if bars_1m else {}
    close_px = last_bar.get('nq_close', last_bar.get('close', 0)) if isinstance(last_bar, dict) else getattr(last_bar, 'close', getattr(last_bar, 'nq_close', 0))
    return close_px, close_px, close_px

def compute_walls(symbol: str, now_utc: datetime, bars_1m: list) -> Walls:
    """
    Walls are price levels where the market has shown it cares.
    No magic, no fitting — just session structure.
    """
    today = now_utc.date()
    
    session_bars = []
    for b in bars_1m:
        ts = b.get('ts') if isinstance(b, dict) else getattr(b, 'ts')
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if ts.date() == today:
            session_bars.append(b)
            
    def get_high(b):
        return b.get('nq_high', b.get('high', 0)) if isinstance(b, dict) else getattr(b, 'high', getattr(b, 'nq_high', 0))
    def get_low(b):
        return b.get('nq_low', b.get('low', 0)) if isinstance(b, dict) else getattr(b, 'low', getattr(b, 'nq_low', 0))
        
    if session_bars:
        session_high = max(get_high(b) for b in session_bars)
        session_low = min(get_low(b) for b in session_bars)
    else:
        last_bar = bars_1m[-1] if bars_1m else {}
        close_px = last_bar.get('nq_close', last_bar.get('close', 0)) if isinstance(last_bar, dict) else getattr(last_bar, 'close', getattr(last_bar, 'nq_close', 0))
        session_high = close_px
        session_low = close_px
        
    prior_high, prior_low, prior_close = get_prior_day_hlc(symbol, now_utc, bars_1m)
    
    pivot = (prior_high + prior_low + prior_close) / 3
    r1 = 2 * pivot - prior_low
    s1 = 2 * pivot - prior_high
    
    return Walls(
        session_high=session_high,
        session_low=session_low,
        prior_high=prior_high,
        prior_low=prior_low,
        pivot=pivot,
        r1=r1,
        s1=s1
    )

def compute_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
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

def compute_wpr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(closes) < period:
        return -50.0
    
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    current_close = closes[-1]
    
    denom = hh - ll
    if denom == 0:
        return -50.0
    return ((hh - current_close) / denom) * -100.0

def should_enter(symbol: str, price: float, walls: Walls | SwingWalls, regime: str,
                 rsi_val_or_dir, wpr_14: float) -> EntryDecision | None:
    if regime == "AVOID":
        return None

    if hasattr(walls, 'support'):
        support = walls.support
        resistance = walls.resistance
    else:
        support = walls.s1
        resistance = walls.r1

    if isinstance(rsi_val_or_dir, (list, tuple)):
        rsi_rising = any(d == "RISING" for d in rsi_val_or_dir)
        rsi_falling = any(d == "FALLING" for d in rsi_val_or_dir)
    elif isinstance(rsi_val_or_dir, str):
        rsi_rising = (rsi_val_or_dir == "RISING")
        rsi_falling = (rsi_val_or_dir == "FALLING")
    else:
        rsi_rising = (rsi_val_or_dir > 55.0)
        rsi_falling = (rsi_val_or_dir < 45.0)

    wpr_oversold = (wpr_14 <= -80.0)
    wpr_overbought = (wpr_14 >= -20.0)

    room_up = resistance - price
    distance_to_floor = price - support
    
    room_down = price - support
    distance_to_ceiling = resistance - price

    sizing_factor = 0.5 if regime == "CHOP" else 1.0

    decision = None

    is_mr_long = regime in {"MEAN_REVERT", "MEAN_REVERT_BULLISH", "RANGE", "NEUTRAL", "CHOP", "BULLISH"}
    is_trend_long = regime in {"TREND", "NEUTRAL", "CHOP", "BULLISH"}
    is_pin_long = regime in {"PIN"}

    is_mr_short = regime in {"MEAN_REVERT", "MEAN_REVERT_BEARISH", "RANGE", "NEUTRAL", "CHOP", "BEARISH"}
    is_trend_short = regime in {"TREND", "NEUTRAL", "CHOP", "BEARISH"}
    is_pin_short = regime in {"PIN"}

    if symbol == "NQ" and hasattr(walls, 'session_high') and hasattr(walls, 'session_low'):
        session_range = walls.session_high - walls.session_low
        if session_range >= 250.0:
            pct_in_range = (price - walls.session_low) / session_range if session_range > 0 else 0.5
            if pct_in_range >= 0.70:
                is_mr_short = False
                is_trend_short = False
                is_pin_short = False
                if price >= resistance - 15.0 and rsi_rising and wpr_14 >= -30.0:
                    return EntryDecision(
                        side="LONG",
                        entry=price,
                        stop=price - 45.0,
                        target=price + 90.0,
                        reason=f"Breakout long above Call Wall (spatial trend), range={session_range:.0f}",
                        sizing_factor=sizing_factor
                    )
            elif pct_in_range <= 0.30:
                is_mr_long = False
                is_trend_long = False
                is_pin_long = False
                if price <= support + 15.0 and rsi_falling and wpr_14 <= -70.0:
                    return EntryDecision(
                        side="SHORT",
                        entry=price,
                        stop=price + 45.0,
                        target=price - 90.0,
                        reason=f"Breakdown short below Put Wall (spatial trend), range={session_range:.0f}",
                        sizing_factor=sizing_factor
                    )

    if is_pin_long:
        if abs(distance_to_floor) <= MIN_DISTANCE[symbol] and room_up >= MIN_ROOM[symbol]:
            if rsi_rising and wpr_oversold:
                decision = EntryDecision(
                    side="LONG",
                    entry=price,
                    stop=support - STOP_BUFFER[symbol],
                    target=resistance - (room_up * 0.0085),
                    reason=f"Pin fade long, room={room_up:.0f}, floor={distance_to_floor:.0f}",
                    sizing_factor=sizing_factor
                )
    elif is_mr_long:
        if abs(distance_to_floor) <= MIN_DISTANCE[symbol] and room_up >= MIN_ROOM[symbol]:
            if rsi_rising and wpr_oversold:
                decision = EntryDecision(
                    side="LONG",
                    entry=price,
                    stop=price - 45.0 if symbol == "NQ" else price - price * 0.012,
                    target=price + min(90.0, room_up - 5.0) if symbol == "NQ" else resistance - (room_up * 0.0085),
                    reason=f"MR long, room={room_up:.0f}, floor={distance_to_floor:.0f}",
                    sizing_factor=sizing_factor
                )

    if decision is None and is_trend_long:
        if room_up >= MIN_ROOM[symbol]:
            if rsi_rising and wpr_oversold:
                decision = EntryDecision(
                    side="LONG",
                    entry=price,
                    stop=price - 45.0 if symbol == "NQ" else price - price * 0.012,
                    target=price + min(90.0, room_up - 5.0) if symbol == "NQ" else resistance - (room_up * 0.0085),
                    reason=f"Trend long, room={room_up:.0f}",
                    sizing_factor=sizing_factor
                )

    if is_pin_short:
        if abs(distance_to_ceiling) <= MIN_DISTANCE[symbol] and room_down >= MIN_ROOM[symbol]:
            if rsi_falling and wpr_overbought:
                decision = EntryDecision(
                    side="SHORT",
                    entry=price,
                    stop=resistance + STOP_BUFFER[symbol],
                    target=support + (room_down * 0.0085),
                    reason=f"Pin fade short, room={room_down:.0f}, ceiling={distance_to_ceiling:.0f}",
                    sizing_factor=sizing_factor
                )
    elif is_mr_short:
        if abs(distance_to_ceiling) <= MIN_DISTANCE[symbol] and room_down >= MIN_ROOM[symbol]:
            if rsi_falling and wpr_overbought:
                decision = EntryDecision(
                    side="SHORT",
                    entry=price,
                    stop=price + 45.0 if symbol == "NQ" else price + price * 0.012,
                    target=price - min(90.0, room_down - 5.0) if symbol == "NQ" else support + (room_down * 0.0085),
                    reason=f"MR short, room={room_down:.0f}, ceiling={distance_to_ceiling:.0f}",
                    sizing_factor=sizing_factor
                )

    if decision is None and is_trend_short:
        if room_down >= MIN_ROOM[symbol]:
            if rsi_falling and wpr_overbought:
                decision = EntryDecision(
                    side="SHORT",
                    entry=price,
                    stop=price + 45.0 if symbol == "NQ" else price + price * 0.012,
                    target=price - min(90.0, room_down - 5.0) if symbol == "NQ" else support + (room_down * 0.0085),
                    reason=f"Trend short, room={room_down:.0f}",
                    sizing_factor=sizing_factor
                )

    if decision is not None:
        if decision.side == "LONG":
            if not (decision.stop < decision.entry < decision.target):
                logger.warning(f"Safety gate blocked invalid LONG: stop={decision.stop:.2f}, entry={decision.entry:.2f}, target={decision.target:.2f}")
                return None
        elif decision.side == "SHORT":
            if not (decision.stop > decision.entry > decision.target):
                logger.warning(f"Safety gate blocked invalid SHORT: stop={decision.stop:.2f}, entry={decision.entry:.2f}, target={decision.target:.2f}")
                return None

    return decision


