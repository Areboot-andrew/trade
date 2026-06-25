# live_grid_execution_manager.py
import logging
import re
import time
import traceback
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from asset_quantization_module import adjust_quantity_to_step

logger = logging.getLogger(__name__)

ORDER_STATUS_MONITORING = "Моніторинг"
ORDER_STATUS_TRIGGERED = "Тригер"
ORDER_STATUS_PLACED = "Виставлено"
ORDER_STATUS_REJECTED = "Відхилено"
ORDER_STATUS_ERROR = "Помилка"
ORDER_STATUS_TP = "Очікує Тейк-Профіт"
ORDER_STATUS_FAILED_TO_PLACE = "Помилка виставлення"
ORDER_STATUS_REPOSITIONED_MAJOR = "Безпека (Погіршення)"
ORDER_STATUS_REPOSITIONED_MINOR = "Безпека (Близько/Вище)"


class LiveGridExecutionManager:
    def __init__(self, app_instance: Any, binance_api_handler: Any, grid_generator_ref: Any):
        self.app = app_instance
        self.api_handler = binance_api_handler
        self.grid_generator = grid_generator_ref
        
        self.placed_orders_state: Dict[str, Dict[str, Any]] = {}
        
        self.live_trading_active: bool = False
        self.current_live_grid_params: Dict[str, Any] = {}
        
        self.real_positions_by_side: Dict[str, Dict[str, Any]] = {"LONG": {}, "SHORT": {}}
        
        self.partial_tp_cooldowns: Dict[str, float] = {}
        self.executed_partial_tp_levels_by_side: Dict[str, set[str]] = {"LONG": set(), "SHORT": set()}
        
        self.monitor_timer: Optional[Any] = None
        self.MONITOR_INTERVAL_MS = 1500

        self.ORDER_PLACEMENT_COOLDOWN_SECONDS = 15
        self.last_order_placement_time: float = 0.0

        # --- НАЛАШТУВАННЯ СТРАХУВАЛЬНИХ ОРДЕРІВ ---
        self.MAJOR_SAFETY_OFFSET_BASE = Decimal("2.5")  
        self.MAJOR_SAFETY_OFFSET_STEP = Decimal("0.35") 

        self.MINOR_SAFETY_ZONE_PERCENT = Decimal("1.2") 
        self.MINOR_SAFETY_OFFSET_BASE = Decimal("1.5") 
        self.MINOR_SAFETY_OFFSET_STEP = Decimal("0.3")  

    def _log_to_gui(self, message: str, level: str = "INFO"):
        if hasattr(self.app, 'gui_queue'):
            log_message = f"[{level.upper()}] [GridExec] {message}"
            self.app.gui_queue.put(("live_log", log_message))
        else:
            print(f"LOG_FALLBACK [GridExec] [{level.upper()}]: {message}")

    def _update_bot_status(self, main: str="", grid: str="", pnl: str="", action: str=""):
        update_data = {}
        if main: update_data["main_status"] = main
        if grid: update_data["grid_status"] = grid
        if pnl: update_data["pnl_status"] = pnl
        if action: update_data["action_status"] = action
        self.app.gui_queue.put(("live_bot_status_update_display", update_data))

    def _simplify_reason(self, reason_str: str) -> str:
        if not reason_str: return "N/A"
        parts = []
        fibo_match = re.search(r"Fibo_(\d\.\d+).*?\((.*?)\)", reason_str)
        bb_match = re.search(r"bb_(upper|lower)\((.*?)\)", reason_str)
        extremum_match = re.search(r"extremum_(local_high|local_low)\((.*?)\)", reason_str)
        
        if fibo_match:
            level, tf = fibo_match.groups()
            parts.append(f"Фібо {level} {tf.split('_')[0]}")
        elif bb_match:
            band, tf_full = bb_match.groups()
            band_name = "Верх" if band == "upper" else "Низ"
            parts.append(f"BB {band_name} {tf_full.split('_')[0]}")
        elif extremum_match:
            ext_type, tf = extremum_match.groups()
            ext_name = "Макс" if ext_type == "local_high" else "Мін"
            parts.append(f"Лок.{ext_name} {tf}")

        sma_matches = re.findall(r"sma_(\d+)\((.*?)\)", reason_str)
        for period, tf in sma_matches:
            parts.append(f"SMA{period} {tf}")

        return "; ".join(parts) if parts else reason_str[:30]

    def start_managing_grid(self, potential_grid_orders: List[Dict[str, Any]], grid_params: Dict[str, Any]):
        self._log_to_gui("Запуск реального бота в режимі 'Тіні'...", "INFO")
        self.stop_managing_grid(cancel_active_orders=False)

        self.live_trading_active = True
        self.current_live_grid_params = grid_params
        self.partial_tp_cooldowns.clear()
        self.executed_partial_tp_levels_by_side["LONG"].clear()
        self.executed_partial_tp_levels_by_side["SHORT"].clear()
        self.last_order_placement_time = 0.0 
        
        self._update_bot_status(main="Статус: Активний (Тінь)", action="Дія: Слідкую за ЛПБ...")
        self._schedule_monitoring_tasks()

    def stop_managing_grid(self, cancel_active_orders: bool = True):
        self._log_to_gui("Зупинка реального бота...", "INFO")
        self.live_trading_active = False

        if self.monitor_timer:
            try: self.app.after_cancel(self.monitor_timer)
            except Exception: pass
            self.monitor_timer = None

        if cancel_active_orders:
            symbol = self.current_live_grid_params.get("symbol")
            if symbol and self.placed_orders_state:
                self._log_to_gui(f"Скасування активних ордерів бота для {symbol}...", "INFO")
                for order_state in list(self.placed_orders_state.values()):
                    if order_state.get("status") in [ORDER_STATUS_TRIGGERED, ORDER_STATUS_PLACED, "API: NEW"]:
                        real_order_id = order_state.get("real_order_id")
                        if real_order_id:
                            try:
                                self._log_to_gui(f"Скасовую ордер ID: {real_order_id}", "INFO")
                                self.api_handler.cancel_order(symbol=symbol, order_id_str=str(real_order_id))
                            except Exception as e:
                                self._log_to_gui(f"Помилка при скасуванні ордера {real_order_id}: {e}", "ERROR")
                        else:
                             self._log_to_gui(f"Неможливо скасувати ордер {order_state.get('manager_id')}, немає real_order_id.", "WARNING")

        self.placed_orders_state.clear()
        self.current_live_grid_params.clear()
        self.real_positions_by_side["LONG"].clear()
        self.real_positions_by_side["SHORT"].clear()
        self.partial_tp_cooldowns.clear()
        self.executed_partial_tp_levels_by_side["LONG"].clear()
        self.executed_partial_tp_levels_by_side["SHORT"].clear()
        
        self._update_bot_status(main="Статус: Неактивний", grid="Сітка: Неактивна", pnl="PNL: Не відстежується", action="Дія: Зупинено")
        self._send_managed_orders_to_gui()

    def _update_real_position_info(self):
        symbol = self.current_live_grid_params.get("symbol")
        if not symbol: return
        
        # Оновлюємо обидві сторони
        for side in ["LONG", "SHORT"]:
            try:
                if side == "LONG":
                    amt_str = self.app.live_long_pos_amt.get()
                    entry_str = self.app.live_long_pos_entry.get()
                else:
                    amt_str = self.app.live_short_pos_amt.get()
                    entry_str = self.app.live_short_pos_entry.get()
                
                if amt_str and amt_str != "0" and Decimal(amt_str) > 0 and entry_str and entry_str != "0":
                    pos_amt = Decimal(amt_str)
                    if side == "SHORT": pos_amt = -pos_amt
                    
                    if not self.real_positions_by_side[side]:
                        self.executed_partial_tp_levels_by_side[side].clear()
                        self._log_to_gui(f"Виявлено нову {side} позицію, скинуто рівні ТП.", "DEBUG")

                    self.real_positions_by_side[side] = {
                        "symbol": symbol, 
                        "positionAmt": pos_amt, 
                        "entryPrice": Decimal(entry_str), 
                        "positionSide": side
                    }
                else:
                    if self.real_positions_by_side[side]:
                        self.executed_partial_tp_levels_by_side[side].clear()
                        self._log_to_gui(f"{side} позицію закрито, скинуто рівні ТП.", "DEBUG")
                    self.real_positions_by_side[side].clear()
            except (ValueError, InvalidOperation):
                self.real_positions_by_side[side].clear()

    def _perform_monitoring_tasks_tick(self):
        """
        Метод для виклику через асинхронний heartbeat сервера.
        """
        if not self.live_trading_active: return
        
        try:
            self._update_real_position_info()
            self._synchronize_orders_with_lpb()
            
            pnl_long = self._get_pnl_percent_from_gui("LONG")
            pnl_short = self._get_pnl_percent_from_gui("SHORT")
            
            status_parts = []
            if self.real_positions_by_side["LONG"]:
                if pnl_long is not None:
                    status_parts.append(f"L: {pnl_long:.2f}%")
                    self._check_and_manage_tp_levels(pnl_long, "LONG")
            if self.real_positions_by_side["SHORT"]:
                if pnl_short is not None:
                    status_parts.append(f"S: {pnl_short:.2f}%")
                    self._check_and_manage_tp_levels(pnl_short, "SHORT")
            
            if status_parts:
                self._update_bot_status(pnl=f"PNL: {' | '.join(status_parts)}")
            else:
                self._update_bot_status(pnl="PNL: Позицій немає")

            self._send_managed_orders_to_gui()
        except Exception as e:
            self._log_to_gui(f"Помилка в циклі моніторингу: {e}", "ERROR")
            self._update_bot_status(main="Статус: Помилка!", action=f"Помилка: {e}")

    def _perform_monitoring_tasks(self):
        # Залишаємо для сумісності з GUI, якщо він ще використовує tkinter
        self._perform_monitoring_tasks_tick()
        self._schedule_monitoring_tasks()

    def _synchronize_orders_with_lpb(self):
        if not hasattr(self.app, 'live_bot_active') or not self.app.live_bot_active:
            self._update_bot_status(grid="Сітка: ЛПБ неактивний")
            if self.placed_orders_state:
                self.stop_managing_grid(cancel_active_orders=True)
            return

        try:
            market_price = Decimal(self.app.current_price_var.get())
            if market_price <= 0: return
        except (InvalidOperation, ValueError):
            return

        # [!!!] Перевіряємо, чи не активний кулдаун
        current_time = time.time()
        if (current_time - self.last_order_placement_time) < self.ORDER_PLACEMENT_COOLDOWN_SECONDS:
            self._update_bot_status(action=f"Дія: Охолодження ({int(self.ORDER_PLACEMENT_COOLDOWN_SECONDS - (current_time - self.last_order_placement_time))} сек)...")
            return

        lpb_orders = {o['order_id_sim']: o for o in self.app.live_bot_state.get('orders_data_virtual', []) if o.get('status') == 'pending_live_bot'}
        
        placed_v_ids = list(self.placed_orders_state.keys())
        for v_id in placed_v_ids:
            if v_id not in lpb_orders:
                order_to_cancel = self.placed_orders_state.get(v_id)
                if order_to_cancel and order_to_cancel.get("status") not in ["API: FILLED", "API: CANCELED", "API: REJECTED", ORDER_STATUS_FAILED_TO_PLACE, "API: CANCELED_PENDING"]:
                    real_order_id = order_to_cancel.get('real_order_id')
                    if real_order_id:
                        self._log_to_gui(f"ЛПБ скасував ордер {v_id}. Скасовую реальний ордер ID {real_order_id}", "INFO")
                        self.api_handler.cancel_order(symbol=order_to_cancel['symbol'], order_id_str=str(real_order_id))
                        order_to_cancel["status"] = "API: CANCELED_PENDING"
                    else:
                        if v_id in self.placed_orders_state:
                            del self.placed_orders_state[v_id]
            else:
                # [NEW] Check for worsening: if existing order price is now too far from LPB order price
                pinned_order = self.placed_orders_state.get(v_id)
                if pinned_order and pinned_order.get("status") == ORDER_STATUS_PLACED:
                    new_p = Decimal(lpb_orders[v_id]["price"])
                    old_p = Decimal(pinned_order["price"])
                    # If shifted more than 0.05%, cancel and let it re-trigger
                    if abs(new_p - old_p) / old_p > Decimal("0.0005"):
                        self._log_to_gui(f"Ордер {v_id} змістився ({old_p} -> {new_p}). Перевиставляю...", "INFO")
                        real_id = pinned_order.get("real_order_id")
                        if real_id:
                            self.api_handler.cancel_order(symbol=pinned_order['symbol'], order_id_str=str(real_id))
                            pinned_order["status"] = "API: CANCELED_PENDING"
                        else:
                            del self.placed_orders_state[v_id]
        
        self._update_bot_status(grid=f"Сітка: Слідкую за {len(lpb_orders)} ордерами ЛПБ...")
        
        candidate_to_place = None
        
        # Count existing parked orders to apply offsets progressively
        major_safety_count_by_side = {"LONG": 0, "SHORT": 0}
        minor_safety_count_by_side = {"LONG": 0, "SHORT": 0}
        
        for state in self.placed_orders_state.values():
            s_side = state.get("position_side", "LONG").upper()
            if state.get("status") == ORDER_STATUS_REPOSITIONED_MAJOR:
                major_safety_count_by_side[s_side] += 1
            elif state.get("status") == ORDER_STATUS_REPOSITIONED_MINOR:
                minor_safety_count_by_side[s_side] += 1

        for v_id, v_order in lpb_orders.items():
            # Skip if already in the process of being placed (triggered) or active on exchange
            pinned = self.placed_orders_state.get(v_id)
            if pinned and pinned.get("status") in [ORDER_STATUS_TRIGGERED, ORDER_STATUS_PLACED] or \
               pinned and "API:" in pinned.get("status", ""):
                continue

            # Skip if we already found a candidate to place in this tick
            if candidate_to_place: break

            try:
                order_price = Decimal(v_order["price"])
                side = "BUY" if Decimal(v_order.get("base_amount", 0)) > 0 else "SELL"
                # In hedge mode opening orders (grid dots), BUY=LONG, SELL=SHORT.
                pos_side_for_offset = "LONG" if side == "BUY" else "SHORT"
                
                is_worsening = False
                is_too_close = False
                is_above_market = False
                
                real_pos = self.real_positions_by_side.get(pos_side_for_offset, {})
                base_calc = market_price
                
                if real_pos and real_pos.get("entryPrice", Decimal(0)) > 0:
                    real_entry_price = real_pos.get("entryPrice", Decimal(0))
                    base_calc = min(real_entry_price, market_price) if pos_side_for_offset == "LONG" else max(real_entry_price, market_price)
                    
                    if (pos_side_for_offset == "LONG" and side == "BUY" and order_price > real_entry_price) or \
                       (pos_side_for_offset == "SHORT" and side == "SELL" and order_price < real_entry_price):
                        is_worsening = True
                    else:
                        if (pos_side_for_offset == "LONG" and side == "BUY" and order_price > market_price) or \
                           (pos_side_for_offset == "SHORT" and side == "SELL" and order_price < market_price):
                            is_above_market = True
                        
                        zone_limit = real_entry_price * (Decimal(1) - self.MINOR_SAFETY_ZONE_PERCENT / 100) if pos_side_for_offset == "LONG" else \
                                     real_entry_price * (Decimal(1) + self.MINOR_SAFETY_ZONE_PERCENT / 100)
                        if (pos_side_for_offset == "LONG" and order_price > zone_limit) or \
                           (pos_side_for_offset == "SHORT" and order_price < zone_limit):
                            is_too_close = True
                else:
                    # No position yet, block worsening relative to market price anyway
                    if side == "BUY" and order_price > market_price:
                        is_above_market = True
                    if side == "SELL" and order_price < market_price:
                        is_above_market = True

                # --- Park the order if it violates rules ---
                if is_worsening:
                    total_offset = self.MAJOR_SAFETY_OFFSET_BASE + (Decimal(major_safety_count_by_side[pos_side_for_offset]) * self.MAJOR_SAFETY_OFFSET_STEP)
                    new_price = base_calc * (Decimal(1) - total_offset / 100) if pos_side_for_offset == "LONG" else base_calc * (Decimal(1) + total_offset / 100)
                    
                    if not pinned or pinned.get("status") != ORDER_STATUS_REPOSITIONED_MAJOR:
                        self._log_to_gui(f"[SAFETY] Ордер {v_id[-8:]} ({side}) погіршує позицію {pos_side_for_offset}. Паркую на -{total_offset}% від входу.", "WARNING")

                    self.placed_orders_state[v_id] = {
                        "status": ORDER_STATUS_REPOSITIONED_MAJOR, 
                        **v_order,
                        "price": str(new_price), 
                        "gui_source_note": f"Безп(Погірш) -{total_offset}%"
                    }
                    major_safety_count_by_side[pos_side_for_offset] += 1
                    continue
                elif is_too_close or is_above_market:
                    total_offset = self.MINOR_SAFETY_OFFSET_BASE + (Decimal(minor_safety_count_by_side[pos_side_for_offset]) * self.MINOR_SAFETY_OFFSET_STEP)
                    new_price = base_calc * (Decimal(1) - total_offset / 100) if pos_side_for_offset == "LONG" else base_calc * (Decimal(1) + total_offset / 100)
                    reason = "Близько" if is_too_close and not is_above_market else "Вище ринку"
                    
                    if not pinned or pinned.get("status") != ORDER_STATUS_REPOSITIONED_MINOR:
                        self._log_to_gui(f"[SAFETY] Ордер {v_id[-8:]} ({side}) занадто {reason.lower()}. Паркую на -{total_offset}% від входу/ринку.", "WARNING")

                    self.placed_orders_state[v_id] = {
                        "status": ORDER_STATUS_REPOSITIONED_MINOR, 
                        **v_order,
                        "price": str(new_price), 
                        "gui_source_note": f"Безп({reason}) -{total_offset}%"
                    }
                    minor_safety_count_by_side[pos_side_for_offset] += 1
                    continue
                
                # --- Normal good order logic ---
                # If it was previously parked, but now it's good, we clear the parked state
                if pinned and pinned.get("status") in [ORDER_STATUS_REPOSITIONED_MAJOR, ORDER_STATUS_REPOSITIONED_MINOR]:
                    self._log_to_gui(f"[SAFETY] Ордер {v_id[-8:]} повернувся в безпечну зону. Повертаю до моніторингу.", "SUCCESS")
                    del self.placed_orders_state[v_id]
                    pinned = None
                trigger_dist_percent_str = self.current_live_grid_params.get("price_trigger_buffer_percent", "0.1")
                trigger_dist = order_price * Decimal(trigger_dist_percent_str) / 100

                is_triggered = False
                if (side == "BUY" and market_price <= order_price + trigger_dist) or \
                   (side == "SELL" and market_price >= order_price - trigger_dist):
                    is_triggered = True
                
                if is_triggered:
                    candidate_to_place = {
                        "v_id": v_id, "v_order": v_order, "order_price": order_price, 
                        "side": side, "pos_side": pos_side_for_offset
                    }
                    break

            except (KeyError, InvalidOperation, TypeError) as e:
                self._log_to_gui(f"Помилка обробки віртуального ордера {v_id}: {e}", "ERROR")
                continue
                
        # --- Check parked orders if no candidate was found ---
        if not candidate_to_place:
            for v_id, state in self.placed_orders_state.items():
                if state.get('status') in [ORDER_STATUS_REPOSITIONED_MAJOR, ORDER_STATUS_REPOSITIONED_MINOR]:
                    side = "BUY" if Decimal(state.get("base_amount", 0)) > 0 else "SELL"
                    parked_price = Decimal(state["price"])
                    
                    if (side == "BUY" and market_price <= parked_price) or \
                       (side == "SELL" and market_price >= parked_price):
                        self._log_to_gui(f"Ціна досягла страхувального ордера {v_id[-8:]}! Виставляю по {parked_price:.4f}", "WARNING")
                        candidate_to_place = {
                            "v_id": v_id, "v_order": state, "order_price": parked_price,
                            "side": side, "pos_side": state.get("position_side", "LONG").upper()
                        }
                        break
                        
        # --- Place the candidate ---
        if candidate_to_place:
            v_id = candidate_to_place["v_id"]
            v_order = candidate_to_place["v_order"]
            order_price = candidate_to_place["order_price"]
            side = candidate_to_place["side"]
            pos_side = candidate_to_place["pos_side"]
            
            self._update_bot_status(action=f"Дія: Виставляю ордер {pos_side} @{order_price}")
            client_order_id = f"gb_{self.app.selected_symbol.lower()[:4]}_{int(time.time() * 1000)}_{v_id[-4:]}"
            
            self.placed_orders_state[v_id] = {"status": ORDER_STATUS_TRIGGERED, "client_order_id": client_order_id, **v_order}
            
            self.api_handler.place_order(
                symbol=v_order["symbol"], 
                side=side, 
                order_type="LIMIT",
                quantity_str=str(abs(Decimal(v_order["base_amount"]))),
                price_str=str(order_price), 
                client_order_id_param=client_order_id,
                position_side_gui=pos_side,
                reduce_only_gui=False
            )
            
            self.last_order_placement_time = time.time()
            self.placed_orders_state[v_id]["status"] = ORDER_STATUS_PLACED
            self._log_to_gui(f"Ордер {v_id} відправлено. Активно охолодження (15с).", "INFO")

    def _get_pnl_percent_from_gui(self, side: str = "LONG") -> Optional[Decimal]:
        try:
            if side == "LONG":
                pnl_string = self.app.live_long_pos_pnl.get()
            else:
                pnl_string = self.app.live_short_pos_pnl.get()
                
            match = re.search(r'\(([^%]+) %\)', pnl_string)
            if match:
                return Decimal(match.group(1).strip())
            return None
        except (AttributeError, InvalidOperation, ValueError, TypeError):
            return None
            
    def _check_and_manage_tp_levels(self, current_pnl_percent: Decimal, side: str):
        pos_amt = self.real_positions_by_side[side].get("positionAmt", Decimal(0))
        if pos_amt == Decimal(0): return

        if not hasattr(self.app, 'live_trading_manager'): return
        rules = self.app.live_trading_manager.live_trading_rules.get("partial_tp_levels", [])
        if not rules: return
        rules.sort(key=lambda x: Decimal(x["pnl_threshold_percent"]))

        executed_levels = self.executed_partial_tp_levels_by_side[side]
        levels_to_reset = {lvl_id for lvl_id in executed_levels if current_pnl_percent < Decimal(lvl_id.split('_')[-1])}
        if levels_to_reset:
            self._log_to_gui(f"[{side}] PNL впав нижче рівнів: {levels_to_reset}. Рівні знову активні.", "DEBUG")
            for r_id in levels_to_reset:
                executed_levels.remove(r_id)

        current_time = time.time()
        for i, rule in enumerate(rules):
            try:
                pnl_threshold = Decimal(rule["pnl_threshold_percent"])
                rule_id = f"tp_level_{pnl_threshold}"

                if rule_id not in executed_levels and \
                   current_pnl_percent >= pnl_threshold and \
                   current_time > self.partial_tp_cooldowns.get(f"{side}_{rule_id}", 0.0):
                    
                    self._update_bot_status(action=f"[{side}] Дія: Фіксую прибуток ({pnl_threshold}%)")
                    self._log_to_gui(f"[{side}] Спрацював ТП! PNL {current_pnl_percent:.2f}% >= {pnl_threshold}%.", "SUCCESS")
                    
                    percent_to_close = Decimal(rule["close_percent_of_pos"])
                    quantity_to_close_abs = abs(pos_amt) * (percent_to_close / 100)
                    
                    min_qty_str = self.app.current_symbol_precision_info.get('min_quantity_str', '0.001')
                    is_last_rule = (i == len(rules) - 1)
                    if is_last_rule and (abs(pos_amt) - quantity_to_close_abs) < Decimal(min_qty_str):
                        quantity_to_close_abs = abs(pos_amt)

                    step_size = self.app.current_symbol_precision_info.get('quantity_precision_str', '0.001')
                    adjusted_quantity = adjust_quantity_to_step(
                        quantity=quantity_to_close_abs, step_size_str=step_size,
                        min_qty_str=min_qty_str, rounding_mode="UP"
                    )

                    if adjusted_quantity and adjusted_quantity > 0:
                        if adjusted_quantity > abs(pos_amt):
                            adjusted_quantity = abs(pos_amt)

                        actual_order_side = "SELL" if side == "LONG" else "BUY"
                        
                        self.api_handler.place_order(
                            symbol=self.real_positions_by_side[side]["symbol"], side=actual_order_side,
                            order_type="MARKET", quantity_str=str(adjusted_quantity),
                            reduce_only_gui=True, position_side_gui=side,
                            skip_local_validation=True
                        )
                        
                        self.partial_tp_cooldowns[f"{side}_{rule_id}"] = current_time + 300 
                        executed_levels.add(rule_id)
                        
                        self._log_to_gui(f"[{side}] Рівень ТП {pnl_threshold}% ВИКОНАНО.", "INFO")
                        break
            except (ValueError, InvalidOperation, KeyError):
                continue
                
    def process_binance_order_update(self, binance_order_data: Dict[str, Any]):
        client_order_id = binance_order_data.get('c')
        if not client_order_id: return
        
        v_id = next((vid for vid, state in self.placed_orders_state.items() if state.get("client_order_id") == client_order_id), None)
        if not v_id: return

        order_status_ws = binance_order_data.get('X')
        real_order_id = binance_order_data.get('i')
        
        if v_id in self.placed_orders_state:
            if real_order_id: self.placed_orders_state[v_id]['real_order_id'] = real_order_id
            self.placed_orders_state[v_id]['status'] = f"API: {order_status_ws}"
            if order_status_ws in ["FILLED", "CANCELED", "EXPIRED", "REJECTED"]:
                log_level = "SUCCESS" if order_status_ws == "FILLED" else "WARNING"
                self._log_to_gui(f"Керований ордер {client_order_id} завершено. Статус: {order_status_ws}", log_level)
                del self.placed_orders_state[v_id]
        
        self._send_managed_orders_to_gui()

    def process_order_placement_failed(self, failed_order_params: Dict[str, Any], error_message: str):
        client_order_id = failed_order_params.get("newClientOrderId") or failed_order_params.get("clientOrderId")
        if not client_order_id: return

        v_id = next((vid for vid, state in self.placed_orders_state.items() if state.get("client_order_id") == client_order_id), None)
        if v_id and v_id in self.placed_orders_state:
            self.placed_orders_state[v_id]['status'] = ORDER_STATUS_FAILED_TO_PLACE
            self.placed_orders_state[v_id]['error_message'] = error_message
            self._log_to_gui(f"Керований ордер {client_order_id} ВІДХИЛЕНО: {error_message}", "ERROR")
            self._send_managed_orders_to_gui()

    def _send_managed_orders_to_gui(self):
        orders_for_gui = []
        if not (hasattr(self.app, 'live_bot_state') and self.app.live_bot_active):
            if self.live_trading_active:
                self.app.gui_queue.put(("update_live_managed_grid_orders_display", []))
            return
        lpb_orders = self.app.live_bot_state.get('orders_data_virtual', [])
        
        for v_id, state in self.placed_orders_state.items():
            status_for_gui = state.get("status", ORDER_STATUS_MONITORING)
            gui_source_note = state.get("gui_source_note")
            reason = gui_source_note or self._simplify_reason(state.get("reason", ""))
            
            orders_for_gui.append({
                "manager_id": v_id,
                "side": "BUY" if Decimal(state.get("base_amount", 0)) > 0 else "SELL",
                "price": float(Decimal(state.get("price", 0))),
                "quantity": float(abs(Decimal(state.get("base_amount", 0)))),
                "margin_usd": float(Decimal(state.get("margin", 0))),
                "source": reason,
                "status": status_for_gui,
            })
            
        for v_order in lpb_orders:
            v_id = v_order.get("order_id_sim")
            if v_id and v_id not in self.placed_orders_state:
                orders_for_gui.append({
                    "manager_id": v_id,
                    "side": "BUY" if Decimal(v_order.get("base_amount", 0)) > 0 else "SELL",
                    "price": float(Decimal(v_order.get("price", 0))),
                    "quantity": float(abs(Decimal(v_order.get("base_amount", 0)))),
                    "margin_usd": float(Decimal(v_order.get("margin", 0))),
                    "source": self._simplify_reason(v_order.get("reason", "")),
                    "status": ORDER_STATUS_MONITORING,
                })
        
        for side in ["LONG", "SHORT"]:
            if self.real_positions_by_side[side]:
                if hasattr(self.app, 'live_trading_manager'):
                    rules = self.app.live_trading_manager.live_trading_rules.get("partial_tp_levels", [])
                    if rules:
                        current_time = time.time()
                        for rule in sorted(rules, key=lambda x: Decimal(x["pnl_threshold_percent"])):
                            try:
                                pnl_threshold = Decimal(rule["pnl_threshold_percent"])
                                rule_id = f"tp_level_{pnl_threshold}"
                                
                                if rule_id in self.executed_partial_tp_levels_by_side[side]: continue
                                
                                if current_time > self.partial_tp_cooldowns.get(f"{side}_{rule_id}", 0.0):
                                    percent_to_close = rule["close_percent_of_pos"]
                                    orders_for_gui.append({
                                        "manager_id": f"tp_info_{side}_{rule_id}",
                                        "side": f"TP {side}",
                                        "price": 0.0,
                                        "quantity": 0.0,
                                        "margin_usd": 0.0,
                                        "source": f"PNL >= {pnl_threshold}% (Закр. {percent_to_close}%)",
                                        "status": ORDER_STATUS_TP,
                                    })
                                    break
                            except (ValueError, InvalidOperation, KeyError):
                                continue

        self.app.gui_queue.put(("update_live_managed_grid_orders_display", orders_for_gui))

    def _schedule_monitoring_tasks(self):
        if self.monitor_timer:
            try: self.app.after_cancel(self.monitor_timer)
            except Exception: pass
        if self.live_trading_active:
            self.monitor_timer = self.app.after(self.MONITOR_INTERVAL_MS, self._perform_monitoring_tasks)