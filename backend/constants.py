# constants.py
import os
from binance.client import Client 
from decimal import Decimal # Не забуваємо імпортувати Decimal

# --- Глобальні конфігураційні файли ---
CONFIG_FILE_CURRENCIES = "crypto_pnl_currencies.json"
DB_NAME = "market_kline_data.db"
ADVANCED_CONFIG_FILE = "advanced_strategy_settings.json"

# --- Глобальні константи та дефолти ---
DATA_REFRESH_INTERVAL_SECONDS = 5 * 60 
DEFAULT_COMMISSION_RATE_STR = "0.0004" # Можна залишити для сумісності або прибрати, якщо не використовується
INITIAL_DB_FILL_LIMIT = 1000 
MIN_KLINE_DATA_FOR_ANALYSIS = 800 

API_KEY = os.getenv("BINANCE_API_KEY", "") 
API_SECRET = os.getenv("BINANCE_API_SECRET", "") 

DEFAULT_M1_TF_ID = "M1_default_volatility"
DEFAULT_M5_TF_ID = "M5_default_base"
DEFAULT_M15_TF_ID = "M15_default_main" 
DEFAULT_H1_TF_ID = "H1_default_trend"
DEFAULT_H4_TF_ID = "H4_default_long_trend"

LIVE_DATA_REFRESH_INTERVAL_SECONDS = 10 

# --- НОВІ КОНСТАНТИ ДЛЯ КОМІСІЙ ---
TAKER_COMMISSION_RATE = Decimal("0.0004") # 0.04% (приклад для Binance Futures)
MAKER_COMMISSION_RATE = Decimal("0.0002") # 0.02% (приклад для Binance Futures)
# --- НОВІ КОНСТАНТИ ДЛЯ КОМІСІЙ ---
DEFAULT_TAKER_COMMISSION_RATE = Decimal("0.0004") # 0.04% (приклад для Binance Futures)
DEFAULT_MAKER_COMMISSION_RATE = Decimal("0.0002") # 0.02% (приклад для Binance Futures)
# ------------------------------------
WEBSOCKET_RECONNECT_DELAY_SECONDS = 5
