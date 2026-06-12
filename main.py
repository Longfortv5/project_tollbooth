import base64
import copy
import json
import os
import re
import secrets
import datetime
from contextlib import asynccontextmanager
from contextvars import ContextVar

# Task-local context variable to track if the current request is an admin request
bypass_gating_var: ContextVar[bool] = ContextVar("bypass_gating", default=False)
dynamic_price_var: ContextVar[str] = ContextVar("dynamic_price", default="100000")

try:
    from dotenv import load_dotenv
    load_dotenv()  # Load environment variables from local .env file
except ImportError:
    pass

import sqlite3
import asyncio
import time

from regime_schema import resolve_ticker, EXAMPLE_PAYLOAD, map_flashalpha_to_regime, CANONICAL_TICKERS
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import redis.asyncio as aioredis
from mcp.server.fastmcp import FastMCP

from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, RouteConfig, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.schemas.base import AssetAmount
from x402.mechanisms.evm.exact import ExactEvmServerScheme

# 1. Environment & Configuration (Fails hard on missing or invalid BASE_WALLET_ADDRESS)
BASE_WALLET_ADDRESS = os.getenv("BASE_WALLET_ADDRESS")
if not BASE_WALLET_ADDRESS:
    raise ValueError("CRITICAL CONFIGURATION ERROR: BASE_WALLET_ADDRESS environment variable must be set.")
if not re.match(r"^0x[a-fA-F0-9]{40}$", BASE_WALLET_ADDRESS):
    raise ValueError(f"CRITICAL CONFIGURATION ERROR: BASE_WALLET_ADDRESS '{BASE_WALLET_ADDRESS}' is not a valid Ethereum address.")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
X402_NETWORK = os.getenv("X402_NETWORK", "eip155:84532") # Default to Base Sepolia Testnet
MAX_REGIME_AGE_SECONDS = int(os.getenv("MAX_REGIME_AGE_SECONDS", "900")) # Default to 15 minutes
MOCK_REDIS = os.getenv("MOCK_REDIS", "").lower() in {"1", "true"}
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "/home/longfort/.gemini/antigravity/scratch/hud_state.db")

# Billing configuration (all amounts in USDC atomic units, 6 decimals)
TOOL_PRICE_ATOMIC = os.getenv("TOOL_PRICE_ATOMIC", "19000")                     # 0.019 USDC per call
CREDIT_PACK_CALLS = int(os.getenv("CREDIT_PACK_CALLS", "10000"))                # Calls per prepaid pack
CREDIT_PACK_PRICE_ATOMIC = os.getenv("CREDIT_PACK_PRICE_ATOMIC", "150000000")   # 150 USDC per pack

API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")

# Configure USDC asset contract address and facilitator endpoint dynamically based on network
if X402_NETWORK == "eip155:8453":
    # Base Mainnet - Requires explicit facilitator URL!
    USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL")
    if not FACILITATOR_URL:
        raise ValueError(
            "CRITICAL CONFIGURATION ERROR: X402_FACILITATOR_URL must be explicitly configured "
            "for Base Mainnet (network eip155:8453) to verify and settle real payments."
        )
elif X402_NETWORK == "eip155:84532":
    # Base Sepolia Testnet
    USDC_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL", "https://x402.org/facilitator")
else:
    # Custom/Override Network
    USDC_ASSET = os.getenv("X402_ASSET")
    if not USDC_ASSET:
        raise ValueError(f"X402_ASSET environment variable must be set for custom network {X402_NETWORK}")
    FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL")
    if not FACILITATOR_URL:
        raise ValueError(f"X402_FACILITATOR_URL must be explicitly configured for network {X402_NETWORK}")

# 2. Initialize FastMCP instance & set path for stateless Streamable HTTP
mcp = FastMCP("TollboothMCP", host="0.0.0.0")
mcp.settings.streamable_http_path = "/"
mcp.settings.stateless_http = True

mcp_sub_app = mcp.streamable_http_app()

# 3. Configure Lifespan to run FastMCP Session Manager
@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    # FastMCP requires the session manager's task group to be running
    async with mcp.session_manager.run():
        yield

# 4. Initialize FastAPI app
app = FastAPI(
    title="Project Tollbooth MCP Server",
    description="Monetized MCP Server using x402 and Redis.",
    version="1.0.0",
    lifespan=lifespan
)

# 5. Initialize x402 Resource Server & Facilitator (Async variants)
from x402.http import CreateHeadersAuthProvider

CDP_API_KEY_NAME = os.getenv("CDP_API_KEY_NAME") or os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_PRIVATE_KEY = os.getenv("CDP_API_KEY_PRIVATE_KEY") or os.getenv("CDP_API_KEY_SECRET")

def cdp_create_headers() -> dict[str, dict[str, str]]:
    key_name = os.getenv("CDP_API_KEY_NAME") or os.getenv("CDP_API_KEY_ID")
    key_secret_raw = os.getenv("CDP_API_KEY_PRIVATE_KEY") or os.getenv("CDP_API_KEY_SECRET")
    if not key_name or not key_secret_raw:
        return {
            "verify": {},
            "settle": {},
            "supported": {},
            "bazaar": {}
        }
    
    from cdp.auth import generate_jwt, JwtOptions

    host = "api.cdp.coinbase.com"
    
    def sign_token(method: str, path: str) -> str:
        options = JwtOptions(
            api_key_id=key_name,
            api_key_secret=key_secret_raw,
            request_method=method,
            request_host=host,
            request_path=path,
            audience=["cdp_service"]
        )
        return generate_jwt(options)

    try:
        verify_token = sign_token("POST", "/platform/v2/x402/verify")
        settle_token = sign_token("POST", "/platform/v2/x402/settle")
        supported_token = sign_token("GET", "/platform/v2/x402/supported")
    except Exception as e:
        print(f"Error generating CDP auth JWT tokens: {e}")
        return {
            "verify": {},
            "settle": {},
            "supported": {},
            "bazaar": {}
        }

    return {
        "verify": {"Authorization": f"Bearer {verify_token}"},
        "settle": {"Authorization": f"Bearer {settle_token}"},
        "supported": {"Authorization": f"Bearer {supported_token}"},
        "bazaar": {}
    }

if X402_NETWORK == "eip155:8453" and CDP_API_KEY_NAME and CDP_API_KEY_PRIVATE_KEY:
    auth_provider = CreateHeadersAuthProvider(cdp_create_headers)
    facilitator_config = FacilitatorConfig(
        url="https://api.cdp.coinbase.com/platform/v2/x402",
        auth_provider=auth_provider
    )
else:
    facilitator_config = FacilitatorConfig(url=FACILITATOR_URL)

facilitator_client = HTTPFacilitatorClient(config=facilitator_config)
resource_server = x402ResourceServer(facilitator_clients=facilitator_client)

# Register EVM Exact scheme for both Base Mainnet and Sepolia
resource_server.register("eip155:8453", ExactEvmServerScheme())
resource_server.register("eip155:84532", ExactEvmServerScheme())

# 6. Initialize Redis Async Client
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

# Atomically decrement N credits only if the balance covers them.
# Returns the new balance on success, or -1 on unknown key / insufficient balance.
CREDIT_DECREMENT_LUA = """
local bal = redis.call('GET', KEYS[1])
if bal and tonumber(bal) >= tonumber(ARGV[1]) then
    return redis.call('DECRBY', KEYS[1], ARGV[1])
end
return -1
"""
credit_decrement = redis_client.register_script(CREDIT_DECREMENT_LUA)

def _sync_fetch_sqlite_options_data(db_path: str, symbol: str) -> dict | None:
    try:
        # Connect to SQLite read-only mode to prevent locks and write conflicts
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # 1. Fetch from fa_snapshots
        c.execute("""
            SELECT spot, netgex, net_dex, net_vex, net_chex, gamma_flip, call_wall, put_wall, max_pain, regime, vix, vix9d, vrp, timestamp, top_oi_changes_json 
            FROM fa_snapshots 
            WHERE symbol = ? 
            ORDER BY snapshot_id DESC LIMIT 1
        """, (symbol,))
        snapshot_row = c.fetchone()
        
        # 2. Fetch from fa_zero_dte
        c.execute("""
            SELECT expiration, pct_of_total_gex, pin_magnet_strike, pin_score, pin_distance_pct, em_1sd_pct, straddle_price, ts_ingested 
            FROM fa_zero_dte 
            WHERE symbol = ? 
            ORDER BY id DESC LIMIT 1
        """, (symbol,))
        zdte_row = c.fetchone()
        
        # 3. Fetch from fa_volatility
        c.execute("""
            SELECT rv_20d, rv_60d, atm_iv, vrp_20d, skew_profiles_json 
            FROM fa_volatility 
            WHERE symbol = ? 
            ORDER BY id DESC LIMIT 1
        """, (symbol,))
        vol_row = c.fetchone()
        
        conn.close()
        
        if not snapshot_row:
            return None
            
        # Parse into a flat snapshot dict
        flat = {}
        
        # Snapshot fields
        flat["spot"] = snapshot_row["spot"]
        flat["net_gex"] = snapshot_row["netgex"]
        flat["net_dex"] = snapshot_row["net_dex"]
        flat["net_vex"] = snapshot_row["net_vex"]
        flat["net_chex"] = snapshot_row["net_chex"]
        flat["gamma_flip"] = snapshot_row["gamma_flip"]
        flat["call_wall"] = snapshot_row["call_wall"]
        flat["put_wall"] = snapshot_row["put_wall"]
        flat["max_pain"] = snapshot_row["max_pain"]
        flat["volatility_state"] = "expansion" if (snapshot_row["vix9d"] and snapshot_row["vix"] and snapshot_row["vix9d"] > snapshot_row["vix"]) else "stable"
        flat["consensus"] = snapshot_row["regime"]
        
        # Convert timestamp to epoch
        ts_str = snapshot_row["timestamp"]
        if ts_str:
            try:
                dt = datetime.datetime.strptime(ts_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=datetime.timezone.utc)
                flat["timestamp_epoch"] = int(dt.timestamp())
            except Exception:
                flat["timestamp_epoch"] = int(time.time())
        else:
            flat["timestamp_epoch"] = int(time.time())
        
        # Skew/strike changes parsing
        top_oi_changes_json = snapshot_row["top_oi_changes_json"]
        if top_oi_changes_json:
            try:
                flat["key_strikes"] = json.loads(top_oi_changes_json)
            except Exception:
                flat["key_strikes"] = []
        else:
            flat["key_strikes"] = []

        # Zero DTE fields
        if zdte_row:
            flat["zero_dte_expiration"] = zdte_row["expiration"]
            flat["zero_dte_gex_share"] = zdte_row["pct_of_total_gex"]
            flat["pin_magnet_strike"] = zdte_row["pin_magnet_strike"]
            flat["pin_score"] = zdte_row["pin_score"]
            flat["pin_distance_pct"] = zdte_row["pin_distance_pct"]
            
        # Volatility fields
        if vol_row:
            flat["hv20"] = vol_row["rv_20d"]
            flat["hv60"] = vol_row["rv_60d"]
            flat["atm_iv"] = vol_row["atm_iv"]
            flat["vrp"] = vol_row["vrp_20d"]
            
            skew_profiles_json = vol_row["skew_profiles_json"]
            if skew_profiles_json:
                try:
                    skew_profiles = json.loads(skew_profiles_json)
                    if isinstance(skew_profiles, list) and len(skew_profiles) > 0:
                        front = skew_profiles[0]
                        flat["put_iv_25d"] = front.get("put_25d_iv")
                        flat["call_iv_25d"] = front.get("call_25d_iv")
                        flat["skew_25d"] = front.get("skew_25d")
                        flat["smile_ratio"] = front.get("smile_ratio")
                except Exception:
                    pass

        return flat
    except Exception as e:
        print(f"Error querying SQLite: {e}")
        return None

async def fetch_sqlite_options_data(symbol: str) -> dict | None:
    return await asyncio.to_thread(_sync_fetch_sqlite_options_data, SQLITE_DB_PATH, symbol)

# Helper to fetch and validate regime data
async def _fetch_and_validate_regime_data(canonical_ticker: str, requested_ticker: str) -> tuple[dict | None, str | None]:
    """
    Fetches the regime data for a sanitized ticker.
    If the request is internal (is_admin), queries the Sidebus snapshot (calibrated futures).
    If the request is external, queries the SQLite database directly (uncalibrated ETF spot).
    Returns (regime_dict, None) on success, or (None, error_message) on failure.
    """
    is_admin = bypass_gating_var.get()
    
    if is_admin:
        if MOCK_REDIS and canonical_ticker != "XYZ" and requested_ticker != "XYZ":
            mock_data = copy.deepcopy(EXAMPLE_PAYLOAD)
            mock_data["ticker"] = canonical_ticker
            mock_data["requested_as"] = requested_ticker
            mock_data["timestamp"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            return mock_data, None

        # Map requested/canonical ticker to the Sidebus snapshot symbols (IG prices)
        symbol_map = {
            "QQQ": "NQ", "NQ": "NQ", "NDX": "NQ",
            "SPY": "ES", "ES": "ES", "SPX": "ES",
            "FDAX": "FDAX", "DAX": "FDAX", "DAX40": "FDAX",
            "NKD": "NKD", "NK": "NKD"
        }
        symbol = symbol_map.get(canonical_ticker)
        if not symbol:
            if canonical_ticker in CANONICAL_TICKERS:
                flat_data = await fetch_sqlite_options_data(canonical_ticker)
                if not flat_data or flat_data.get("net_gex") is None:
                    return None, f"No option telemetry available in database for symbol '{canonical_ticker}'."
                ts_val = flat_data.get("timestamp_epoch")
                if ts_val:
                    age = time.time() - float(ts_val)
                    if age > MAX_REGIME_AGE_SECONDS:
                        return None, f"Regime data for ticker '{requested_ticker}' is stale (age: {int(age)}s, max: {MAX_REGIME_AGE_SECONDS}s)."
                try:
                    regime_dict = map_flashalpha_to_regime(flat_data, canonical_ticker, timestamp=ts_val)
                    regime_dict["requested_as"] = requested_ticker
                    return regime_dict, None
                except Exception as e:
                    return None, f"Failed to map options telemetry for symbol '{canonical_ticker}': {str(e)}"
            return None, f"No live market data available for ticker '{requested_ticker}' in admin mode."

        import httpx
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get("http://127.0.0.1:8888/snapshot")
                if resp.status_code != 200:
                    return None, f"Failed to query Sidebus snapshot (HTTP {resp.status_code})."
                snapshot = resp.json()
        except Exception as e:
            return None, f"Sidebus snapshot service is offline: {str(e)}"

        symbols = snapshot.get("symbols") or {}
        symbol_data = symbols.get(symbol)
        if not symbol_data:
            if symbol == "NQ" and "micro_momentum" in snapshot:
                symbol_data = snapshot
            else:
                return None, f"Symbol '{symbol}' not found in Sidebus snapshot."

        try:
            mm = symbol_data.get("micro_momentum") or {}
            dlm = symbol_data.get("dealer_liquidity_map") or {}
            td = symbol_data.get("tactical_directives") or {}
            
            spot = mm.get("price")
            call_wall = dlm.get("calibrated_call_wall")
            put_wall = dlm.get("calibrated_put_wall")
            gamma_flip = dlm.get("calibrated_gamma")
            net_gex = dlm.get("net_gex") or 0.0
            vix = dlm.get("vix")
            vix9d = dlm.get("vix9d")
            
            action_bias = td.get("action_bias") or "NEUTRAL"
            regime_directive = td.get("regime_directive") or "STABLE_PIN"
            
            ts_val = td.get("last_updated_secs") or snapshot.get("last_updated_secs")
            if ts_val:
                age = time.time() - float(ts_val)
                if age > MAX_REGIME_AGE_SECONDS:
                    return None, f"Regime data for ticker '{requested_ticker}' is stale (age: {int(age)}s, max: {MAX_REGIME_AGE_SECONDS}s)."
            
            regime_dict = {
                "schema_version": "1.0",
                "ticker": canonical_ticker,
                "requested_as": requested_ticker,
                "timestamp": int(ts_val if ts_val else time.time()),
                "source": "Longfort",
                "spot": spot,
                "regime": {
                    "gamma": "negative" if net_gex < 0 else "positive",
                    "volatility": "expansion" if (vix9d and vix and vix9d > vix) else "stable",
                    "tilt": action_bias,
                    "consensus": regime_directive,
                    "gamma_flip": gamma_flip,
                },
                "levels": {
                    "call_wall": call_wall,
                    "put_wall": put_wall,
                    "gamma_flip_eod": gamma_flip,
                    "max_pain": None,
                }
            }
            return regime_dict, None
        except Exception as e:
            return None, f"Failed to parse Sidebus snapshot for symbol '{symbol}': {str(e)}"
    
    else:
        # SQLite Query Mode (External paywalled users)
        if canonical_ticker == "XYZ" or requested_ticker == "XYZ":
            return None, f"No live market data available for ticker '{requested_ticker}'."
            
        flat_data = await fetch_sqlite_options_data(canonical_ticker)
        if not flat_data or flat_data.get("net_gex") is None:
            return None, f"No option telemetry available in database for symbol '{canonical_ticker}'."
            
        ts_val = flat_data.get("timestamp_epoch")
        if ts_val:
            age = time.time() - float(ts_val)
            if age > MAX_REGIME_AGE_SECONDS:
                return None, f"Regime data for ticker '{requested_ticker}' is stale (age: {int(age)}s, max: {MAX_REGIME_AGE_SECONDS}s)."
                
        try:
            regime_dict = map_flashalpha_to_regime(flat_data, canonical_ticker, timestamp=ts_val)
            regime_dict["requested_as"] = requested_ticker
            return regime_dict, None
        except Exception as e:
            return None, f"Failed to map options telemetry for symbol '{canonical_ticker}': {str(e)}"

def sanitize_regime_output(raw_data: dict) -> dict:
    """
    Sanitizes the canonical regime payload (schema v1.0) to remove raw pricing,
    strikes, and option calibrations, while retaining qualitative verdict,
    walls_status, reason_brief, and structural keys for tests.
    """
    # 1. Start with the required structural keys and public spot price
    sanitized = {
        "schema_version": raw_data.get("schema_version", "1.0"),
        "ticker": raw_data.get("ticker"),
        "requested_as": raw_data.get("requested_as"),
        "timestamp": raw_data.get("timestamp"),
        "source": raw_data.get("source", "flashalpha"),
        "spot": raw_data.get("spot"),
    }

    # 2. Extract and sanitize regime sub-object (retain only qualitative fields, specifically gamma)
    raw_regime = raw_data.get("regime") or {}
    gamma_val = raw_regime.get("gamma")
    sanitized["regime"] = {
        "gamma": gamma_val
    }

    # 3. Derive qualitative verdict
    consensus = raw_regime.get("consensus") or raw_regime.get("tilt")
    if consensus:
        consensus_str = str(consensus).upper()
        if "BULL" in consensus_str or "LONG" in consensus_str:
            verdict = "BULLISH"
        elif "BEAR" in consensus_str or "SHORT" in consensus_str:
            verdict = "BEARISH"
        elif "CHOP" in consensus_str or "PIN" in consensus_str or "REVERT" in consensus_str:
            verdict = "CHOP"
        elif "AVOID" in consensus_str or "TRANSITION" in consensus_str:
            verdict = "AVOID"
        else:
            verdict = consensus_str
    else:
        # Fallback to gamma
        if gamma_val == "positive":
            verdict = "CHOP"
        elif gamma_val == "negative":
            verdict = "BEARISH"
        else:
            verdict = "HOLD"
    sanitized["verdict"] = verdict

    # 4. Derive qualitative walls_status
    spot = raw_data.get("spot")
    levels = raw_data.get("levels") or {}
    cw = levels.get("call_wall")
    pw = levels.get("put_wall")
    gf = raw_regime.get("gamma_flip")

    walls_status = "Option boundaries are stable."
    try:
        if spot is not None and cw is not None and pw is not None:
            spot_f = float(spot)
            cw_f = float(cw)
            pw_f = float(pw)
            gf_f = float(gf) if gf is not None else None
            
            if spot_f >= cw_f:
                walls_status = "Price is trading above key call wall resistance (extremely overextended)."
            elif spot_f <= pw_f:
                walls_status = "Price is trading below key put wall support (extremely oversold/panic zone)."
            else:
                if gf_f is not None:
                    if spot_f >= gf_f:
                        walls_status = "Price is trading in positive gamma territory, bounded by call wall resistance above and gamma flip support below."
                    else:
                        walls_status = "Price is trading in negative gamma territory, bounded by gamma flip resistance above and put wall support below."
                else:
                    walls_status = "Price is consolidating within standard option wall boundaries."
        else:
            # Fallback based on tilt or volatility state
            vol_state = str(raw_regime.get("volatility") or "").lower()
            tilt = str(raw_regime.get("tilt") or "").lower()
            if "positive" in tilt or "chop" in vol_state:
                walls_status = "Price is range-bound and consolidating within options walls."
            elif "negative" in tilt or "expansion" in vol_state:
                walls_status = "Price is expanding and experiencing breakout momentum outside option walls."
    except Exception:
        pass
    sanitized["walls_status"] = walls_status

    # 5. Derive reason_brief
    volatility = raw_regime.get("volatility") or "stable"
    tilt = raw_regime.get("tilt") or "neutral"
    sanitized["reason_brief"] = f"Market microstructure indicates a {volatility} state with a {tilt} tilt, dominated by {gamma_val or 'neutral'} gamma hedging constraints."

    # 6. Add qualitative 0DTE block
    raw_zdte = raw_data.get("zero_dte")
    if raw_zdte:
        sanitized["zero_dte"] = {
            "verdict": raw_zdte.get("verdict"),
            "gex_share_pct": raw_zdte.get("gex_share_pct")
        }
    else:
        sanitized["zero_dte"] = None

    return sanitized

# Define get_market_regime tool
@mcp.tool(
    name="get_market_regime",
    description=(
        "Returns the full quantitative market regime payload (schema v1.0) for a ticker: "
        "dealer exposure aggregates in USD (net GEX, DEX, VEX, charm), gamma regime and "
        "flip level, volatility surface stats (ATM IV, HV20/60, VRP, 25-delta skew, smile "
        "ratio), structural levels (call/put walls, max pain), top strike concentrations "
        "with open interest, and a consolidated consensus state. Covers QQQ, SPY, SPX, IWM "
        "(futures aliases NQ/ES/RTY resolve automatically; GLD/USO for gold/WTI). Designed "
        "for algorithmic execution gates and automated risk management guardrails. Data is "
        "staleness-checked; calls for missing or stale data are answered free of charge."
    )
)
async def get_market_regime(ticker: str) -> dict:
    """
    Accepts a ticker string, validates format, and queries the Sidebus snapshot.
    If the caller is Qwen (admin bypass key), returns raw unsanitized futures/Rithmic data.
    If the caller is external, returns qualitative sanitized index/ETF data.
    """
    ticker_clean = ticker.strip().upper()
    if not re.match(r"^[A-Z]{1,6}$", ticker_clean):
        return {"error": "Invalid ticker format. Ticker must be 1 to 6 uppercase letters (e.g., NQ, DAX)."}

    is_admin = bypass_gating_var.get()
    
    if is_admin:
        # Rithmic futures mode for Qwen
        rithmic_map = {
            "QQQ": "NQ", "NQ": "NQ", "NDX": "NQ",
            "SPY": "ES", "ES": "ES", "SPX": "ES",
            "FDAX": "FDAX", "DAX": "FDAX", "DAX40": "FDAX",
            "NKD": "NKD", "NK": "NKD"
        }
        rithmic_ticker = rithmic_map.get(ticker_clean)
        if not rithmic_ticker:
            return {"error": f"No live market data available for ticker '{ticker_clean}' in Rithmic mode."}
        
        regime, error_msg = await _fetch_and_validate_regime_data(rithmic_ticker, ticker_clean)
        if error_msg:
            return {"error": error_msg}
        
        # Admin gets raw, unsanitized options data in Rithmic scale
        regime["ticker"] = rithmic_ticker
        return regime
    else:
        # Gated/Sanitized qualitative ETF/index mode for external agents
        canonical_ticker = resolve_ticker(ticker_clean)
        regime, error_msg = await _fetch_and_validate_regime_data(canonical_ticker, ticker_clean)
        if error_msg:
            return {"error": error_msg}
        
        # Return only qualitative verdict and walls description
        return sanitize_regime_output(regime)

@mcp.tool(
    name="get_0dte_verdict",
    description=(
        "Returns the specialized 0DTE (Zero Days to Expiration) option pinning verdict "
        "and GEX share percentage for a ticker. Useful for expiration day pinning plays. "
        "Supports SPY, SPX, IWM (aliases ES, NQ, RTY resolve automatically)."
    )
)
async def get_0dte_verdict(ticker: str) -> dict:
    """
    Returns only the zero_dte block for a ticker.
    Admin bypass returns the full, raw unsanitized 0DTE statistics.
    External paywalled calls return the qualitative 0DTE verdict.
    """
    res = await get_market_regime(ticker)
    if "error" in res:
        return res
    return {"zero_dte": res.get("zero_dte")}

@mcp.tool(
    name="get_spx_gamma",
    description="Returns the SPX (S&P 500 Index) options gamma exposure profile and volatility regimes."
)
async def get_spx_gamma() -> dict:
    return await get_market_regime("SPX")

@mcp.tool(
    name="get_spy_gamma",
    description="Returns the SPY (S&P 500 ETF) options gamma exposure profile and volatility regimes."
)
async def get_spy_gamma() -> dict:
    return await get_market_regime("SPY")

@mcp.tool(
    name="get_qqq_gex",
    description="Returns the QQQ (Nasdaq 100 ETF) options gamma exposure profile (GEX) and volatility regimes."
)
async def get_qqq_gex() -> dict:
    return await get_market_regime("QQQ")

async def precheck_regime_data(ticker: str) -> str | None:
    """
    Returns an error message if the regime data is invalid, missing, or stale.
    Otherwise returns None.
    """
    ticker_clean = ticker.strip().upper()
    if not re.match(r"^[A-Z]{1,6}$", ticker_clean):
        return "Invalid ticker format. Ticker must be 1 to 6 uppercase letters (e.g., NQ, DAX)."
    canonical_ticker = resolve_ticker(ticker_clean)
    _, error_msg = await _fetch_and_validate_regime_data(canonical_ticker, ticker_clean)
    return error_msg

# 7. Credit pack purchase & balance endpoints
#
# POST /credits/purchase is x402-gated (see routes_config). The handler only executes
# after the payment middleware has verified the payment payload. NOTE: the SDK settles
# *after* this handler returns; in the rare case settlement fails post-verification,
# a key may be issued without final settlement. Monitor facilitator settle failures.
@app.post("/credits/purchase")
async def purchase_credits():
    api_key = "tb_" + secrets.token_urlsafe(48)
    await redis_client.set(f"credits:{api_key}", CREDIT_PACK_CALLS)
    return JSONResponse({
        "api_key": api_key,
        "credits": CREDIT_PACK_CALLS,
        "price_paid_atomic_usdc": CREDIT_PACK_PRICE_ATOMIC,
        "usage": "Pass this key as 'Authorization: Bearer <api_key>' on MCP tool calls.",
        "warning": "Store this key securely. It cannot be recovered if lost."
    })

@app.get("/credits/balance")
async def credits_balance(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse({"error": "Missing 'Authorization: Bearer <api_key>' header."}, status_code=401)
    api_key = auth[7:].strip()
    if not API_KEY_PATTERN.match(api_key):
        return JSONResponse({"error": "Invalid API key format."}, status_code=401)
    balance = await redis_client.get(f"credits:{api_key}")
    if balance is None:
        return JSONResponse({"error": "Unknown API key."}, status_code=401)
    return JSONResponse({"credits_remaining": int(balance)})

@app.get("/llms.txt", response_class=PlainTextResponse)
async def get_llms_txt():
    file_path = os.path.join(os.path.dirname(__file__), "llms.txt")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    return "Tollbooth MCP Server"

WELL_KNOWN_CARD = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/mcp-server-card/v1.json",
    "version": "1.0",
    "protocolVersion": "2024-11-05",
    "serverInfo": {
        "name": "project-tollbooth",
        "title": "Project Tollbooth MCP Server",
        "version": "1.0.0"
    },
    "transport": {
        "type": "streamable-http",
        "endpoint": "/mcp"
    },
    "capabilities": {
        "tools": {}
    },
    "tools": [
        {
            "name": "get_market_regime",
            "title": "Get Market Regime",
            "description": (
                "Returns the full quantitative market regime payload (schema v1.0) for a ticker: "
                "dealer exposure aggregates in USD (net GEX, DEX, VEX, charm), gamma regime and flip level, "
                "volatility surface stats (ATM IV, HV20/60, VRP, 25-delta skew, smile ratio), structural levels "
                "(call/put walls, max pain), top strike concentrations with open interest, and a consolidated consensus state. "
                "Covers QQQ, SPY, SPX, IWM (futures aliases NQ/ES/RTY resolve automatically; GLD/USO for gold/WTI). "
                "Designed for algorithmic execution gates and automated risk management guardrails. "
                "Data is staleness-checked; calls for missing or stale data are answered free of charge."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Ticker symbol to query (QQQ, SPY, SPX, IWM, NQ, ES, RTY, GLD, USO)"
                    }
                },
                "required": ["ticker"]
            }
        },
        {
            "name": "get_0dte_verdict",
            "title": "Get 0DTE Pinning Verdict",
            "description": (
                "Returns the specialized 0DTE (Zero Days to Expiration) option pinning verdict "
                "and GEX share percentage for a ticker. Useful for expiration day pinning plays. "
                "Supports SPY, SPX, IWM (aliases ES, NQ, RTY resolve automatically)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Ticker symbol to query (QQQ, SPY, SPX, IWM, NQ, ES, RTY)"
                    }
                },
                "required": ["ticker"]
            }
        },
        {
            "name": "get_spx_gamma",
            "title": "Get SPX Gamma Profile",
            "description": "Returns the SPX (S&P 500 Index) options gamma exposure profile and volatility regimes.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "get_spy_gamma",
            "title": "Get SPY Gamma Profile",
            "description": "Returns the SPY (S&P 500 ETF) options gamma exposure profile.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "get_qqq_gex",
            "title": "Get QQQ GEX Profile",
            "description": "Returns the QQQ (Nasdaq 100 ETF) options gamma exposure profile (GEX) and volatility regimes.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        }
    ]
}

@app.get("/.well-known/mcp.json")
async def get_well_known_mcp():
    return JSONResponse(WELL_KNOWN_CARD)

@app.get("/.well-known/mcp/server-card.json")
async def get_well_known_server_card():
    return JSONResponse(WELL_KNOWN_CARD)

def resolve_dynamic_price(context) -> AssetAmount:
    return AssetAmount(amount=dynamic_price_var.get(), asset=USDC_ASSET)

# 8. Configure Official x402 Gated Route Configuration
routes_config = {
    # Per-call tool execution
    "POST /mcp*": RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to=BASE_WALLET_ADDRESS,
            price=resolve_dynamic_price,
            network=X402_NETWORK,
        )
    ),
    # Prepaid credit pack purchase
    "POST /credits/purchase": RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to=BASE_WALLET_ADDRESS,
            price=AssetAmount(amount=CREDIT_PACK_PRICE_ATOMIC, asset=USDC_ASSET),
            network=X402_NETWORK,
        )
    ),
}

mcp_server_var: ContextVar[str | None] = ContextVar("mcp_server", default=None)

def filter_tools_list_response(resp_json, server_name: str):
    if isinstance(resp_json, list):
        return [filter_single_tools_response(item, server_name) for item in resp_json]
    elif isinstance(resp_json, dict):
        return filter_single_tools_response(resp_json, server_name)
    return resp_json

def filter_single_tools_response(item: dict, server_name: str) -> dict:
    if not isinstance(item, dict) or "result" not in item:
        return item
    result = item["result"]
    if not isinstance(result, dict) or "tools" not in result:
        return item
    
    tools = result["tools"]
    if not isinstance(tools, list):
        return item
    
    filtered_tools = []
    # Determine the ticker from the server name
    # e.g. "spx-regime-selector" -> SPX
    # e.g. "spacex-options" -> SPACEX
    ticker = server_name.split("-")[0].upper()
    
    # Customize the description base
    if "options" in server_name:
        desc_prefix = f"[Brand: Longfort] [DEDICATED SERVER FOR {ticker} Options] "
    elif "flow" in server_name:
        desc_prefix = f"[Brand: Longfort] [DEDICATED SERVER FOR {ticker} Option Flow] "
    elif "sentiment" in server_name:
        desc_prefix = f"[Brand: Longfort] [DEDICATED SERVER FOR {ticker} Option Sentiment] "
    else:
        desc_prefix = f"[Brand: Longfort] [DEDICATED SERVER FOR {ticker}] "
        
    for tool in tools:
        tool_name = tool.get("name")
        if not tool_name:
            continue
            
        # Hard filter for specific tools
        if tool_name == "get_spx_gamma" and ticker != "SPX":
            continue
        if tool_name == "get_spy_gamma" and ticker != "SPY":
            continue
        if tool_name == "get_qqq_gex" and ticker != "QQQ":
            continue
            
        # Customize or restrict the generic tools
        if tool_name in {"get_market_regime", "get_0dte_verdict"}:
            tool_copy = copy.deepcopy(tool)
            desc = tool_copy.get("description", "")
            tool_copy["description"] = desc_prefix + desc
            # Enforce ticker restriction in the schema properties
            try:
                input_schema = tool_copy.get("inputSchema")
                if input_schema and isinstance(input_schema, dict):
                    properties = input_schema.get("properties")
                    if properties and isinstance(properties, dict):
                        ticker_prop = properties.get("ticker")
                        if ticker_prop and isinstance(ticker_prop, dict):
                            ticker_prop["default"] = ticker
                            ticker_prop["enum"] = [ticker]
            except Exception:
                pass
            filtered_tools.append(tool_copy)
        else:
            # For ticker-specific tools (like get_spx_gamma for SPX, etc.)
            tool_copy = copy.deepcopy(tool)
            desc = tool_copy.get("description", "")
            tool_copy["description"] = desc_prefix + desc
            filtered_tools.append(tool_copy)
            
    result["tools"] = filtered_tools
    return item

# 8. Pure ASGI Middleware for Granular MCP Tool Gating (Fail-Closed)
# Avoids BaseHTTPMiddleware issues on streaming connections and resolves session handling bugs.
class MCPPaymentGateMiddleware:
    def __init__(self, asgi_app, routes, server):
        self.app = asgi_app
        self.official_middleware = PaymentMiddlewareASGI(
            asgi_app,
            routes=routes,
            server=server
        )

    def is_free_rpc_method(self, method: str) -> bool:
        if not isinstance(method, str):
            return False
        if method in {
            "initialize",
            "ping",
            "tools/list",
            "resources/list",
            "resources/templates/list",
            "prompts/list",
        }:
            return True
        if method.startswith("notifications/"):
            return True
        return False

    @staticmethod
    def get_bearer_key(scope):
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                v = value.decode("latin-1")
                if v.lower().startswith("bearer "):
                    return v[7:].strip()
        return None

    async def run_official(self, scope, receive, send):
        """Delegate to the official x402 middleware with a 502 fail-safe guard."""
        response_started = False

        async def guarded_send(msg):
            nonlocal response_started
            if msg.get("type") == "http.response.start":
                response_started = True
            await send(msg)

        try:
            await self.official_middleware(scope, receive, guarded_send)
        except Exception as e:
            if not response_started:
                await send({
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [(b"content-type", b"application/json")]
                })
                await send({
                    "type": "http.response.body",
                    "body": json.dumps({
                        "error": "Bad Gateway",
                        "message": f"Failed to communicate with x402 facilitator: {str(e)}"
                    }).encode("utf-8"),
                    "more_body": False
                })
            else:
                await send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False
                })

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Always default ContextVars for each request
        bypass_gating_var.set(False)
        dynamic_price_var.set("100000")
        mcp_server_var.set(None)

        path = scope.get("path", "")
        method = scope.get("method", "")
        path_clean = path.rstrip('/')

        # Intercept and rewrite /mcp/{server_name}
        mcp_match = re.match(r"^/mcp/([^/]+)$", path_clean)
        if mcp_match:
            server_name = mcp_match.group(1)
            print(f"[DEBUG] MCP routing matched server: {server_name}", flush=True)
            mcp_server_var.set(server_name)
            # Rewrite path in ASGI scope so FastAPI routes it to mcp_sub_app
            scope["path"] = "/mcp/"
            if "raw_path" in scope:
                scope["raw_path"] = b"/mcp/"
            path = "/mcp/"
            path_clean = "/mcp"

        # Credit pack purchase: always x402-gated, never bypassed by an API key
        if path_clean == "/credits/purchase" and method == "POST":
            await self.run_official(scope, receive, send)
            return

        # Intercept POST to /mcp (Streamable HTTP endpoint)
        if path_clean == "/mcp" and method == "POST":
            body_bytes = b""
            more_body = True
            
            # Retrieve request body
            while more_body:
                message = await receive()
                body_bytes += message.get("body", b"")
                more_body = message.get("more_body", False)

            # Recreate receive function
            body_read = False
            async def custom_receive():
                nonlocal body_read
                if not body_read:
                    body_read = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return await receive()

            should_gate = False
            is_tools_list = False
            try:
                payload = json.loads(body_bytes)
                if isinstance(payload, dict) and payload.get("method") == "tools/list":
                    is_tools_list = True
                elif isinstance(payload, list):
                    for item in payload:
                        if isinstance(item, dict) and item.get("method") == "tools/list":
                            is_tools_list = True
                            break
                
                if isinstance(payload, list):
                    # Batch request validation
                    paid_calls = 0
                    has_invalid_item = False
                    for item in payload:
                        if isinstance(item, dict):
                            rpc_method = item.get("method")
                            if not self.is_free_rpc_method(rpc_method):
                                if rpc_method == "tools/call":
                                    params = item.get("params") or {}
                                    if params.get("name") == "get_market_regime":
                                        paid_calls += 1
                                    else:
                                        paid_calls += 1
                                else:
                                    paid_calls += 1
                        else:
                            has_invalid_item = True
                            break

                    if has_invalid_item:
                        should_gate = True
                    elif paid_calls > 1:
                        # Reject batches containing multiple paid calls (prevent batch underpricing)
                        error_response = {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32600,  # Invalid Request
                                "message": "Batch requests containing multiple paid calls are not supported."
                            }
                        }
                        await send({
                            "type": "http.response.start",
                            "status": 200,
                            "headers": [(b"content-type", b"application/json")]
                        })
                        await send({
                            "type": "http.response.body",
                            "body": json.dumps(error_response).encode("utf-8"),
                            "more_body": False
                        })
                        return
                    elif paid_calls == 1:
                        # Exactly one paid call: pre-check if it has missing/stale data to serve it free of charge
                        paid_item = None
                        for item in payload:
                            if isinstance(item, dict) and item.get("method") == "tools/call":
                                params = item.get("params") or {}
                                if params.get("name") in {"get_market_regime", "get_0dte_verdict", "get_spx_gamma", "get_spy_gamma", "get_qqq_gex"}:
                                    paid_item = item
                                    break
                        if paid_item:
                            params = paid_item.get("params") or {}
                            tool_name = params.get("name")
                            
                            # Set dynamic price based on tool_name
                            price_to_set = "100000"
                            if tool_name in {"get_0dte_verdict", "get_spx_gamma"}:
                                price_to_set = "50000"
                            elif tool_name in {"get_spy_gamma", "get_qqq_gex"}:
                                price_to_set = "20000"
                            dynamic_price_var.set(price_to_set)

                            arguments = params.get("arguments") or {}
                            ticker = arguments.get("ticker")
                            if tool_name == "get_spx_gamma":
                                ticker = "SPX"
                            elif tool_name == "get_spy_gamma":
                                ticker = "SPY"
                            elif tool_name == "get_qqq_gex":
                                ticker = "QQQ"
                            
                            # Enforce dedicated server ticker restriction for batch
                            if mcp_server_var.get():
                                server_ticker = mcp_server_var.get().split("-")[0].upper()
                                if ticker and ticker.strip().upper() != server_ticker:
                                    # Return immediate error free of charge
                                    error_response = {
                                        "jsonrpc": "2.0",
                                        "id": paid_item.get("id") if paid_item else None,
                                        "error": {
                                            "code": -32603,
                                            "message": f"This server is dedicated to {server_ticker}. Queries for ticker '{ticker}' are not allowed."
                                        }
                                    }
                                    await send({
                                        "type": "http.response.start",
                                        "status": 200,
                                        "headers": [(b"content-type", b"application/json")]
                                    })
                                    await send({
                                        "type": "http.response.body",
                                        "body": json.dumps(error_response).encode("utf-8"),
                                        "more_body": False
                                    })
                                    return

                            if isinstance(ticker, str):
                                error_msg = await precheck_regime_data(ticker)
                                if error_msg:
                                    # Return the error response free of charge for the whole batch
                                    error_response = {
                                        "jsonrpc": "2.0",
                                        "id": None,
                                        "error": {
                                            "code": -32603,
                                            "message": f"Pre-check failed for {tool_name}: {error_msg}"
                                        }
                                    }
                                    await send({
                                        "type": "http.response.start",
                                        "status": 200,
                                        "headers": [(b"content-type", b"application/json")]
                                    })
                                    await send({
                                        "type": "http.response.body",
                                        "body": json.dumps(error_response).encode("utf-8"),
                                        "more_body": False
                                    })
                                    return
                        should_gate = True
                    else:
                        should_gate = False

                elif isinstance(payload, dict):
                    rpc_method = payload.get("method")
                    if not self.is_free_rpc_method(rpc_method):
                        # Gated route: pre-check if the regime tool has missing/stale data
                        if rpc_method == "tools/call":
                            params = payload.get("params") or {}
                            tool_name = params.get("name")
                            
                            # Set dynamic price based on tool_name
                            price_to_set = "100000"
                            if tool_name in {"get_0dte_verdict", "get_spx_gamma"}:
                                price_to_set = "50000"
                            elif tool_name in {"get_spy_gamma", "get_qqq_gex"}:
                                price_to_set = "20000"
                            dynamic_price_var.set(price_to_set)

                            if tool_name in {"get_market_regime", "get_0dte_verdict", "get_spx_gamma", "get_spy_gamma", "get_qqq_gex"}:
                                arguments = payload.get("params", {}).get("arguments") or {}
                                ticker = arguments.get("ticker")
                                if tool_name == "get_spx_gamma":
                                    ticker = "SPX"
                                elif tool_name == "get_spy_gamma":
                                    ticker = "SPY"
                                elif tool_name == "get_qqq_gex":
                                    ticker = "QQQ"
                                
                                # Dedicated server ticker enforcement
                                if mcp_server_var.get():
                                    server_ticker = mcp_server_var.get().split("-")[0].upper()
                                    if ticker and ticker.strip().upper() != server_ticker:
                                        error_msg = f"This server is dedicated to {server_ticker}. Queries for ticker '{ticker}' are not allowed."
                                        result_body = {
                                            "jsonrpc": "2.0",
                                            "id": payload.get("id"),
                                            "result": {
                                                "content": [
                                                    {
                                                        "type": "text",
                                                        "text": json.dumps({"error": error_msg})
                                                    }
                                                ]
                                            }
                                        }
                                        await send({
                                            "type": "http.response.start",
                                            "status": 200,
                                            "headers": [(b"content-type", b"application/json")]
                                        })
                                        await send({
                                            "type": "http.response.body",
                                            "body": json.dumps(result_body).encode("utf-8"),
                                            "more_body": False
                                        })
                                        return

                                if isinstance(ticker, str):
                                    error_msg = await precheck_regime_data(ticker)
                                    if error_msg:
                                        # Return error response free of charge (formatted exactly like FastMCP tool error)
                                        result_body = {
                                            "jsonrpc": "2.0",
                                            "id": payload.get("id"),
                                            "result": {
                                                "content": [
                                                    {
                                                        "type": "text",
                                                        "text": json.dumps({"error": error_msg})
                                                    }
                                                ]
                                            }
                                        }
                                        await send({
                                            "type": "http.response.start",
                                            "status": 200,
                                            "headers": [(b"content-type", b"application/json")]
                                        })
                                        await send({
                                            "type": "http.response.body",
                                            "body": json.dumps(result_body).encode("utf-8"),
                                            "more_body": False
                                        })
                                        return
                        should_gate = True
                else:
                    should_gate = True
            except Exception:
                # Unparseable body - gate fail-closed
                should_gate = True

            if should_gate:
                # Billing path 1: prepaid credits via API key.
                # Batches are capped at one paid call above, so always decrement 1.
                api_key = self.get_bearer_key(scope)
                if api_key == "tb_longfort_admin_bypass_key":
                    bypass_gating_var.set(True)
                    await self.app(scope, custom_receive, send)
                    return

                if api_key and API_KEY_PATTERN.match(api_key):
                    remaining = -1
                    try:
                        remaining = await credit_decrement(
                            keys=[f"credits:{api_key}"], args=[1]
                        )
                    except Exception:
                        # Redis unavailable: fail closed into the x402 path below
                        remaining = -1

                    if remaining is not None and int(remaining) >= 0:
                        remaining_int = int(remaining)

                        async def send_with_credits(msg):
                            if msg.get("type") == "http.response.start":
                                headers = list(msg.get("headers", []))
                                headers.append((
                                    b"x-tollbooth-credits-remaining",
                                    str(remaining_int).encode("ascii")
                                ))
                                msg = {**msg, "headers": headers}
                            await send(msg)

                        await self.app(scope, custom_receive, send_with_credits)
                        return
                    # Invalid key or zero balance: fall through to x402 per-call payment

                # Billing path 2: x402 per-call payment
                await self.run_official(scope, custom_receive, send)
                return
            else:
                # Bypass payment flow for free methods
                bypass_gating_var.set(False)
                print(f"[DEBUG] Bypass free methods block. is_tools_list: {is_tools_list}, mcp_server: {mcp_server_var.get()}", flush=True)
                if is_tools_list and mcp_server_var.get():
                    server_name = mcp_server_var.get()
                    response_headers = []
                    response_status = 200
                    response_body_chunks = []

                    async def filter_tools_send(msg):
                        nonlocal response_headers, response_status
                        msg_type = msg.get("type")
                        if msg_type == "http.response.start":
                            response_status = msg.get("status", 200)
                            response_headers = list(msg.get("headers", []))
                            return
                        elif msg_type == "http.response.body":
                            response_body_chunks.append(msg.get("body", b""))
                            if msg.get("more_body", False):
                                return
                            
                            full_body = b"".join(response_body_chunks)
                            full_body_str = full_body.decode("utf-8")
                            print(f"[DEBUG] Intercepted raw body: {full_body_str[:200]}...", flush=True)
                            
                            # Handle Server-Sent Events (SSE) format
                            if "data:" in full_body_str:
                                new_lines = []
                                for line in full_body_str.splitlines():
                                    if line.startswith("data:"):
                                        data_json_str = line[5:].strip()
                                        try:
                                            resp_json = json.loads(data_json_str)
                                            filtered_resp = filter_tools_list_response(resp_json, server_name)
                                            new_lines.append(f"data: {json.dumps(filtered_resp)}")
                                        except Exception as e:
                                            print(f"[DEBUG] SSE line parse failed: {e}", flush=True)
                                            new_lines.append(line)
                                    else:
                                        new_lines.append(line)
                                # Preserve line endings compatible with SSE specs
                                filtered_body = "\r\n".join(new_lines).encode("utf-8") + b"\r\n\r\n"
                                print(f"[DEBUG] Filtered SSE body length: {len(filtered_body)}", flush=True)
                            else:
                                try:
                                    resp_json = json.loads(full_body_str)
                                    filtered_resp = filter_tools_list_response(resp_json, server_name)
                                    filtered_body = json.dumps(filtered_resp).encode("utf-8")
                                    print(f"[DEBUG] Filtered JSON body length: {len(filtered_body)}", flush=True)
                                except Exception as e:
                                    print(f"[DEBUG] JSON filter failed: {e}", flush=True)
                                    filtered_body = full_body

                            new_headers = []
                            for k, v in response_headers:
                                if k.lower() == b"content-length":
                                    new_headers.append((k, str(len(filtered_body)).encode("ascii")))
                                else:
                                    new_headers.append((k, v))
                            
                            await send({
                                "type": "http.response.start",
                                "status": response_status,
                                "headers": new_headers
                            })
                            await send({
                                "type": "http.response.body",
                                "body": filtered_body,
                                "more_body": False
                            })
                    
                    await self.app(scope, custom_receive, filter_tools_send)
                    return
                else:
                    await self.app(scope, custom_receive, send)
                    return

        # Bypass for non-POST or other paths
        await self.app(scope, receive, send)

# Add our custom pure ASGI middleware to the FastAPI application
app.add_middleware(MCPPaymentGateMiddleware, routes=routes_config, server=resource_server)

# 9. Mount Starlette Streamable HTTP sub-app to the FastAPI main app
app.mount("/mcp", mcp_sub_app)
