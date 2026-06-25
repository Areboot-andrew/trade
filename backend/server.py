import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import asyncio
import logging
import websockets as ws_client
from decimal import Decimal
import pandas as pd
import os

CONFIG_CACHE_PATH = "bot_config_cache.json"

def save_bot_config_json(all_configs: dict):
    try:
        with open(CONFIG_CACHE_PATH, "w") as f:
            json.dump(all_configs, f, indent=4)
        logger.info(f"Config saved to {CONFIG_CACHE_PATH}")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

def load_bot_config_json() -> dict:
    if os.path.exists(CONFIG_CACHE_PATH):
        try:
            with open(CONFIG_CACHE_PATH, "r") as f:
                data = json.load(f)
                if "leverage" in data:
                    return {"BTCUSDT": data}
                return data
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
    return {}

# Імпортуємо локальні модулі
from config_manager import load_and_apply_profile_settings, load_asset_precisions, get_default_advanced_settings
from market_analyzer_module import MarketAnalyzer
from grid_generator_module import GridGenerator
from binance_live_handler import BinanceLiveHandler
from main_live_trading_manager import LiveTradingManager
from binance.client import Client as BinanceClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Trading Bot API - Dual Direction")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

main_loop = None

class MockGuiQueue:
    def put(self, item):
        action, data = item
        if action not in ["update_open_orders_display", "update_positions_display", "update_trade_history_display", "symbol_data_status", "update_live_managed_grid_orders_display", "live_bot_status_update_display"]:
            logger.info(f"GUI Event: {action} - {str(data)[:100]}")
            
        if action == "update_positions_display":
            # Sync PNL to bot_ctx for legacy managers (Both sides)
            positions = data.get("positions", [])
            
            # Keep track of found sides to reset the ones that are NOT in the list
            found_sides = {"LONG": False, "SHORT": False}
            
            for p in positions:
                if p.get("symbol") == STATE["symbol"]:
                    try:
                        side = str(p.get("position_side", "BOTH")).upper()
                        if side == "BOTH": continue # Ignore one-way for now as we focus on hedge
                        
                        user_lev = float(STATE["bot_config"].get("leverage", 20))
                        amt = float(p.get("amount", 0))
                        pnl_usd = float(p.get("pnl_usd", 0))
                        entry = float(p.get("entry_price", 0))
                        
                        margin = abs(amt) * entry / user_lev
                        pnl_pct = (pnl_usd / margin * 100) if margin > 0 else 0
                        pnl_str = f"{pnl_usd} ({pnl_pct:.2f} %)"
                        
                        if side == "LONG":
                            bot_ctx.live_long_pos_amt.val = str(abs(amt))
                            bot_ctx.live_long_pos_entry.val = str(entry)
                            bot_ctx.live_long_pos_pnl.val = pnl_str
                            found_sides["LONG"] = True
                        elif side == "SHORT":
                            bot_ctx.live_short_pos_amt.val = str(abs(amt))
                            bot_ctx.live_short_pos_entry.val = str(entry)
                            bot_ctx.live_short_pos_pnl.val = pnl_str
                            found_sides["SHORT"] = True
                    except Exception as e:
                        logger.error(f"Error syncing PNL to bot_ctx for {side}: {e}")
            
            # Reset sides that were not found in the current position update
            if not found_sides["LONG"]:
                bot_ctx.live_long_pos_amt.val = "0"
                bot_ctx.live_long_pos_entry.val = "0"
                bot_ctx.live_long_pos_pnl.val = "0.0 (0.0 %)"
            if not found_sides["SHORT"]:
                bot_ctx.live_short_pos_amt.val = "0"
                bot_ctx.live_short_pos_entry.val = "0"
                bot_ctx.live_short_pos_pnl.val = "0.0 (0.0 %)"

        if main_loop and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                manager.broadcast_message({"type": "gui_event", "action": action, "data": data}),
                main_loop
            )

class DummyVar:
    def __init__(self, val=""): self.val = val
    def get(self): return self.val
    
class BotContext:
    def __init__(self):
        self.selected_symbol = "BTCUSDT"
        self.currency_var = DummyVar("BTCUSDT")
        self.live_grid_position_type_var = DummyVar("Long")
        self.asset_precisions_data = load_asset_precisions()
        self.current_price_velocity_percent = Decimal(0)
        self.public_binance_client = BinanceClient(None, None)
        
        # Simulation/State bridge attributes for GridGenerator
        self.sim_orders_data = []
        self.sim_pending_orders_margin = Decimal(0)
        self.sim_total_input_margin = Decimal(0)
        self.sim_avg_entry_price = Decimal(0)
        self.sim_total_invested_margin_usd = Decimal(0)
        self.sim_total_base_amount = Decimal(0)
        self.sim_pos_size_base = Decimal(0)
        self.sim_pos_type = "Long"
        self._initial_capital_for_sim_set_value = Decimal(0)
        self.sim_cumulative_realized_pnl_plus = Decimal(0)
        self.sim_cumulative_realized_pnl_minus = Decimal(0)
        self.sim_total_realized_pnl_from_closing_chunks = Decimal(0)
        
        # Dummy Input Vars for GUI compatibility in legacy modules
        self.sim_initial_group_margin_input_var = DummyVar("20")
        self.sim_margin_increase_factor_input_var = DummyVar("1.2")
        self.sim_max_total_active_groups_input_var = DummyVar("6")
        self.sim_cluster_orders_count_input_var = DummyVar("3")
        self.sim_atr_multiplier_cluster_spread_input_var = DummyVar("0.3")
        self.sim_leverage_input_var = DummyVar("20")
        self.sim_total_capital_limit_var = DummyVar("1000")
        self.commission_entry = DummyVar("0.0004")
        self.current_price_var = DummyVar("0")
        self.position_type_var = DummyVar("Long")
        
        # Live position dummy vars (Dual-Side)
        self.live_long_pos_amt = DummyVar("0")
        self.live_long_pos_entry = DummyVar("0")
        self.live_long_pos_pnl = DummyVar("0.0 (0.0 %)")
        
        self.live_short_pos_amt = DummyVar("0")
        self.live_short_pos_entry = DummyVar("0")
        self.live_short_pos_pnl = DummyVar("0.0 (0.0 %)")

        # Legacy backward compatibility (maps to Long by default or becomes obsolete)
        self.live_current_position_pnl_var_value = self.live_long_pos_pnl

        # Scheduler
        self.recalc_interval_hours = Decimal("8")
        
        # Load profile
        self.active_strategy_settings = load_and_apply_profile_settings()
        if not self.active_strategy_settings:
            self.active_strategy_settings = get_default_advanced_settings()
        
        # Bridge attributes for LiveGridExecutionManager and GridGenerator
        self.live_bot_state = {"orders_data_virtual": []}
        self.gui_queue = MockGuiQueue()
        self.live_bot_active = False 
        self.live_bot_params = {
            "leverage": Decimal("20"), 
            "margin_multiplier": Decimal("1.2"), 
            "initial_group_margin": Decimal("20"),
            "recalc_interval_hours": Decimal("8")
        }
        self._group_id_counter = 1
        self._order_id_counter = 1
        
        # Instances (will be linked after creation)
        self.market_analyzer = None
        self.grid_generator = None
        self.live_trading_manager = None

    def after(self, ms, func, *args):
        # Dummy for tkinter compatibility
        return "timer_id"

    def after_cancel(self, timer_id):
        # Dummy for tkinter compatibility
        pass

    def log_message_to_file(self, msg, log_type="app_log"):
        logger.info(f"[{log_type}] {msg}")

    @property
    def current_symbol_precision_info(self):
        return self.asset_precisions_data.get(self.selected_symbol, {})

    def set_active_profile(self, profile_name):
        try:
            logger.info(f"Setting active profile to: {profile_name}")
            self.active_strategy_settings = load_and_apply_profile_settings(profile_name)
            # Sync to dummy vars if needed
            self.sim_initial_group_margin_input_var.val = str(self.active_strategy_settings.get("general_grid_settings", {}).get("initial_group_margin_usd", "20"))
            return True
        except Exception as e:
            logger.error(f"Failed to set profile {profile_name}: {e}")
            return False

    def sim_next_group_id_num(self):
        val = self._group_id_counter
        self._group_id_counter += 1
        return val

    def sim_next_order_id_num(self):
        val = self._order_id_counter
        self._order_id_counter += 1
        return val
            
    def add_sim_log(self, msg, level="INFO"):
        logger.debug(f"BOT LOG: {msg}")
        
    def get_actual_live_leverage(self):
        return self.live_bot_params.get("leverage", Decimal("20"))

    def get_tf_id_by_common_name(self, name):
        # name typically "M1", "M5", "M15", "H1", "H4", "D1"
        configs = self.active_strategy_settings.get("grid_timeframe_escalation", {}).get("timeframes_config", [])
        # Search in config first
        for c in configs:
            if name.lower() in c.get("timeframe_id", "").lower() or name.lower() in c.get("binance_interval_notation", "").lower():
                return c.get("timeframe_id")
        # Fallback mapping
        mapping = {"M1": "M1_Base", "M5": "M5_Base", "M15": "M15_Base", "H1": "H1_Base", "H4": "H4_Base", "D1": "D1_Base"}
        return mapping.get(name, "M15_default")

    def get_tf_id_by_binance_interval(self, interval):
        configs = self.active_strategy_settings.get("grid_timeframe_escalation", {}).get("timeframes_config", [])
        for c in configs:
            if c.get("binance_interval_notation") == interval:
                return c.get("timeframe_id")
        return "M15_default"

    def _get_tf_config_by_id(self, tf_id):
        configs = self.active_strategy_settings.get("grid_timeframe_escalation", {}).get("timeframes_config", [])
        for c in configs:
            if isinstance(c, dict) and c.get("timeframe_id") == tf_id: return c
        
        # Extended search for Fibonacci source TFs and others
        if tf_id == "M15_default" and configs: return configs[0]
        return None

bot_ctx = BotContext()

# Ініціалізуємо старі менеджери для Live торгівлі
market_analyzer = MarketAnalyzer(bot_ctx.active_strategy_settings, app_instance=bot_ctx)
grid_generator = GridGenerator(market_analyzer, app_instance=bot_ctx, strategy_settings=bot_ctx.active_strategy_settings)
binance_handler = BinanceLiveHandler(bot_ctx.gui_queue, bot_ctx)
live_manager = LiveTradingManager(bot_ctx, binance_handler, grid_generator)

# Link instances to bot_ctx
bot_ctx.market_analyzer = market_analyzer
bot_ctx.grid_generator = grid_generator
bot_ctx.live_trading_manager = live_manager

# Завантажуємо правила (ТП рівні тощо)
live_manager.load_live_trading_rules()

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_message(self, message: dict):
        disconnected = []
        # Serialize with default=str to handle Decimal/datetime, then re-parse to get native types
        safe_text = json.dumps(message, default=str)
        for connection in self.active_connections:
            try:
                await connection.send_text(safe_text)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()
STATE = {
    "symbol": bot_ctx.selected_symbol,
    "price": 0.0,
    "long_grid": [],
    "short_grid": [],
    "bot_grid": [],
    "bot_grid_position_type": "Long",
    "bot_active": False,
    "available_symbols": list(bot_ctx.asset_precisions_data.keys()),
    "all_configs": {},
    "bot_config": {
        "leverage": "20",
        "initial_margin": "20",
        "margin_multiplier": "1.2",
        "cluster_count": "3",
        "capital_limit": "300",
        "min_stake": "1.1"
    }
}

# Overwrite default STATE with cached config if available
cached_configs = load_bot_config_json()
if cached_configs:
    STATE["all_configs"] = cached_configs
    sym = STATE["symbol"]
    if sym in cached_configs:
        STATE["bot_config"].update(cached_configs[sym])

# Sync initial state to bot_ctx
bot_ctx.selected_symbol = STATE["symbol"]
bot_ctx.sim_leverage_input_var.val = STATE["bot_config"]["leverage"]
bot_ctx.sim_initial_group_margin_input_var.val = STATE["bot_config"]["initial_margin"]
bot_ctx.sim_margin_increase_factor_input_var.val = STATE["bot_config"]["margin_multiplier"]
bot_ctx.sim_cluster_orders_count_input_var.val = STATE["bot_config"]["cluster_count"]
bot_ctx.sim_total_capital_limit_var.val = STATE["bot_config"]["capital_limit"]
bot_ctx.active_strategy_settings.setdefault("dynamic_margin_logic", {})["min_margin_per_order_usd"] = float(STATE["bot_config"]["min_stake"])

def clean_decimal_orders(orders: list) -> list:
    cleaned = []
    if not orders: return cleaned
    for order in orders:
        if not isinstance(order, dict): continue
        cleaned_order = {}
        for k, v in order.items():
            if isinstance(v, Decimal): cleaned_order[k] = float(v)
            else: cleaned_order[k] = v
        if 'base_amount' in cleaned_order:
             cleaned_order['amount'] = abs(cleaned_order['base_amount'])
        cleaned.append(cleaned_order)
    return cleaned

async def sync_market_data(symbol: str):
    # Detect all needed timeframes from strategy
    needed_tfs = []
    # Main escalation TFs
    escalation_configs = bot_ctx.active_strategy_settings.get("grid_timeframe_escalation", {}).get("timeframes_config", [])
    for c in escalation_configs:
        needed_tfs.append((c.get("timeframe_id"), c.get("binance_interval_notation", "15m")))
    
    # Fibonacci TFs
    fibo_tfs = bot_ctx.active_strategy_settings.get("level_sourcing_and_processing", {}).get("fibonacci_levels", {}).get("source_timeframes", [])
    for tf_id in fibo_tfs:
        interval = "15m"
        for c in escalation_configs:
            if c.get("timeframe_id") == tf_id:
                interval = c.get("binance_interval_notation")
                break
        if (tf_id, interval) not in needed_tfs:
            needed_tfs.append((tf_id, interval))

    if not needed_tfs:
        needed_tfs = [("M15_default", "15m")]

    logger.info(f"Starting multi-timeframe data fetch for {symbol}... TFs: {[t[0] for t in needed_tfs]}")
    for tf_id, interval in needed_tfs:
        try:
            lookback_map = {
                "1m": "12 hours ago UTC", "5m": "2 days ago UTC", "15m": "5 days ago UTC",
                "1h": "20 days ago UTC", "4h": "80 days ago UTC", "1d": "300 days ago UTC"
            }
            lookback = lookback_map.get(interval, "5 days ago UTC")
            
            klines = await asyncio.to_thread(bot_ctx.public_binance_client.get_historical_klines, symbol, interval, lookback)
            if not klines:
                logger.warning(f"  No klines received for {tf_id} ({interval})")
                continue
                
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
            df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            
            market_analyzer.current_symbol_klines_dfs[tf_id] = df
            logger.info(f"  Fetched {len(df)} candles for {tf_id} ({interval})")
        except Exception as e:
            logger.error(f"  Error fetching {tf_id} ({interval}): {e}")
    
    try:
        market_analyzer.analyze_symbol_all_configured_tfs(symbol)
        logger.info(f"Analysis complete for {symbol}.")
    except Exception as e:
        logger.error(f"Market analysis error: {e}")

async def binance_price_streamer():
    import time
    last_broadcast_time = 0
    current_stream_symbol = STATE['symbol']
    uri = f"wss://stream.binance.com:9443/ws/{current_stream_symbol.lower()}@ticker"
    while True:
        try:
            async with ws_client.connect(uri) as websocket:
                while True:
                    if STATE['symbol'] != current_stream_symbol:
                        current_stream_symbol = STATE['symbol']
                        uri = f"wss://stream.binance.com:9443/ws/{current_stream_symbol.lower()}@ticker"
                        break 
                        
                    msg = await websocket.recv()
                    data = json.loads(msg)
                    current_price = Decimal(data['c'])
                    STATE['price'] = float(current_price)
                    
                    current_time = time.time()
                    if manager.active_connections:
                        # Get ready TFs for UI info
                        ready_tfs = [tf for tf, res in market_analyzer.analysis_results_by_tf.items() if not res.get("error")]
                        asyncio.create_task(manager.broadcast_message({
                            "type": "market_update",
                            "symbol": STATE["symbol"],
                            "price": STATE["price"],
                            "long_grid": STATE["long_grid"],
                            "short_grid": STATE["short_grid"],
                            "ready_tfs": ready_tfs
                        }))
        except Exception as e:
            logger.error(f"Binance Price Streamer Error: {e}")
            await asyncio.sleep(5)

# REST Ендпоінти для Лайв Торгівлі (Замінюють кнопки GUI)
class ConnectPayload(BaseModel):
    api_key: str = ""
    api_secret: str = ""

@app.post("/api/connect")
async def connect_binance(payload: ConnectPayload = None):
    try:
        api_key = payload.api_key if payload else ""
        api_secret = payload.api_secret if payload else ""
        
        if api_key and api_secret:
            with open("binance_api_keys.json", "w") as f:
                json.dump({"api_key": api_key, "api_secret": api_secret}, f)
        else:
            try:
                with open("binance_api_keys.json", "r") as f:
                    keys = json.load(f)
                    api_key = keys.get("api_key", "")
                    api_secret = keys.get("api_secret", "")
            except Exception:
                pass
                
        if api_key and api_secret:
            binance_handler.connect(api_key, api_secret)
            return {"status": "connecting"}
        else:
            return {"status": "error", "message": "API Keys are missing. Please enter them in the UI."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/disconnect")
async def disconnect_binance():
    try:
        binance_handler.disconnect()
        return {"status": "disconnected"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- Profile Management ---
class SetProfileRequest(BaseModel):
    profile_name: str

@app.post("/api/set_profile")
async def set_profile(req: SetProfileRequest):
    success = bot_ctx.set_active_profile(req.profile_name)
    if success:
        # Re-initialize modules with new settings
        market_analyzer.strategy_settings = bot_ctx.active_strategy_settings
        grid_generator.strategy_settings = bot_ctx.active_strategy_settings
        # Trigger re-fetch for new possible timeframes
        return {"status": "profile_set", "profile": req.profile_name}
    else:
        return {"status": "error", "message": f"Could not load profile {req.profile_name}"}

# --- Розрахунок Гріду Бота ---
class CalculateGridRequest(BaseModel):
    position_type: str = "Long"

@app.post("/api/clear_grids")
async def clear_grids():
    STATE['long_grid'] = []
    STATE['short_grid'] = []
    # Sync to bot_ctx legacy fields
    bot_ctx.sim_orders_data = []
    return {"status": "cleared"}

async def internal_calculate_grid():
    """Централізована логіка перерахунку сітки для API та планувальника."""
    # Визначаємо клієнт для запиту даних (якщо не підключено - використовуємо публічний)
    client_to_use = binance_handler.client if binance_handler.connected else bot_ctx.public_binance_client
    
    symbol = STATE["symbol"]
    if not symbol:
        raise Exception("Символ не вибрано")

    logger.info(f"Starting calculation for {symbol}... (Connected: {binance_handler.connected})")
    
    # 1. Fetch analysis
    logger.info(f"Starting multi-timeframe data fetch for {symbol}...")
    
    # Визначаємо необхідні ТФ
    needed_tf_ids = set()
    gte_conf = bot_ctx.active_strategy_settings.get("grid_timeframe_escalation", {})
    for tf_cfg in gte_conf.get("timeframes_config", []):
        if tf_cfg.get("timeframe_id"):
            needed_tf_ids.add(tf_cfg["timeframe_id"])
    
    if not needed_tf_ids:
        needed_tf_ids.add("M15_Base") # Fallback
        
    all_dfs = {}
    for tf_id in needed_tf_ids:
        tf_cfg = bot_ctx._get_tf_config_by_id(tf_id)
        if not tf_cfg: continue
        
        interval = tf_cfg.get("binance_interval_notation", "15m")
        # Fetching enough for analysis
        try:
            # Використовуємо client_to_use
            raw_klines = await asyncio.to_thread(client_to_use.futures_klines, symbol=symbol, interval=interval, limit=480)
            if raw_klines:
                df = pd.DataFrame(raw_klines, columns=[
                    'timestamp', 'open', 'high', 'low', 'close', 'volume',
                    'close_time', 'quote_asset_volume', 'number_of_trades',
                    'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
                ])
                df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric, errors='coerce')
                all_dfs[tf_id] = df
                logger.info(f"  Fetched {len(df)} candles for {tf_id} ({interval})")
        except Exception as e:
            logger.error(f"  Error fetching {tf_id}: {e}")

    if not all_dfs:
        raise Exception("Не вдалося завантажити дані для аналізу")

    market_analyzer.current_symbol_klines_dfs = all_dfs
    analysis_results = market_analyzer.analyze_symbol_all_configured_tfs(symbol)
    
    # Визначаємо поточну ціну (з останньої свічки базового ТФ)
    base_tf_id = list(needed_tf_ids)[0]
    current_price = Decimal(str(all_dfs[base_tf_id]['close'].iloc[-1]))
    logger.info(f"Analysis complete for {symbol}. Current price: {current_price}")
    
    analysis = {
        "current_price": current_price,
        "bundle": analysis_results
    }
    
    # 2. Get config
    lev = Decimal(STATE["bot_config"]["leverage"])
    margin = Decimal(STATE["bot_config"]["initial_margin"])
    mult = Decimal(STATE["bot_config"]["margin_multiplier"])
    cluster_count = STATE["bot_config"]["cluster_count"]
    capital_limit = STATE["bot_config"]["capital_limit"]
    min_stake = STATE["bot_config"]["min_stake"]
    
    current_price = Decimal(str(analysis["current_price"]))
    
    def reset_bot_ctx_for_calc():
        bot_ctx.sim_pending_orders_margin = Decimal(0)
        bot_ctx.sim_total_input_margin = Decimal(0)
        bot_ctx.sim_total_base_amount = Decimal(0)
        bot_ctx.sim_cumulative_realized_pnl_plus = Decimal(0)
        bot_ctx.sim_cumulative_realized_pnl_minus = Decimal(0)
        bot_ctx.sim_total_realized_pnl_from_closing_chunks = Decimal(0)
        bot_ctx.sim_orders_data = []
        bot_ctx._group_id_counter = 1
        bot_ctx._order_id_counter = 1
        # Sync variables for the generator/limit check
        bot_ctx.sim_total_capital_limit_var.val = str(capital_limit)
        bot_ctx.sim_cluster_orders_count_input_var.val = str(cluster_count)
        bot_ctx.active_strategy_settings.setdefault("dynamic_margin_logic", {})["min_margin_per_order_usd"] = float(min_stake)

    # 3. Handle Long
    reset_bot_ctx_for_calc()
    long_raw = bot_ctx.grid_generator.generate_dynamic_grid(
        current_market_price=current_price,
        position_type="Long",
        leverage=lev,
        initial_overall_margin=margin,
        margin_increase_factor_from_sim_logic=mult,
        symbol=symbol,
        current_pnl_percent=Decimal(0)
    )
    
    # 4. Handle Short
    reset_bot_ctx_for_calc()
    short_raw = bot_ctx.grid_generator.generate_dynamic_grid(
        current_market_price=current_price,
        position_type="Short",
        leverage=lev,
        initial_overall_margin=margin,
        margin_increase_factor_from_sim_logic=mult,
        symbol=symbol,
        current_pnl_percent=Decimal(0)
    )
    
    STATE['long_grid'] = clean_decimal_orders(long_raw)
    STATE['short_grid'] = clean_decimal_orders(short_raw)
    
    # [NEW] Sync to live_bot_state for Execution Manager (Dual Side)
    # The manager expects orders to have 'pending_live_bot' status to track them
    virtual_orders = []
    for o in STATE['long_grid']:
        o_copy = o.copy()
        o_copy['status'] = 'pending_live_bot'
        o_copy['position_side'] = 'LONG'
        virtual_orders.append(o_copy)
    for o in STATE['short_grid']:
        o_copy = o.copy()
        o_copy['status'] = 'pending_live_bot'
        o_copy['position_side'] = 'SHORT'
        virtual_orders.append(o_copy)
    
    bot_ctx.live_bot_state['orders_data_virtual'] = virtual_orders
    
    logger.info(f"[GRID CALC RESULT] Long: {len(STATE['long_grid'])} orders, Short: {len(STATE['short_grid'])} orders synchronized to virtual_state.")
    
    if bot_ctx.live_trading_manager:
        bot_ctx.live_trading_manager.grid_execution_manager._log_to_gui(f"Авто-перерахунок завершено ({symbol}).", "SUCCESS")
    
    return {
        "status": "success",
        "long_grid": STATE['long_grid'],
        "short_grid": STATE['short_grid']
    }

@app.post("/api/calculate_grid")
async def calculate_grid_endpoint(req: CalculateGridRequest):
    try:
        return await internal_calculate_grid()
    except Exception as e:
        import traceback
        logger.error(f"[GRID CALC] EXCEPTION: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(e), "trace": traceback.format_exc()}

@app.post("/api/start_trading")
async def start_trading():
    if not STATE['long_grid'] and not STATE['short_grid']:
        return {"status": "error", "message": "Спочатку розрахуйте сітку (Calculate Grid)"}
    STATE['bot_active'] = True
    # Передаємо розраховану сітку у live_manager
    live_manager.activate_live_trading()
    return {"status": "started", "position_type": "Both"}

@app.post("/api/stop_trading")
async def stop_trading():
    STATE['bot_active'] = False
    live_manager.deactivate_live_trading()
    return {"status": "stopped"}

# --- Ручне розміщення ордерів ---
class PlaceOrderRequest(BaseModel):
    symbol: str = ""
    side: str = "BUY"
    order_type: str = "LIMIT"
    quantity: str = "0"
    price: str = "0"
    position_side: str = "LONG"

@app.post("/api/place_order")
async def place_order(req: PlaceOrderRequest):
    if not binance_handler.connected:
        return {"status": "error", "message": "Не підключено до Binance"}
    symbol = req.symbol or STATE["symbol"]
    try:
        binance_handler.place_order(
            symbol=symbol,
            side=req.side.upper(),
            order_type=req.order_type.upper(),
            quantity_str=req.quantity,
            price_str=req.price if req.order_type.upper() == "LIMIT" else None,
            reduce_only_gui=False,
            position_side_gui=req.position_side.upper()
        )
        return {"status": "order_sent"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- Скасування ордерів ---
class CancelOrderRequest(BaseModel):
    symbol: str = ""
    order_id: str = ""

@app.post("/api/cancel_order")
async def cancel_order(req: CancelOrderRequest):
    if not binance_handler.connected:
        return {"status": "error", "message": "Не підключено до Binance"}
    symbol = req.symbol or STATE["symbol"]
    try:
        binance_handler.cancel_order(symbol, req.order_id)
        return {"status": "cancel_sent"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

class CancelAllOrdersRequest(BaseModel):
    symbol: str = ""

@app.post("/api/cancel_all_orders")
async def cancel_all_orders(req: CancelAllOrdersRequest):
    if not binance_handler.connected:
        return {"status": "error", "message": "Не підключено до Binance"}
    symbol = req.symbol or STATE["symbol"]
    try:
        binance_handler.cancel_all_orders(symbol)
        return {"status": "cancel_all_sent"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- Закриття позиції по ринку ---
class ClosePositionRequest(BaseModel):
    symbol: str = ""
    position_side: str = "LONG"

@app.post("/api/close_position_market")
async def close_position_market(req: ClosePositionRequest):
    if not binance_handler.connected:
        return {"status": "error", "message": "Не підключено до Binance"}
    symbol = req.symbol or STATE["symbol"]
    try:
        positions_info = binance_handler.client.futures_position_information(symbol=symbol)
        target_pos = None
        for p in positions_info:
            if p.get("positionSide", "").upper() == req.position_side.upper():
                amt = Decimal(p.get("positionAmt", "0"))
                if amt != Decimal(0):
                    target_pos = p
                    break
        if not target_pos:
            return {"status": "error", "message": f"Немає відкритої {req.position_side} позиції"}
        pos_amt = abs(Decimal(target_pos["positionAmt"]))
        close_side = "SELL" if req.position_side.upper() == "LONG" else "BUY"
        binance_handler.place_order(
            symbol=symbol, side=close_side, order_type="MARKET",
            quantity_str=str(pos_amt), price_str=None,
            reduce_only_gui=True, position_side_gui=req.position_side.upper()
        )
        return {"status": "close_sent"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- Тейк-Профіт Правила ---
class TpLevel(BaseModel):
    pnl_threshold_percent: str = "0"
    close_percent_of_pos: str = "0"

class ApplyTpRulesRequest(BaseModel):
    levels: list[TpLevel] = []

@app.post("/api/apply_tp_rules")
async def apply_tp_rules(req: ApplyTpRulesRequest):
    try:
        gui_data = []
        for level in req.levels:
            gui_data.append({
                "pnl_threshold_percent": level.pnl_threshold_percent,
                "close_percent_of_pos": level.close_percent_of_pos
            })
        live_manager.update_live_trading_rules_from_gui(gui_data)
        
        # Save for persistence
        with open("live_trading_rules.json", "w") as f:
            json.dump({"partial_tp_levels": gui_data}, f, indent=4)
            
        return {"status": "tp_rules_applied", "levels_count": len(gui_data)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/shutdown")
async def shutdown_server():
    logger.info("Shutdown requested from UI...")
    # Delay exit to allow response to send
    async def gradual_shutdown():
        await asyncio.sleep(1)
        import os
        os._exit(0)
    asyncio.create_task(gradual_shutdown())
    return {"status": "shutting_down"}


@app.websocket("/ws/stream")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_text(json.dumps({
            "type": "market_update",
            "symbol": STATE["symbol"],
            "price": STATE["price"],
            "available_symbols": STATE["available_symbols"],
            "bot_config": STATE["bot_config"],
            "long_grid": STATE["long_grid"],
            "short_grid": STATE["short_grid"],
            "precisions": bot_ctx.asset_precisions_data
        }, default=str))
        while True:
            data = await websocket.receive_text()
            data = json.loads(data)
            if data.get("type") == "change_symbol":
                new_symbol = data.get("symbol", STATE["symbol"])
                STATE["symbol"] = new_symbol
                bot_ctx.selected_symbol = new_symbol
                binance_handler.set_active_symbol(new_symbol)
                
                # Load config for new symbol
                if new_symbol in STATE["all_configs"]:
                    STATE["bot_config"].update(STATE["all_configs"][new_symbol])
                else:
                    # Reset to defaults for a new symbol if not seen before
                    STATE["bot_config"].update({
                        "leverage": "20",
                        "initial_margin": "20",
                        "margin_multiplier": "1.2",
                        "cluster_count": "3",
                        "capital_limit": "300",
                        "min_stake": "1.1"
                    })
                
                bot_ctx.live_bot_params["leverage"] = Decimal(STATE["bot_config"]["leverage"])
                bot_ctx.live_bot_params["initial_group_margin"] = Decimal(STATE["bot_config"]["initial_margin"])
                bot_ctx.live_bot_params["margin_multiplier"] = Decimal(STATE["bot_config"]["margin_multiplier"])
                bot_ctx.recalc_interval_hours = Decimal(str(STATE["bot_config"].get("recalc_interval_hours", "8")))
                bot_ctx.active_strategy_settings.setdefault("general_grid_settings", {})["cluster_orders_count"] = int(STATE["bot_config"]["cluster_count"])

            elif data.get("type") == "update_config":
                STATE["symbol"] = data.get("symbol", STATE["symbol"])
                STATE["bot_config"]["leverage"] = data.get("leverage", "20")
                STATE["bot_config"]["initial_margin"] = data.get("initial_margin", "20")
                STATE["bot_config"]["margin_multiplier"] = data.get("margin_multiplier", "1.2")
                STATE["bot_config"]["cluster_count"] = data.get("cluster_count", "3")
                STATE["bot_config"]["capital_limit"] = data.get("capital_limit", "300")
                STATE["bot_config"]["min_stake"] = data.get("min_stake", "1.1")
                
                # Sync to bot_ctx
                bot_ctx.selected_symbol = STATE["symbol"]
                bot_ctx.sim_leverage_input_var.val = STATE["bot_config"]["leverage"]
                bot_ctx.sim_initial_group_margin_input_var.val = STATE["bot_config"]["initial_margin"]
                bot_ctx.sim_margin_increase_factor_input_var.val = STATE["bot_config"]["margin_multiplier"]
                bot_ctx.sim_cluster_orders_count_input_var.val = STATE["bot_config"]["cluster_count"]
                bot_ctx.sim_total_capital_limit_var.val = STATE["bot_config"]["capital_limit"]
                
                logger.info(f"Config updated for {STATE['symbol']}: {STATE['bot_config']}")
                binance_handler.set_active_symbol(STATE["symbol"])
                bot_ctx.live_bot_params["leverage"] = Decimal(STATE["bot_config"]["leverage"])
                bot_ctx.live_bot_params["initial_group_margin"] = Decimal(STATE["bot_config"]["initial_margin"])
                bot_ctx.live_bot_params["margin_multiplier"] = Decimal(STATE["bot_config"]["margin_multiplier"])
                bot_ctx.recalc_interval_hours = Decimal(str(STATE["bot_config"].get("recalc_interval_hours", "8")))

                # Update strategy settings directly for the generator
                bot_ctx.active_strategy_settings.setdefault("general_grid_settings", {})["cluster_orders_count"] = int(STATE["bot_config"]["cluster_count"])
                
                # Save to disk per-symbol
                STATE["all_configs"][STATE["symbol"]] = dict(STATE["bot_config"])
                save_bot_config_json(STATE["all_configs"])
    except Exception:
        manager.disconnect(websocket)

async def binance_state_poller():
    while True:
        if binance_handler.connected and STATE.get("symbol"):
            try:
                binance_handler.fetch_full_position_state(STATE["symbol"])
                binance_handler.fetch_open_orders(STATE["symbol"])
                binance_handler.fetch_trade_history(STATE["symbol"], limit=20)
            except Exception as e:
                pass
        await asyncio.sleep(2)

async def live_bot_heartbeat():
    """
    Periodic tick for the live trading manager (replaces app.after).
    """
    logger.info("Starting live_bot_heartbeat loop...")
    while True:
        try:
            if bot_ctx.live_bot_active and bot_ctx.live_trading_manager:
                # The execution manager logic
                bot_ctx.live_trading_manager.grid_execution_manager._perform_monitoring_tasks_tick()
        except Exception as e:
            logger.error(f"Error in live_bot_heartbeat: {e}")
        await asyncio.sleep(1.5)

async def recalc_scheduler():
    """
    Періодичне завдання для перерахунку сітки.
    """
    logger.info("Starting recalc_scheduler loop...")
    while True:
        try:
            interval = float(bot_ctx.recalc_interval_hours)
            if interval > 0:
                await asyncio.sleep(interval * 3600)
                if bot_ctx.live_bot_active:
                    logger.info(f"[AUTO-RECALC] Triggering scheduled grid update ({interval}h)...")
                    try:
                        await internal_calculate_grid()
                        logger.info(f"[AUTO-RECALC] Successfully recalculated grids")
                    except Exception as inner_e:
                        logger.error(f"Failed to auto-recalculate: {inner_e}")
            else:
                await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Error in recalc_scheduler: {e}")
            await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()
    # Background fetch is removed as per user request (On-demand only)
    asyncio.create_task(binance_price_streamer())
    asyncio.create_task(binance_state_poller())
    asyncio.create_task(live_bot_heartbeat())
    asyncio.create_task(recalc_scheduler())

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
