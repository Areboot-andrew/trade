# botV3/binance_live_handler.py
import logging
import time
import json
import threading
from typing import Callable, Dict, Set, Optional, List, Any
# BinanceSocketManager тут не використовується, оскільки цей клас для REST API
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
from decimal import Decimal, InvalidOperation
from datetime import datetime
import queue

from asset_quantization_module import (
    adjust_quantity_to_step,
    adjust_price_to_tick,
    is_notional_valid,
    fetch_and_update_precisions_for_symbol,
    get_symbol_precision_info,
    save_asset_precision_data
)

logger = logging.getLogger(__name__)

# Створюємо семафор, який дозволяє, наприклад, не більше 5 одночасних API-запитів
# Ви можете підібрати це значення. 10 - це розмір пулу, можливо, варто почати з меншого.
API_CALL_SEMAPHORE = threading.Semaphore(5)

class BinanceLiveHandler:
    def __init__(self, gui_queue_ref: queue.Queue, app_instance_ref: Any):
        self.client: Optional[Client] = None
        self.gui_queue = gui_queue_ref
        self.app = app_instance_ref

        self.api_key: Optional[str] = None
        self.api_secret: Optional[str] = None
        self.connected: bool = False

        self.exchange_info_raw: Optional[Dict[str, Any]] = None
        self.current_symbol_details: Dict[str, Dict[str, Any]] = {}
        self.symbol_data_ready: Dict[str, bool] = {}

        self.current_position_mode: Optional[bool] = None # True for Hedge, False for One-way
        self.current_symbol_active_in_handler: Optional[str] = None

    def _log_to_gui(self, message: str, level: str = "INFO"):
        self.gui_queue.put(("live_log", f"[{level.upper()}] [API_Handler] {message}"))

    def connect(self, api_key: str, api_secret: str) -> bool:
        self.api_key = api_key
        self.api_secret = api_secret
        if not self.api_key or not self.api_secret:
            self._log_to_gui("API Key або Secret не надано.", "ERROR") # API Key or Secret not provided.
            self.gui_queue.put(("update_connection_status", {"connected": False, "message": "Ключі API відсутні"})) # API keys missing
            return False
        try:
            # При створенні клієнта можна передати власний requests.Session
            # для більш тонкого налаштування, але зазвичай це не потрібно для простого обмеження.
            self.client = Client(self.api_key, self.api_secret, testnet=False)
            # Sync time to fix APIError(code=-1021): Timestamp for this request is outside of the recvWindow
            server_time = self.client.get_server_time()
            self.client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)
            self._log_to_gui(f"Синхронізація часу виконана. Офсет: {self.client.timestamp_offset}ms", "DEBUG")
            
            self.client.ping()
            self.connected = True
            self._log_to_gui("Успішно підключено до Binance Futures.", "SUCCESS") # Successfully connected to Binance Futures.
            self.gui_queue.put(("update_connection_status", {"connected": True, "message": "Підключено (Futures)"})) # Connected (Futures)

            self.fetch_position_mode()

            current_gui_symbol = self.app.currency_var.get()
            if current_gui_symbol and current_gui_symbol != "Оберіть/Додайте": # Select/Add
                self.set_active_symbol(current_gui_symbol)

            return True
        except BinanceAPIException as bae:
            self.connected = False
            self.client = None
            err_msg = f"Помилка API Binance при підключенні: {bae}" # Binance API error during connection:
            self._log_to_gui(err_msg, "ERROR")
            self.gui_queue.put(("update_connection_status", {"connected": False, "message": f"Помилка API: {str(bae)[:30]}..."})) # API Error:
            return False
        except Exception as e:
            self.connected = False
            self.client = None
            err_msg = f"Загальна помилка підключення: {e}" # General connection error:
            self._log_to_gui(err_msg, "ERROR")
            self.gui_queue.put(("update_connection_status", {"connected": False, "message": err_msg[:50]}))
            return False

    def disconnect(self):
        self._log_to_gui("Процес відключення від Binance...", "INFO") # Disconnecting from Binance...
        self.gui_queue.put(("update_connection_status", {"connected": False, "message": "Відключено"})) # Disconnected
        self.gui_queue.put(("live_data_reset_on_disconnect", None))

        self.client = None
        self.connected = False
        self.api_key = None
        self.api_secret = None

        self.current_position_mode = None
        self.exchange_info_raw = None
        self.current_symbol_details.clear()
        self.symbol_data_ready.clear()
        self.current_symbol_active_in_handler = None

        self._log_to_gui("Відключено від Binance.", "INFO") # Disconnected from Binance.

    def set_active_symbol(self, symbol: Optional[str]):
        new_symbol_upper = symbol.upper() if symbol and symbol != "Оберіть/Додайте" else None # Select/Add

        if self.current_symbol_active_in_handler != new_symbol_upper:
            self.current_symbol_active_in_handler = new_symbol_upper
            self._log_to_gui(f"Активний символ для API Handler встановлено: {new_symbol_upper if new_symbol_upper else 'None'}", "DEBUG") # Active symbol for API Handler set to:

            if self.connected and self.current_symbol_active_in_handler:
                self.symbol_data_ready[self.current_symbol_active_in_handler] = False
                self.gui_queue.put(("symbol_data_status", {"symbol": self.current_symbol_active_in_handler, "ready": False, "status_message": "Завантаження..."})) # Loading...
                self.load_exchange_info_for_symbol(self.current_symbol_active_in_handler)
                self.fetch_current_leverage(self.current_symbol_active_in_handler)
            elif self.current_symbol_active_in_handler:
                 self.symbol_data_ready[self.current_symbol_active_in_handler] = False
                 self.gui_queue.put(("symbol_data_status", {"symbol": self.current_symbol_active_in_handler, "ready": False, "status_message": "Очікування підключення..."})) # Waiting for connection...
            else:
                 self.gui_queue.put(("symbol_data_status", {"symbol": None, "ready": False, "status_message": "Символ не обрано"})) # Symbol not selected

    def _execute_api_task(self, target_func: Callable, *args: Any, **kwargs: Any):
        """Target function for the thread, includes semaphore."""
        error_queue_msg_type = kwargs.pop("callback_error_queue_message_internal", None) # Use a different key to avoid conflict
        with API_CALL_SEMAPHORE:
            try:
                # self._log_to_gui(f"Thread {threading.get_ident()} acquired semaphore for {target_func.__name__}", "TRACE")
                target_func(*args, **kwargs)
            except Exception as e_task:
                 self._log_to_gui(f"Unhandled exception in _execute_api_task ({target_func.__name__}): {e_task}", "CRITICAL")
                 if error_queue_msg_type: # Optionally send a generic error if task crashes badly
                    self.gui_queue.put((error_queue_msg_type, {"error": f"Task crash: {target_func.__name__}"}))
            # finally:
                # self._log_to_gui(f"Thread {threading.get_ident()} released semaphore for {target_func.__name__}", "TRACE")


    def _threaded_api_call(self, target_func: Callable, *args: Any, **kwargs: Any):
        """Manages threaded API calls using a semaphore."""
        if not self.connected or not self.client:
            self._log_to_gui("API виклик неможливий: немає підключення до Binance.", "ERROR") # API call not possible: no connection to Binance.
            error_queue_msg_type = kwargs.get("callback_error_queue_message") # Get it before it's popped
            if error_queue_msg_type:
                self.gui_queue.put((error_queue_msg_type, {"error": "Немає підключення"})) # No connection
            return

        # Pass the error message type to the actual task executor
        internal_error_msg_type = kwargs.pop("callback_error_queue_message", None)

        # Wrap the original target_func and its args/kwargs to be called by _execute_api_task
        # _execute_api_task will now handle the semaphore
        thread = threading.Thread(
            target=self._execute_api_task,
            args=(target_func, *args), # Pass original target_func and its args
            kwargs={**kwargs, "callback_error_queue_message_internal": internal_error_msg_type}, # Pass original kwargs and internal error type
            daemon=True
        )
        thread.start()


    def load_exchange_info_for_symbol(self, symbol: str):
        if not symbol:
            self._log_to_gui("Символ не вказано для завантаження exchange info.", "WARNING") # Symbol not specified for loading exchange info.
            return
        s_upper = symbol.upper()
        self._threaded_api_call(self._task_load_exchange_info, s_upper)

    def _task_load_exchange_info(self, symbol: str):
        if not self.client:
            self.symbol_data_ready[symbol] = False
            self.gui_queue.put(("symbol_data_status", {"symbol": symbol, "ready": False, "error_message": "Клієнт не ініціалізовано"})) # Client not initialized
            return
        try:
            if self.exchange_info_raw is None: # Завантажуємо, якщо ще не було
                self._log_to_gui("Завантаження загальної Exchange Info...", "DEBUG") # Loading general Exchange Info...
                self.exchange_info_raw = self.client.futures_exchange_info()
                self._log_to_gui("Загальна Exchange Info завантажена.", "DEBUG") # General Exchange Info loaded.

            symbol_data_raw = next((s for s in self.exchange_info_raw['symbols'] if s['symbol'] == symbol), None)

            if symbol_data_raw:
                self._log_to_gui(f"Знайдено дані для {symbol}. Оновлення точності...", "DEBUG") # Data found for {symbol}. Updating precision...
                precisions_updated = fetch_and_update_precisions_for_symbol(self.client, symbol, self.app.asset_precisions_data)
                if precisions_updated:
                    save_asset_precision_data(self.app.asset_precisions_data)
                    self._log_to_gui(f"Точність для {symbol} оновлено та збережено.", "DEBUG") # Precision for {symbol} updated and saved.

                precision_info_for_symbol = get_symbol_precision_info(symbol, self.app.asset_precisions_data)
                filters_for_symbol = {f['filterType']: f for f in symbol_data_raw['filters']}

                self.current_symbol_details[symbol] = {
                    "precision_info": precision_info_for_symbol,
                    "filters_raw": filters_for_symbol,
                    "raw_symbol_data": symbol_data_raw
                }
                self.symbol_data_ready[symbol] = True
                self._log_to_gui(f"Exchange info та точність для {symbol} завантажено.", "INFO") # Exchange info and precision for {symbol} loaded.
                self.gui_queue.put(("symbol_data_status", {"symbol": symbol, "ready": True, "precision_info": precision_info_for_symbol}))
            else:
                self._log_to_gui(f"Не знайдено Exchange Info для {symbol} на ф'ючерсах.", "WARNING") # Exchange Info for {symbol} not found on futures.
                if symbol in self.current_symbol_details: del self.current_symbol_details[symbol]
                self.symbol_data_ready[symbol] = False
                self.gui_queue.put(("symbol_data_status", {"symbol": symbol, "ready": False, "error_message": f"Не знайдено дані для {symbol}"})) # Data not found for {symbol}

        except BinanceAPIException as bae_ex:
             self._log_to_gui(f"Помилка API Binance при завантаженні Exchange Info для {symbol}: {bae_ex}", "ERROR") # Binance API error loading Exchange Info for {symbol}:
             self.symbol_data_ready[symbol] = False
             self.gui_queue.put(("symbol_data_status", {"symbol": symbol, "ready": False, "error_message": f"Помилка API: {str(bae_ex)[:50]}"})) # API Error:
        except Exception as e_gen:
            self._log_to_gui(f"Загальна помилка завантаження Exchange Info для {symbol}: {e_gen}", "ERROR") # General error loading Exchange Info for {symbol}:
            self.symbol_data_ready[symbol] = False
            self.gui_queue.put(("symbol_data_status", {"symbol": symbol, "ready": False, "error_message": f"Помилка: {str(e_gen)[:50]}"})) # Error:

    def get_symbol_precision_info(self, symbol: str) -> Optional[Dict[str, str]]:
        return self.current_symbol_details.get(symbol.upper(), {}).get("precision_info")

    def get_symbol_filters_raw(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.current_symbol_details.get(symbol.upper(), {}).get("filters_raw")

    def fetch_position_mode(self):
        self._threaded_api_call(self._task_get_position_mode)

    def _task_get_position_mode(self):
        if not self.client:
            self.gui_queue.put(("update_position_mode_display", "Помилка: Клієнт N/A")) # Error: Client N/A
            return
        try:
            pos_mode_data = self.client.futures_get_position_mode()
            self.current_position_mode = pos_mode_data.get('dualSidePosition')
            mode_str = "Hedge Mode" if self.current_position_mode else "One-way Mode"
            self._log_to_gui(f"Поточний режим позиції: {mode_str}", "INFO") # Current position mode:
            self.gui_queue.put(("update_position_mode_display", mode_str))
        except Exception as e:
            self._log_to_gui(f"Помилка отримання режиму позиції: {e}", "ERROR") # Error getting position mode:
            self.gui_queue.put(("update_position_mode_display", "Помилка")) # Error

    def fetch_current_leverage(self, symbol: str):
        if not symbol: return
        self._threaded_api_call(self._task_get_current_leverage, symbol.upper())

    def _task_get_current_leverage(self, symbol: str):
        if not self.client:
            self.gui_queue.put(("update_leverage_display", "Помилка: Клієнт N/A")) # Error: Client N/A
            return
        try:
            # Потрібно отримати інформацію про позицію, щоб дізнатись плече
            # futures_leverage() змінює плече, а не отримує поточне для вже відкритої позиції
            positions = self.client.futures_position_information(symbol=symbol)
            leverage_val_str = "N/A" # Default
            if positions:
                # Зазвичай для символу повертається один або два об'єкти (для LONG/SHORT у Hedge)
                # Нас цікавить будь-який, оскільки плече встановлюється на символ.
                for pos_info in positions: # Ітеруємо, хоча для одного символу може бути один запис
                    if pos_info.get('symbol') == symbol: # Подвійна перевірка символу
                        lev_cand = pos_info.get('leverage')
                        if lev_cand and Decimal(lev_cand) > 0: # Беремо перше валідне плече
                            leverage_val_str = f"{lev_cand}x"
                            break # Знайшли плече для символу

            self._log_to_gui(f"Поточне плече для {symbol}: {leverage_val_str}", "DEBUG") # Current leverage for {symbol}:
            self.gui_queue.put(("update_leverage_display", leverage_val_str))
        except Exception as e:
            self._log_to_gui(f"Помилка отримання плеча для {symbol}: {e}", "ERROR") # Error getting leverage for {symbol}:
            self.gui_queue.put(("update_leverage_display", "Помилка")) # Error

    def fetch_account_balance(self):
        self._threaded_api_call(self._task_get_account_balance)

    def _task_get_account_balance(self):
        if not self.client:
            self.gui_queue.put(("update_account_balance_display", Decimal(-1))) # Signal error
            return
        try:
            balance_info = self.client.futures_account_balance()
            usdt_balance_item = next((item for item in balance_info if item["asset"] == "USDT"), None)
            balance_val = Decimal(usdt_balance_item['balance']) if usdt_balance_item else Decimal(0)
            self.gui_queue.put(("update_account_balance_display", balance_val))
        except Exception as e:
            self._log_to_gui(f"Помилка отримання балансу: {e}", "ERROR") # Error getting balance:
            self.gui_queue.put(("update_account_balance_display", Decimal(-1))) # Signal error

    def fetch_full_position_state(self, symbol: Optional[str] = None):
        """
        Нова функція, що збирає дані про позицію та відкриті ордери для розрахунку прогнозованої ціни.
        """
        _symbol = symbol.upper() if symbol else None
        if _symbol:
            self._threaded_api_call(self._task_fetch_full_position_state, _symbol)
        else:
            # Якщо символ не вказано, просто оновлюємо дані по позиціях без прогнозу
            self._threaded_api_call(self._task_get_positions, None)

    def _task_fetch_full_position_state(self, symbol: str):
        """
        Внутрішній таск, що виконує API-запити та розрахунки.
        """
        if not self.client:
            self.gui_queue.put(("update_positions_display", {"positions": []}))
            return
        
        try:
            # 1. Отримати інформацію про поточні позиції
            positions_info = self.client.futures_position_information(symbol=symbol)
            # 2. Отримати інформацію про відкриті ордери
            open_orders_info = self.client.futures_get_open_orders(symbol=symbol)

            active_positions_gui = []
            projected_avg_price = None

            for pos_raw in positions_info:
                pos_amt = Decimal(pos_raw.get('positionAmt', '0'))
                if pos_amt == Decimal(0):
                    continue # Пропускаємо неактивні позиції

                # Базові дані позиції
                entry_price = Decimal(pos_raw.get('entryPrice', '0'))
                unrealized_pnl = Decimal(pos_raw.get('unRealizedProfit', '0'))
                leverage_str = pos_raw.get('leverage', '1')
                position_side_api = pos_raw.get('positionSide', 'BOTH')
                mark_price = Decimal(pos_raw.get('markPrice', '0'))
                
                # --- Початок розрахунку прогнозованої ціни ---
                
                # Початкові значення з поточної позиції
                current_total_notional = pos_amt * entry_price
                current_total_amount = pos_amt
                
                has_averaging_orders = False
                
                # Перебираємо відкриті ордери, щоб знайти усереднюючі
                for order in open_orders_info:
                    # Ігноруємо ордери на закриття (reduceOnly) або не LIMIT ордери
                    if order.get('reduceOnly') is True or order.get('type') != 'LIMIT':
                        continue
                    
                    order_side = order.get('side')
                    order_qty_str = order.get('origQty', '0')
                    order_price_str = order.get('price', '0')
                    
                    is_averaging = False
                    # Для LONG позиції, усереднюючий ордер - це BUY
                    if pos_amt > 0 and order_side == 'BUY':
                        is_averaging = True
                    # Для SHORT позиції, усереднюючий ордер - це SELL
                    elif pos_amt < 0 and order_side == 'SELL':
                        is_averaging = True

                    if is_averaging:
                        try:
                            order_qty = Decimal(order_qty_str)
                            order_price = Decimal(order_price_str)
                            
                            # Додаємо до загальних сум
                            # Для SHORT позиції кількість буде від'ємною
                            signed_order_qty = order_qty if order_side == 'BUY' else -order_qty
                            
                            current_total_notional += (signed_order_qty * order_price)
                            current_total_amount += signed_order_qty
                            has_averaging_orders = True
                        except (InvalidOperation, TypeError):
                            self._log_to_gui(f"Некоректні дані в ордері {order.get('orderId')} для розрахунку прогнозу.", "WARNING")
                            continue
                
                # Розраховуємо фінальну прогнозовану ціну
                if has_averaging_orders and abs(current_total_amount) > Decimal('1e-12'):
                    projected_avg_price = abs(current_total_notional / current_total_amount)

                # --- Кінець розрахунку прогнозованої ціни ---

                active_positions_gui.append({
                    "symbol": pos_raw.get('symbol'), 
                    "amount": pos_amt,
                    "entry_price": entry_price, 
                    "mark_price": mark_price,
                    "pnl_usd": unrealized_pnl, 
                    "leverage": leverage_str, 
                    "margin_type": pos_raw.get('marginType', 'N/A'),
                    "position_side": position_side_api,
                    "projected_avg_price": projected_avg_price # Додаємо нове поле
                })

            self.gui_queue.put(("update_positions_display", {"positions": active_positions_gui}))
        except Exception as e:
            self._log_to_gui(f"Помилка отримання стану позиції ({symbol}): {e}", "ERROR")
            self.gui_queue.put(("update_positions_display", {"positions": []}))

    def fetch_positions(self, symbol: Optional[str] = None):
        _symbol = symbol.upper() if symbol else None
        self._threaded_api_call(self._task_get_positions, _symbol)

    def _task_get_positions(self, symbol: Optional[str] = None):
        # Ця функція залишається для сумісності, але основний потік йде через fetch_full_position_state
        if not self.client:
            self.gui_queue.put(("update_positions_display", {"positions": []}))
            return
        try:
            params = {}
            if symbol: params['symbol'] = symbol 
            positions_info = self.client.futures_position_information(**params)
            active_positions_gui = []
            for pos_raw in positions_info:
                pos_amt = Decimal(pos_raw.get('positionAmt', '0'))
                if pos_amt != Decimal(0):
                    active_positions_gui.append({
                        "symbol": pos_raw.get('symbol'), "amount": pos_amt,
                        "entry_price": Decimal(pos_raw.get('entryPrice', '0')), 
                        "mark_price": Decimal(pos_raw.get('markPrice', '0')),
                        "pnl_usd": Decimal(pos_raw.get('unRealizedProfit', '0')),
                        "leverage": pos_raw.get('leverage', '1'), 
                        "margin_type": pos_raw.get('marginType', 'N/A'),
                        "position_side": pos_raw.get('positionSide', 'BOTH'),
                        "projected_avg_price": None # Прогноз тут не розраховується
                    })
            self.gui_queue.put(("update_positions_display", {"positions": active_positions_gui}))
        except Exception as e:
            self._log_to_gui(f"Помилка отримання позицій ({symbol or 'all'}): {e}", "ERROR")
            self.gui_queue.put(("update_positions_display", {"positions": []}))

    def fetch_open_orders(self, symbol: Optional[str] = None):
        _symbol = symbol.upper() if symbol else None
        self._threaded_api_call(self._task_get_open_orders, _symbol)

    def _task_get_open_orders(self, symbol: Optional[str] = None):
        if not self.client:
            self.gui_queue.put(("update_open_orders_display", []))
            return
        try:
            params = {}
            if symbol: params['symbol'] = symbol 
            
            open_orders_info = self.client.futures_get_open_orders(**params)
            gui_orders = []
            for o in open_orders_info:
                gui_orders.append({ 
                    "orderId": o.get('orderId'), "symbol": o.get('symbol'),
                    "side": o.get('side'), "type": o.get('type'),
                    "origQty": o.get('origQty'), "price": o.get('price', 'MARKET'),
                    "status": o.get('status'), "positionSide": o.get('positionSide', 'N/A'),
                    "reduceOnly": o.get('reduceOnly', False)
                })
            self.gui_queue.put(("update_open_orders_display", gui_orders))
        except Exception as e:
            self._log_to_gui(f"Помилка отримання відкритих ордерів ({symbol or 'all'}): {e}", "ERROR")
            self.gui_queue.put(("update_open_orders_display", []))

    def fetch_trade_history(self, symbol: str, limit: int = 20):
        if not symbol: return
        self._threaded_api_call(self._task_get_trade_history, symbol.upper(), limit)

    def _task_get_trade_history(self, symbol: str, limit: int):
        if not self.client:
            self.gui_queue.put(("update_trade_history_display", []))
            return
        try:
            trades = self.client.futures_account_trades(symbol=symbol, limit=limit)
            gui_trades = []
            for trade in reversed(trades): # Show newest first
                gui_trades.append({ 
                    "time": trade.get('time',0), 
                    "symbol": trade.get('symbol'), "side": trade.get('side'),
                    "price": trade.get('price'), "qty": trade.get('qty'),
                    "realizedPnl": trade.get('realizedPnl'), "commission": trade.get('commission'),
                    "commissionAsset": trade.get('commissionAsset')
                })
            self.gui_queue.put(("update_trade_history_display", gui_trades))
        except Exception as e:
            self._log_to_gui(f"Помилка отримання історії угод для {symbol}: {e}", "ERROR")
            self.gui_queue.put(("update_trade_history_display", []))

    def place_order(self, symbol: str, side: str, order_type: str,
                    quantity_str: str, price_str: Optional[str] = None,
                    reduce_only_gui: bool = False,
                    position_side_gui: Optional[str] = None,
                    skip_local_validation: bool = False,
                    client_order_id_param: Optional[str] = None):

        self._log_to_gui(f"API_Handler: Запит: {symbol} {side} {order_type} Q:{quantity_str} P:{price_str} RO_GUI:{reduce_only_gui} PosSide_GUI:{position_side_gui}, SkipVal:{skip_local_validation}, ClientID: {client_order_id_param}", "DEBUG")

        if not self.connected or not self.client:
            self._log_to_gui("place_order: Не підключено.", "ERROR")
            self.gui_queue.put(("order_failed", {"error_message": "Клієнт не підключений.", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}}))
            return

        symbol_upper = symbol.upper()
        # Fallback: if we have precision info in app cache, we can proceed with validation even if full API info isn't "ready"
        has_cached_precision = self.app.asset_precisions_data.get(symbol_upper) is not None

        if not skip_local_validation and not self.symbol_data_ready.get(symbol_upper, False) and not has_cached_precision:
            self._log_to_gui(f"Дані для символу {symbol_upper} не готові (навіть у кеші). Локальна валідація неможлива.", "ERROR")
            self.gui_queue.put(("order_failed", {"error_message": f"Дані символу {symbol_upper} не знайдено. Перевірте файл точностей або натисніть 'Інфо'.", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}}))
            return
        elif not skip_local_validation and not self.symbol_data_ready.get(symbol_upper, False) and has_cached_precision:
            self._log_to_gui(f"Використовуємо кешовані дані точності для {symbol_upper} (API ще вантажиться).", "INFO")

        elif skip_local_validation and not self.symbol_data_ready.get(symbol_upper, False):
            self._log_to_gui(f"ПОПЕРЕДЖЕННЯ: Дані для {symbol_upper} не завантажені, АЛЕ локальна валідація ПРОПУСКАЄТЬСЯ!", "WARNING")

        try:
            quantity_decimal = Decimal(quantity_str)
            price_decimal = Decimal(price_str) if price_str and price_str.strip() and order_type.upper() == "LIMIT" else None
        except InvalidOperation:
            err_msg_format = f"Некоректний формат кількості ('{quantity_str}') або ціни ('{price_str}')."
            self._log_to_gui(err_msg_format, "ERROR")
            self.gui_queue.put(("order_failed", {"error_message": err_msg_format, "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}}))
            return

        if quantity_decimal <= Decimal(0):
            self._log_to_gui("Кількість має бути більшою за нуль.", "ERROR")
            self.gui_queue.put(("order_failed", {"error_message": "Кількість має бути > 0.", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}}))
            return
        if order_type.upper() == "LIMIT" and (price_decimal is None or price_decimal <= Decimal(0)):
            self._log_to_gui("Ціна для LIMIT ордера має бути більшою за нуль.", "ERROR")
            self.gui_queue.put(("order_failed", {"error_message": "Ціна LIMIT має бути > 0.", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}}))
            return

        adj_qty = quantity_decimal
        adj_price = price_decimal

        if not skip_local_validation:
            self._log_to_gui(f"Виконується локальна валідація для {symbol_upper}...", "DEBUG")
            precision_info_current = self.get_symbol_precision_info(symbol_upper)
            filters_raw_current = self.get_symbol_filters_raw(symbol_upper)
            if not precision_info_current or not filters_raw_current:
                self._log_to_gui(f"Критична помилка валідації: відсутні дані точності/фільтрів для {symbol_upper}.", "ERROR")
                self.gui_queue.put(("order_failed", {"error_message": f"Внутрішня помилка: дані символу {symbol_upper} недоступні для валідації.", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}}))
                return

            lot_size_filter = filters_raw_current.get('LOT_SIZE', {})
            temp_adj_qty = adjust_quantity_to_step(
                quantity_decimal,
                precision_info_current.get('quantity_precision_str'),
                precision_info_current.get('min_quantity_str'),
                lot_size_filter.get('maxQty')
            )
            if temp_adj_qty is None or temp_adj_qty <= Decimal(0):
                self._log_to_gui(f"Кількість {quantity_decimal} для {symbol_upper} невалідна після фільтрів LOT_SIZE. Результат: {temp_adj_qty}", "ERROR")
                self.gui_queue.put(("order_failed", {"error_message": "Кількість не пройшла фільтри.", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}})); return
            adj_qty = temp_adj_qty

            if order_type.upper() == "LIMIT" and price_decimal is not None:
                temp_adj_price = adjust_price_to_tick(
                    price_decimal,
                    precision_info_current.get('price_precision_str')
                )
                if temp_adj_price is None or temp_adj_price <= Decimal(0):
                    self._log_to_gui(f"Ціна {price_decimal} для {symbol_upper} невалідна після фільтрів PRICE_FILTER. Результат: {temp_adj_price}", "ERROR")
                    self.gui_queue.put(("order_failed", {"error_message": "Ціна не пройшла фільтри.", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}})); return
                adj_price = temp_adj_price

            price_for_notional_check = adj_price if order_type.upper() == "LIMIT" else None
            if order_type.upper() == "MARKET" and self.client:
                try:
                    mark_price_data = self.client.futures_mark_price(symbol=symbol_upper)
                    price_for_notional_check = Decimal(mark_price_data['markPrice'])
                except Exception as e_mn:
                    self._log_to_gui(f"Помилка отримання mark price для MinNotional ({symbol_upper}): {e_mn}. Перевірка MinNotional може бути неточною.", "WARNING")

            if price_for_notional_check is not None and price_for_notional_check > 0:
                if not is_notional_valid(adj_qty, price_for_notional_check, precision_info_current.get('min_notional_str')):
                    notional_val = adj_qty * price_for_notional_check
                    min_not_val_str = precision_info_current.get('min_notional_str', 'N/A')
                    self._log_to_gui(f"Ордер для {symbol_upper} (Q:{adj_qty}, P_check:{price_for_notional_check}) не проходить MIN_NOTIONAL ({min_not_val_str}). Номінал: {notional_val}", "ERROR")
                    self.gui_queue.put(("order_failed", {"error_message": f"Не пройдено MinNotional. Поточний: {notional_val:.2f}, Мін.: {min_not_val_str}", "params": {"symbol":symbol, "side":side, "type":order_type, "clientOrderId": client_order_id_param}})); return
            elif order_type.upper() == "MARKET":
                self._log_to_gui(f"Не вдалося отримати ціну для перевірки MinNotional для MARKET {symbol_upper}. Перевірку пропущено.", "WARNING")
        else:
            self._log_to_gui(f"ПОПЕРЕДЖЕННЯ: Локальну валідацію для ордера {symbol_upper} пропущено! Ордер відправляється 'як є'.", "WARNING")

        order_params = {'symbol': symbol_upper, 'side': side.upper(), 'type': order_type.upper(), 'quantity': str(adj_qty)}

        if client_order_id_param:
            order_params['newClientOrderId'] = client_order_id_param

        if order_type.upper() == "LIMIT":
            if adj_price is None:
                 self._log_to_gui(f"Критична помилка: ціна для LIMIT ордера {symbol_upper} є None після всіх перевірок.", "CRITICAL")
                 self.gui_queue.put(("order_failed", {"error_message": "Внутрішня помилка: ціна LIMIT не встановлена.", "params": {**order_params, "clientOrderId": client_order_id_param} }))
                 return
            order_params['price'] = str(adj_price)
            order_params['timeInForce'] = 'GTC'

        is_hedge = self.current_position_mode

        if is_hedge is True:
            if position_side_gui and position_side_gui.upper() in ["LONG", "SHORT"]:
                order_params['positionSide'] = position_side_gui.upper()
            else:
                if not reduce_only_gui :
                    err_msg_ps_hedge = f"Для Hedge Mode (не reduceOnly) потрібно вказати positionSide (LONG/SHORT). Отримано: {position_side_gui}"
                    self._log_to_gui(err_msg_ps_hedge, "ERROR")
                    self.gui_queue.put(("order_failed", {"error_message": err_msg_ps_hedge, "params": {**order_params, "clientOrderId": client_order_id_param}}))
                    return
                elif not (position_side_gui and position_side_gui.upper() in ["LONG", "SHORT"]):
                    err_msg_ps_hedge_reduce = f"Для Hedge Mode (reduceOnly) потрібно вказати positionSide (LONG/SHORT) позиції, що закривається. Отримано: {position_side_gui}"
                    self._log_to_gui(err_msg_ps_hedge_reduce, "ERROR")
                    self.gui_queue.put(("order_failed", {"error_message": err_msg_ps_hedge_reduce, "params": {**order_params, "clientOrderId": client_order_id_param}}))
                    return
                else:
                     order_params['positionSide'] = position_side_gui.upper()

            is_reducing_order_logically = \
                (side.upper() == "SELL" and order_params.get('positionSide') == "LONG") or \
                (side.upper() == "BUY" and order_params.get('positionSide') == "SHORT")

            if reduce_only_gui:
                if not is_reducing_order_logically:
                    self._log_to_gui(f"УВАГА (Hedge): reduceOnly=true з GUI, але комбінація side/positionSide ({side.upper()}/{order_params.get('positionSide')}) не вказує на зменшення. Параметр reduceOnly НЕ буде надіслано.", "WARNING")
                else:
                    self._log_to_gui(f"Hedge Mode: Ордер на зменшення (reduceOnly=true з GUI). Параметр reduceOnly НЕ буде надіслано, покладаємось на positionSide={order_params.get('positionSide')}.", "DEBUG")

        elif is_hedge is False:
            if 'positionSide' in order_params: del order_params['positionSide']
            if position_side_gui and position_side_gui.upper() != "BOTH":
                 self._log_to_gui(f"У One-Way режимі PositionSide '{position_side_gui}' ігнорується. Використовується тільки 'reduceOnly', якщо встановлено.", "WARNING")

            if reduce_only_gui:
                order_params['reduceOnly'] = 'true'

        else:
            self._log_to_gui(f"УВАГА: Режим позиції ще не визначено (is None). Ордер відправляється з обережністю. positionSide з GUI: {position_side_gui}, reduceOnly з GUI: {reduce_only_gui}", "WARNING")
            if position_side_gui and position_side_gui.upper() in ["LONG", "SHORT", "BOTH"]:
                 order_params['positionSide'] = position_side_gui.upper()
            elif 'positionSide' in order_params:
                del order_params['positionSide']

            if reduce_only_gui:
                order_params['reduceOnly'] = 'true'

        self._log_to_gui(f"API_Handler: Фінальні параметри для API: {order_params}", "DEBUG")
        self._threaded_api_call(self._task_place_order, order_params)


    def _task_place_order(self, order_params: dict):
        if not self.client: return
        client_order_id_for_error_reporting = order_params.get("newClientOrderId")

        try:
            self._log_to_gui(f"Надсилання ордера на Binance: {order_params}", "INFO")
            order_response = self.client.futures_create_order(**order_params)
            self._log_to_gui(f"Ордер успішно розміщено. ID: {order_response.get('orderId')}. Відповідь: {order_response}", "SUCCESS")
            self.gui_queue.put(("order_placed_successfully", order_response))
            time.sleep(0.5)
            self.fetch_account_balance()
            self.fetch_full_position_state(order_params['symbol'])
            self.fetch_open_orders(order_params['symbol'])
        except (BinanceAPIException, BinanceOrderException) as e_binance:
            err_msg_place = f"Помилка Binance при розміщенні ордера ({e_binance.code if hasattr(e_binance, 'code') else 'N/A'}): {e_binance.message if hasattr(e_binance, 'message') else str(e_binance)}"
            self._log_to_gui(f"{err_msg_place}. Params: {order_params}", "ERROR")
            error_params_with_client_id = order_params.copy()
            if client_order_id_for_error_reporting and "clientOrderId" not in error_params_with_client_id :
                 error_params_with_client_id["clientOrderId"] = client_order_id_for_error_reporting
            self.gui_queue.put(("order_failed", {"error_message": err_msg_place, "params": error_params_with_client_id}))
        except Exception as e_gen:
            self._log_to_gui(f"Загальна помилка при розміщенні ордера: {e_gen}. Params: {order_params}", "ERROR")
            error_params_with_client_id_gen = order_params.copy()
            if client_order_id_for_error_reporting and "clientOrderId" not in error_params_with_client_id_gen:
                 error_params_with_client_id_gen["clientOrderId"] = client_order_id_for_error_reporting
            self.gui_queue.put(("order_failed", {"error_message": f"Загальна помилка: {e_gen}", "params": error_params_with_client_id_gen}))

    def cancel_order(self, symbol: str, order_id_str: str):
        if not order_id_str or not order_id_str.strip():
            self._log_to_gui("ID ордера для скасування не вказано.", "ERROR")
            self.gui_queue.put(("show_messagebox", ("error", "Помилка Скасування", "ID ордера не вказано.")))
            return
        self._threaded_api_call(self._task_cancel_order, symbol.upper(), str(order_id_str).strip())

    def _task_cancel_order(self, symbol: str, order_id_to_cancel: str):
        if not self.client: return
        try:
            self._log_to_gui(f"Спроба скасувати ордер {order_id_to_cancel} для {symbol}", "INFO")
            cancel_response = self.client.futures_cancel_order(symbol=symbol, orderId=order_id_to_cancel)
            self._log_to_gui(f"Ордер {order_id_to_cancel} для {symbol} скасовано: {cancel_response}", "INFO")
            self.gui_queue.put(("order_cancelled_successfully", cancel_response))
            time.sleep(0.5)
            self.fetch_open_orders(symbol)
            self.fetch_full_position_state(symbol)
        except (BinanceAPIException, BinanceOrderException) as e:
            err_msg_cancel = f"Помилка Binance при скасуванні ордера {order_id_to_cancel} ({e.code if hasattr(e, 'code') else 'N/A'}): {e.message if hasattr(e, 'message') else str(e)}"
            self._log_to_gui(f"{err_msg_cancel} для {symbol}", "ERROR")
            self.gui_queue.put(("show_messagebox", ("error", "Помилка Скасування", err_msg_cancel)))
        except Exception as e_gen:
            self._log_to_gui(f"Загальна помилка при скасуванні ордера {order_id_to_cancel} для {symbol}: {e_gen}", "ERROR")
            self.gui_queue.put(("show_messagebox", ("error", "Помилка Скасування", f"Загальна помилка: {e_gen}")))


    def cancel_all_orders(self, symbol: str):
        if not symbol or not symbol.strip():
            self._log_to_gui("Символ для скасування всіх ордерів не вказано.", "ERROR")
            self.gui_queue.put(("show_messagebox", ("error", "Помилка", "Символ не вказано.")))
            return
        self._threaded_api_call(self._task_cancel_all_orders, symbol.upper())

    def _task_cancel_all_orders(self, symbol: str):
        if not self.client: return
        try:
            self._log_to_gui(f"Спроба скасувати ВСІ відкриті ордери для {symbol}", "INFO")
            cancel_response = self.client.futures_cancel_all_open_orders(symbol=symbol)
            self._log_to_gui(f"Всі ордери для {symbol} запит на скасування відправлено. Відповідь: {cancel_response}", "INFO")
            if isinstance(cancel_response, dict) and cancel_response.get("code") == "200":
                self.gui_queue.put(("info_message", f"Запит на скасування всіх ордерів для {symbol} успішно відправлено."))
            else:
                self.gui_queue.put(("show_messagebox", ("warning", "Результат Скасування Всіх", f"Відповідь від Binance: {str(cancel_response)[:150]}")))

            time.sleep(0.5)
            self.fetch_open_orders(symbol)
            self.fetch_full_position_state(symbol)
        except (BinanceAPIException, BinanceOrderException) as e:
            err_msg_cancel_all = f"Помилка Binance при скасуванні всіх ордерів для {symbol} ({e.code if hasattr(e, 'code') else 'N/A'}): {e.message if hasattr(e, 'message') else str(e)}"
            self._log_to_gui(err_msg_cancel_all, "ERROR")
            self.gui_queue.put(("show_messagebox", ("error", "Помилка Скасування Всіх", err_msg_cancel_all)))
        except Exception as e_gen:
            self._log_to_gui(f"Загальна помилка при скасуванні всіх ордерів для {symbol}: {e_gen}", "ERROR")
            self.gui_queue.put(("show_messagebox", ("error", "Помилка Скасування Всіх", f"Загальна помилка: {e_gen}")))