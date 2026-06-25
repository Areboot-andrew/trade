# main_live_trading_manager.py
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Any, Optional
from copy import deepcopy

try:
    from binance_live_handler import BinanceLiveHandler
    from live_grid_execution_manager import LiveGridExecutionManager
    from grid_generator_module import GridGenerator
    from utils import load_json_file
    from config_manager import load_and_apply_profile_settings
except ImportError as e:
    print(f"Critical Import Error in main_live_trading_manager.py: {e}")
    # Placeholder classes for syntactic validity if imports fail
    class BinanceLiveHandler: pass
    class LiveGridExecutionManager: pass
    class GridGenerator: pass
    def load_json_file(filepath, default_data=None): return default_data or {}
    def load_and_apply_profile_settings(profile_to_activate_name=None): return {}

class LiveTradingManager:
    """
    Керує запуском та зупинкою реального торгового бота.
    Бот може працювати в двох режимах одночасно:
    1. Моніторинг Тейк-Профіту для існуючої позиції.
    2. Режим "Тіні": слідування за сіткою Паперового бота (ЛПБ), якщо він активний.
    """
    def __init__(self, app_instance, binance_api_handler: BinanceLiveHandler, grid_generator_live: GridGenerator):
        self.app = app_instance
        self.binance_api_handler = binance_api_handler
        self.grid_generator_live = grid_generator_live
        self.grid_execution_manager = LiveGridExecutionManager(
            self.app, self.binance_api_handler, self.grid_generator_live
        )
        self.live_trading_active: bool = False
        self.live_trading_rules: Dict[str, Any] = {}
        self.processing_lock = threading.Lock()

    def _log_to_gui(self, message: str, level: str = "INFO"):
        """Допоміжна функція для логування через GUI чергу."""
        if hasattr(self.app, 'gui_queue'):
            log_message = f"[{level.upper()}] [LiveManager] {message}"
            self.app.gui_queue.put(("live_log", log_message))

    def load_live_trading_rules(self, filepath: str = "live_trading_rules.json"):
        """Завантажує правила реальної торгівлі з JSON файлу."""
        default_rules = {
            "partial_tp_levels": [],
        }
        self.live_trading_rules = load_json_file(filepath, default_data=default_rules)
        self._log_to_gui(f"Правила реальної торгівлі завантажено: {self.live_trading_rules}")

    def update_live_trading_rules_from_gui(self, partial_tp_levels_gui_data: List[Dict[str, str]]):
        """Оновлює правила часткового тейк-профіту на основі даних з GUI."""
        self.live_trading_rules["partial_tp_levels"] = []
        for level_data_gui in partial_tp_levels_gui_data:
            try:
                pnl_threshold_str = level_data_gui.get("pnl_threshold_percent", "0")
                close_portion_str = level_data_gui.get("close_percent_of_pos", "0")
                
                pnl_threshold = Decimal(pnl_threshold_str)
                close_portion = Decimal(close_portion_str)

                if 0 < pnl_threshold and 0 < close_portion <= 100:
                    self.live_trading_rules["partial_tp_levels"].append({
                        "pnl_threshold_percent": pnl_threshold_str,
                        "close_percent_of_pos": close_portion_str
                    })
                else:
                    self._log_to_gui(f"Некоректні значення для рівня часткового ТП: PNL={pnl_threshold_str}%, Частка={close_portion_str}%.", "WARNING")
            except (InvalidOperation, TypeError, KeyError) as e:
                self._log_to_gui(f"Помилка обробки даних рівня часткового ТП з GUI: {level_data_gui}, помилка: {e}", "ERROR")

        if self.live_trading_rules["partial_tp_levels"]:
            self.live_trading_rules["partial_tp_levels"].sort(key=lambda x: Decimal(x["pnl_threshold_percent"]))
        self._log_to_gui(f"Правила часткового ТП оновлено.")

    def activate_live_trading(self):
        """
        Активує реальну торгівлю. Бот буде слідкувати за ЛПБ (якщо він активний)
        і завжди моніторити тейк-профіт для обох сторін у хедж-режимі.
        """
        with self.processing_lock:
            if self.live_trading_active:
                self._log_to_gui("Live торгівля вже активна.", "WARNING")
                return
            
            if not self.binance_api_handler.connected:
                self.app.gui_queue.put(("show_messagebox", ("error", "Помилка", "Спочатку підключіться до Binance.")))
                return

            self.live_trading_active = True
            # Синхронізація зі станом BotContext
            self.app.live_bot_active = True 
            
            self._log_to_gui(f"АКТИВАЦІЯ LIVE ТОРГІВЛІ (Обидві сторони)...", "SUCCESS")
            
            leverage = self.app.live_bot_params.get("leverage", Decimal("20"))
            active_strategy_settings = self.app.active_strategy_settings or {}

            grid_params_for_exec_mgr = {
                "symbol": self.app.selected_symbol,
                "leverage": leverage,
                "position_type": "Both",
                "price_trigger_buffer_percent": active_strategy_settings.get("live_grid_config", {}).get("price_trigger_buffer_percent_live", "0.1")
            }
            
            self.grid_execution_manager.start_managing_grid([], grid_params_for_exec_mgr)
            self.app.gui_queue.put(("live_trading_status_update_button", {"active": True, "message": f"Активно (Dual)"}))

    def deactivate_live_trading(self, cancel_orders: bool = True):
        """Деактивує реальну торгівлю та, за бажанням, скасовує активні ордери бота."""
        with self.processing_lock:
            if not self.live_trading_active:
                self._log_to_gui("Live торгівля не активна.", "INFO")
                return

            self.live_trading_active = False
            self.app.live_bot_active = False
            self._log_to_gui("ДЕАКТИВАЦІЯ LIVE ТОРГІВЛІ...", "INFO")
            
            self.grid_execution_manager.stop_managing_grid(cancel_active_orders=cancel_orders)
            
            self.app.gui_queue.put(("live_trading_status_update_button", {"active": False, "message": "Зупинено"}))
            self.app.gui_queue.put(("update_live_managed_grid_orders_display", []))

    def update_real_order_status(self, order_update_data: Dict):
        """Передає оновлення статусу ордера з Binance до GridExecutionManager."""
        if self.live_trading_active and self.grid_execution_manager:
            self.grid_execution_manager.process_binance_order_update(order_update_data)
        else:
            self._log_to_gui("Оновлення ордера отримано, але live торгівля не активна.", "DEBUG")