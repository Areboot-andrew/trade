# config_manager.py
import json
import os
from copy import deepcopy
from typing import Dict, List, Any # Додано імпорт Dict та інших типів
from utils import load_json_file, save_json_file, set_nested_value # Імпортуємо з utils.py

# Імпорти з нового модуля квантизації
from asset_quantization_module import (
    load_asset_precision_data,
    save_asset_precision_data,
    CURRENCIES_PRECISION_FILE # Використовуємо ім'я файлу з модуля
)

# Глобальні константи для імен файлів конфігурації
ADVANCED_SETTINGS_FILENAME = "advanced_strategy_settings.json"
# CURRENCIES_FILENAME тепер визначено в asset_quantization_module як CURRENCIES_PRECISION_FILE

# DEFAULT_CURRENCIES тепер список символів для початкового заповнення, якщо файл відсутній
DEFAULT_SYMBOLS_LIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AAVEUSDT"]

DEFAULT_ADVANCED_SETTINGS = { # Мінімальні дефолтні налаштування, якщо файл відсутній
    "strategy_name": "Default Strategy",
    "general_grid_settings": {
        "initial_group_margin_usd": "10.0",
        "margin_increase_factor_per_group": 1.2
    },
    "grid_timeframe_escalation": {
        "enabled": False,
        "timeframes_config": [
            {
                "timeframe_id": "M15_default",
                "binance_interval_notation": "15m", # Binance інтервал
                "max_active_groups_on_tf": 3,
                "atr_period": 14, "bb_period": 20, "bb_std_dev": "2.0",
                "sma_short_period": 20, "sma_long_period": 50, "sma_200_period": 200,
                "rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
                "stoch_rsi_length": 14, "stoch_rsi_rsi_length": 14, "stoch_rsi_k": 3, "stoch_rsi_d": 3,
                "ichimoku_tenkan": 9, "ichimoku_kijun": 26, "ichimoku_senkou_b_period": 52,
                "ichimoku_chikou_span_offset":26, "ichimoku_senkou_span_a_b_offset":26,
                "extremum_local_lookback_candles": 24,
                "level_type_priority": ["bb", "sma_long", "extremum_local"]
            }
        ]
    },
    "profit_taking_and_risk_reduction": {
        "overall_target_pnl_percent": "15.0",
        "trailing_stop_loss": {
            "enabled": True,
            "activation_pnl_percent": "5.0",
            "distance_from_price_percent": "1.5"
        }
    },
    "user_interface_presets": {
        "default_aggression_profile_name": "moderate_v2", 
        "aggression_profiles": [
            {"profile_name": "moderate_v2", "display_name": "Помірний v2", "overrides": {}}
        ]
    }
}


def get_default_advanced_settings() -> Dict[str, Any]:
    """Повертає копію дефолтних розширених налаштувань."""
    return deepcopy(DEFAULT_ADVANCED_SETTINGS)

def load_and_apply_profile_settings(profile_to_activate_name: str = None, base_filepath: str = ADVANCED_SETTINGS_FILENAME) -> Dict[str, Any]:
    """
    Завантажує розширені налаштування та застосовує вказаний профіль.
    """
    base_advanced_settings = load_json_file(base_filepath, default_data=get_default_advanced_settings())

    if not isinstance(base_advanced_settings, dict) or not base_advanced_settings:
        base_advanced_settings = get_default_advanced_settings()

    working_settings = deepcopy(base_advanced_settings)
    presets_config = working_settings.get("user_interface_presets", {})
    active_profile_name_to_apply = profile_to_activate_name

    if not active_profile_name_to_apply and isinstance(presets_config, dict):
        active_profile_name_to_apply = presets_config.get("default_aggression_profile_name")

    active_profile_data = None
    if active_profile_name_to_apply and isinstance(presets_config, dict) and isinstance(presets_config.get("aggression_profiles"), list):
        for profile in presets_config.get("aggression_profiles", []):
            if isinstance(profile, dict) and profile.get("profile_name") == active_profile_name_to_apply:
                active_profile_data = profile
                break

    if active_profile_data and isinstance(active_profile_data.get("overrides"), dict):
        overrides = active_profile_data.get("overrides", {})
        for path_str, value in overrides.items():
            if not set_nested_value(working_settings, path_str, value):
                pass
    elif profile_to_activate_name:
        default_profile_name_from_config = presets_config.get("default_aggression_profile_name") if isinstance(presets_config, dict) else None
        if profile_to_activate_name != default_profile_name_from_config and default_profile_name_from_config:
            return load_and_apply_profile_settings(default_profile_name_from_config, base_filepath)
        else:
            pass

    working_settings["_applied_profile_name"] = active_profile_name_to_apply if active_profile_data else "base_settings"
    if "user_interface_presets" in working_settings:
        del working_settings["user_interface_presets"]
    return working_settings

def save_advanced_settings(settings_dict: Dict[str, Any], filepath: str = ADVANCED_SETTINGS_FILENAME) -> bool:
    """
    Зберігає розширені налаштування у файл.
    """
    return save_json_file(filepath, settings_dict)


def load_asset_precisions(filepath: str = CURRENCIES_PRECISION_FILE) -> Dict[str, Dict[str, str]]:
    """Завантажує дані точності активів (нова назва для load_currencies)."""
    return load_asset_precision_data(filepath) # Використовуємо функцію з нового модуля

def save_asset_precisions(precision_map: Dict[str, Dict[str, str]], filepath: str = CURRENCIES_PRECISION_FILE) -> bool:
    """Зберігає дані точності активів (нова назва для save_currencies)."""
    return save_asset_precision_data(precision_map, filepath) # Використовуємо функцію з нового модуля

def get_default_precision_for_symbol(symbol: str) -> Dict[str, str]:
    """Повертає дефолтну структуру для даних точності (порожні або базові значення)."""
    return {
        "symbol": symbol,
        "price_precision_str": "0.00000001", 
        "quantity_precision_str": "0.001",    
        "min_quantity_str": "0.001",       
        "min_notional_str": "1.0"          
    }

def ensure_config_files_exist():
    """
    Перевіряє наявність конфігураційних файлів і створює їх з дефолтними значеннями, якщо вони відсутні.
    """
    if not os.path.exists(CURRENCIES_PRECISION_FILE):
        print(f"Файл {CURRENCIES_PRECISION_FILE} не знайдено, створюю з дефолтними символами.")
        default_precision_map: Dict[str, Dict[str, str]] = {} # Явна типізація
        for symbol_str in DEFAULT_SYMBOLS_LIST:
            default_precision_map[symbol_str] = get_default_precision_for_symbol(symbol_str)
        save_asset_precisions(default_precision_map, CURRENCIES_PRECISION_FILE)

    if not os.path.exists(ADVANCED_SETTINGS_FILENAME):
        print(f"Файл {ADVANCED_SETTINGS_FILENAME} не знайдено, створюю з дефолтними значеннями.")
        save_advanced_settings(get_default_advanced_settings(), ADVANCED_SETTINGS_FILENAME)

