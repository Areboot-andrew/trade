# BOTV1/asset_quantization_module.py
import json
import os
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP, InvalidOperation
from binance.client import Client 
from typing import Dict, List, Tuple, Optional, Any
from constants import DEFAULT_TAKER_COMMISSION_RATE, DEFAULT_MAKER_COMMISSION_RATE
import math

CURRENCIES_PRECISION_FILE = "crypto_pnl_currencies.json" 

def load_asset_precision_data(filepath: str = CURRENCIES_PRECISION_FILE) -> Dict[str, Dict[str, str]]:
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content.strip(): return {}
                data_from_file = json.loads(content)
                if isinstance(data_from_file, list):
                    return {item['symbol']: item for item in data_from_file if isinstance(item, dict) and 'symbol' in item}
                elif isinstance(data_from_file, dict):
                    return data_from_file
                return {}
        except (json.JSONDecodeError, IOError, TypeError) as e:
            print(f"Error loading asset settings data from {filepath}: {e}. Returning empty data.")
            return {}
    return {}

def save_asset_precision_data(settings_map: Dict[str, Dict[str, str]], filepath: str = CURRENCIES_PRECISION_FILE) -> bool:
    data_list = list(settings_map.values())
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data_list, f, ensure_ascii=False, indent=2)
        return True
    except (IOError, TypeError) as e:
        print(f"Error saving asset settings data to {filepath}: {e}")
        return False

def fetch_and_update_precisions_for_symbol(binance_client: Client, symbol: str, existing_settings_data: Dict[str, Dict[str, str]]) -> bool:
    if not binance_client or not symbol: return False
    try:
        exchange_info = binance_client.futures_exchange_info()
        symbol_info_api = next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
        if not symbol_info_api: return False

        price_filter = next((f for f in symbol_info_api['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
        lot_size_filter = next((f for f in symbol_info_api['filters'] if f['filterType'] == 'LOT_SIZE'), None)
        min_notional_filter = next((f for f in symbol_info_api['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)
        if not all([price_filter, lot_size_filter, min_notional_filter]): return False

        current_symbol_settings = existing_settings_data.get(symbol, {"symbol": symbol})
        
        fields = {
            "price_precision_str": price_filter.get('tickSize'),
            "quantity_precision_str": lot_size_filter.get('stepSize'),
            "min_quantity_str": lot_size_filter.get('minQty'),
            "min_notional_str": min_notional_filter.get('notional')
        }
        
        for key, value in fields.items():
            if value is not None:
                current_symbol_settings[key] = str(Decimal(value).normalize())
        
        existing_settings_data[symbol] = current_symbol_settings
        return True
    except Exception as e:
        print(f"Precision fetch: Error for {symbol}: {e}")
        return False

def get_symbol_precision_info(symbol: str, settings_data: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not symbol: return None
    return settings_data.get(symbol.upper())

### ВИПРАВЛЕНО: Функції тепер використовують .normalize() для видалення зайвих нулів ###
def adjust_price_to_tick(price: Decimal, tick_size_str: Optional[str]) -> Decimal:
    """Коригує ціну відповідно до кроку зміни ціни (tickSize)."""
    if tick_size_str is None: return price.normalize()
    try:
        tick_size = Decimal(tick_size_str)
        if tick_size <= Decimal(0): return price.normalize()
        
        adjusted_price = (price / tick_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * tick_size
        return adjusted_price.normalize()
    except InvalidOperation:
        print(f"Error: Invalid tick_size_str '{tick_size_str}' for price adjustment of {price}.")
        return price.normalize()

def adjust_quantity_to_step(quantity: Decimal, step_size_str: Optional[str], min_qty_str: Optional[str], max_qty_str: Optional[str] = None, rounding_mode: str = "DOWN") -> Optional[Decimal]:
    """Коригує кількість відповідно до кроку зміни (stepSize)."""
    if step_size_str is None or min_qty_str is None: return None
    try:
        step_size = Decimal(step_size_str)
        min_qty = Decimal(min_qty_str)
        max_qty = Decimal(max_qty_str) if max_qty_str and max_qty_str.strip() else None

        if step_size <= Decimal(0):
            adjusted_quantity = quantity
        else:
            if rounding_mode.upper() == "UP":
                adjusted_quantity = (math.ceil(quantity / step_size)) * step_size
            else:
                adjusted_quantity = (quantity // step_size) * step_size
        
        if adjusted_quantity < min_qty or (max_qty and adjusted_quantity > max_qty) or adjusted_quantity <= Decimal(0):
            return None
        
        return adjusted_quantity.normalize()
    except InvalidOperation:
        print(f"Error: Invalid number string for quantity adjustment (quantity:'{quantity}', step:'{step_size_str}').")
        return None

def is_notional_valid(quantity: Decimal, price: Decimal, min_notional_str: Optional[str]) -> bool:
    if min_notional_str is None: return True
    try:
        min_notional = Decimal(min_notional_str)
        if min_notional <= Decimal(0): return True
        return (quantity * price) >= min_notional
    except InvalidOperation: return False

def calculate_base_from_quote(
    desired_quote_amount: Decimal, price_for_calculation: Decimal,
    quantity_step_size_str: Optional[str], min_quantity_str: Optional[str],
    price_tick_size_str: Optional[str], min_notional_str: Optional[str], 
    max_quantity_str: Optional[str] = None
) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    if price_for_calculation <= Decimal(0) or desired_quote_amount <= Decimal(0): return None, None
    if not all([quantity_step_size_str, min_quantity_str, min_notional_str]): return None, None

    initial_base_quantity = desired_quote_amount / price_for_calculation
    adjusted_base_quantity = adjust_quantity_to_step(initial_base_quantity, quantity_step_size_str, min_quantity_str, max_quantity_str)
    
    if adjusted_base_quantity is None:
        adjusted_base_quantity = adjust_quantity_to_step(Decimal(min_quantity_str), quantity_step_size_str, min_quantity_str, max_quantity_str, rounding_mode="UP")
        if adjusted_base_quantity is None: return None, None

    if not is_notional_valid(adjusted_base_quantity, price_for_calculation, min_notional_str):
        min_notional_val = Decimal(min_notional_str)
        required_qty = (min_notional_val / price_for_calculation) * Decimal('1.01')
        adjusted_base_quantity = adjust_quantity_to_step(required_qty, quantity_step_size_str, min_quantity_str, max_quantity_str, rounding_mode="UP")
        if adjusted_base_quantity is None or not is_notional_valid(adjusted_base_quantity, price_for_calculation, min_notional_str):
            return None, None

    actual_quote_amount = adjusted_base_quantity * price_for_calculation
    return adjusted_base_quantity, actual_quote_amount.quantize(Decimal("0.00000001"))