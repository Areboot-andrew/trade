# BOTV1/grid_generator_module.py
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP, Context
import uuid
import time
from copy import deepcopy
from typing import Dict, List, Any, Optional, Tuple

from market_analyzer_module import MarketAnalyzer # type: ignore
from utils import quantize_decimal # type: ignore
from constants import DEFAULT_M15_TF_ID, DEFAULT_H1_TF_ID # type: ignore
def check_and_adjust_margin_for_capital_limit(app, requested_margin, is_emergency_order=False):
    # Dummy implementation replacing the old simulation_calculations logic.
    # In live mode, capital limits are currently not strictly enforced per order here.
    return requested_margin

from asset_quantization_module import (
    get_symbol_precision_info,
    calculate_base_from_quote,
)

class GridGenerator:
    def __init__(self, market_analyzer: MarketAnalyzer, app_instance: Any, strategy_settings: Dict[str, Any]):
        self.analyzer = market_analyzer
        self.app = app_instance
        self.strategy_settings = strategy_settings
        self.asset_precisions_data: Dict[str, Dict[str, str]] = getattr(app_instance, 'asset_precisions_data', {})
        if not self.asset_precisions_data and hasattr(app_instance, 'add_sim_log'):
            app_instance.add_sim_log("GridGenerator WARN: asset_precisions_data is empty during init.")


    def update_config(self, new_strategy_settings: Dict[str, Any]):
        self.strategy_settings = new_strategy_settings
        if hasattr(self.app, 'asset_precisions_data'):
            self.asset_precisions_data = self.app.asset_precisions_data


    def get_price_precision(self, symbol: Optional[str]) -> str:
        if not symbol: return '0.00000001'
        symbol_info = get_symbol_precision_info(symbol, self.asset_precisions_data)
        if symbol_info and "price_precision_str" in symbol_info:
            return symbol_info["price_precision_str"]
        if "USDT" in symbol: return '0.01' if symbol.startswith("BTC") or symbol.startswith("ETH") else '0.0001'
        return '0.00000001'

    def get_quantity_precision_str(self, symbol: Optional[str]) -> str:
        if not symbol: return '0.001'
        symbol_info = get_symbol_precision_info(symbol, self.asset_precisions_data)
        if symbol_info and "quantity_precision_str" in symbol_info:
            return symbol_info["quantity_precision_str"]
        if "USDT" in symbol: return '0.00001' if symbol.startswith("BTC") else '0.001'
        return '0.001'

    def _get_dynamic_multipliers_for_grid(self) -> tuple[Decimal, Decimal, Decimal]:
        price_velocity = getattr(self.app, 'current_price_velocity_percent', Decimal(0))
        vr_conf = self.strategy_settings.get("volatility_reaction", {})
        pvc_conf = vr_conf.get("price_velocity_config", {})
        strong_move_thresh = Decimal(str(pvc_conf.get("strong_move_threshold_percent", "0.3")))

        gen_grid_conf = self.strategy_settings.get("general_grid_settings", {})
        base_cluster_mult = Decimal(str(gen_grid_conf.get("atr_multiplier_cluster_spread", "0.15")))

        gte_conf = self.strategy_settings.get("grid_timeframe_escalation", {})
        base_distance_mult = Decimal(str(gte_conf.get("atr_multiplier_min_distance_groups_default", "0.4")))

        final_cluster_mult, final_distance_mult = base_cluster_mult, base_distance_mult
        margin_adj_factor = Decimal("1.0")

        if abs(price_velocity) > strong_move_thresh:
            cluster_adj_conf = vr_conf.get("dynamic_cluster_weights_on_strong_move", {})
            reduction_factor_str = str(cluster_adj_conf.get("cluster_spread_reduction_factor_on_strong_move", "0.8"))
            final_cluster_mult = base_cluster_mult * Decimal(reduction_factor_str)

            dyn_spread_conf = vr_conf.get("dynamic_spread_on_volatility", {})
            if dyn_spread_conf.get("enabled", False):
                distance_factor_str = str(dyn_spread_conf.get("atr_distance_multiplier_on_high_proc", "1.2"))
                final_distance_mult = base_distance_mult * Decimal(distance_factor_str)
            else:
                final_distance_mult = base_distance_mult * Decimal("1.1")

        final_cluster_mult = max(final_cluster_mult, Decimal("0.01"))
        final_distance_mult = max(final_distance_mult, Decimal("0.05"))

        return final_cluster_mult.quantize(Decimal("0.001")), \
               final_distance_mult.quantize(Decimal("0.001")), \
               margin_adj_factor.quantize(Decimal("0.01"))


    def _get_base_weight_for_level_type(self, level_type_str: str) -> Decimal:
        level_weights_conf = self.strategy_settings.get("level_weights_by_type", {})
        if level_type_str in level_weights_conf:
            return Decimal(str(level_weights_conf[level_type_str]))
        
        if level_type_str.startswith("Fibo_"):
            specific_fibo_key = level_type_str 
            if "_RetrUp" in specific_fibo_key: specific_fibo_key = specific_fibo_key.replace("_RetrUp","") 
            if "_RetrDown" in specific_fibo_key: specific_fibo_key = specific_fibo_key.replace("_RetrDown","") 
            if specific_fibo_key in level_weights_conf: return Decimal(str(level_weights_conf[specific_fibo_key]))
            return Decimal(str(level_weights_conf.get("Fibo_*", "0.7"))) 
        if level_type_str.startswith("sma_"):
            return Decimal(str(level_weights_conf.get("SMA_*", "0.6")))
        if level_type_str.startswith("bb_"): 
            return Decimal(str(level_weights_conf.get("BB_*", "0.8")))
        if level_type_str.startswith("ichimoku_"):
            return Decimal(str(level_weights_conf.get("Ichimoku_*", "0.65")))
        if level_type_str.startswith("extremum_local_"):
            return Decimal(str(level_weights_conf.get("Extremum_*", "0.65")))
        
        return Decimal("0.5")

    def _get_clean_order_for_deepcopy(self, order_dict: dict) -> dict:
        if not isinstance(order_dict, dict):
            return order_dict 

        safe_keys = [
            "price", "margin", "reason", "base_amount", "notional_leveraged",
            "status", "order_id_sim", "group_id", "is_main_grid_order",
            "timestamp_created", "order_in_group_index", "tf_id_source",
            "executed_price", "timestamp_executed" 
        ]
        cleaned_order = {}
        for k, v in order_dict.items():
            if k in safe_keys:
                if k in ["price", "margin", "base_amount", "notional_leveraged", "executed_price"]:
                    if isinstance(v, Decimal):
                        cleaned_order[k] = v
                    elif v is not None: 
                        try:
                            cleaned_order[k] = Decimal(str(v))
                        except (InvalidOperation, TypeError):
                            if hasattr(self.app, 'add_sim_log'):
                                self.app.add_sim_log(f"Error converting field {k} to Decimal in _get_clean_order_for_deepcopy. Value: {v}, Type: {type(v)}. Order ID: {order_dict.get('order_id_sim', 'N/A')}")
                            cleaned_order[k] = Decimal(0) 
                    else:
                        cleaned_order[k] = None 
                elif isinstance(v, (str, int, float, bool, type(None))): 
                     cleaned_order[k] = v
                else: 
                    try:
                        if not hasattr(v, 'winfo_exists'): 
                            cleaned_order[k] = str(v) 
                    except:
                        pass
        return cleaned_order

    def _collect_and_prioritize_all_levels(self,
                                         reference_price: Decimal,
                                         position_type: str,
                                         symbol: Optional[str],
                                         allowed_level_types_hrz: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        all_potential_levels = []
        level_sourcing_conf = self.strategy_settings.get("level_sourcing_and_processing", {})
        fib_conf = level_sourcing_conf.get("fibonacci_levels", {})
        gte_conf = self.strategy_settings.get("grid_timeframe_escalation", {})
        confluence_conf = level_sourcing_conf.get("confluence_settings", {})
        strong_level_types_for_confluence = set(confluence_conf.get("strong_level_types_for_confluence", []))

        if hasattr(self.app, 'add_sim_log'):
            self.app.add_sim_log(f"LevelCollection: Starting. RefPrice: {reference_price}, PosType: {position_type}, HRZ Allowed: {allowed_level_types_hrz or 'All'}")

        processed_tf_for_ta_levels = set()
        for tf_cfg_entry in gte_conf.get("timeframes_config", []):
            tf_id = tf_cfg_entry.get("timeframe_id")
            if not tf_id or tf_id in processed_tf_for_ta_levels: continue 

            tf_analysis = self.analyzer.get_analysis_for_tf(tf_id)
            if not tf_analysis or tf_analysis.get("error"):
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"LevelCollection: Skipping TF {tf_id} due to analysis error or no data.")
                continue

            indicator_keys = ["bb_upper", "bb_middle", "bb_lower",
                              "extremum_local_high", "extremum_local_low",
                              "ichimoku_span_a", "ichimoku_span_b", "ichimoku_tenkan", "ichimoku_kijun"]
            for sma_period in tf_cfg_entry.get("sma_periods", []):
                if isinstance(sma_period, int) and sma_period > 0 :
                    indicator_keys.append(f"sma_{sma_period}")

            for level_key in indicator_keys:
                if allowed_level_types_hrz and level_key not in allowed_level_types_hrz and \
                   not any(level_key.startswith(allowed_prefix.replace("_*", "")) for allowed_prefix in allowed_level_types_hrz if allowed_prefix.endswith("_*")):
                    continue

                level_price = tf_analysis.get(level_key)
                if level_price and isinstance(level_price, Decimal) and level_price > 0:
                    is_relevant_for_direction = False
                    if position_type == "Long" and level_price < reference_price: is_relevant_for_direction = True
                    elif position_type == "Short" and level_price > reference_price: is_relevant_for_direction = True
                    
                    if is_relevant_for_direction:
                        all_potential_levels.append({
                            "price": level_price, "type": level_key, "source_tf": tf_id,
                            "base_weight": self._get_base_weight_for_level_type(level_key), 
                            "reason": f"{level_key}({tf_id})"
                        })
            processed_tf_for_ta_levels.add(tf_id)
        
        if fib_conf.get("enabled", False):
            for fib_tf_id in fib_conf.get("source_timeframes", []):
                tf_analysis_for_fibo = self.analyzer.get_analysis_for_tf(fib_tf_id)
                if not tf_analysis_for_fibo or tf_analysis_for_fibo.get("error"): continue
                
                for key, fib_price in tf_analysis_for_fibo.items():
                    if key.startswith("Fibo_"): 
                        normalized_fibo_key = key.split("_Retr")[0] 
                        if allowed_level_types_hrz and key not in allowed_level_types_hrz and \
                           normalized_fibo_key not in allowed_level_types_hrz and \
                           "Fibo_*" not in allowed_level_types_hrz: 
                            continue

                        if isinstance(fib_price, Decimal) and fib_price > 0:
                            is_relevant_for_direction = False
                            if position_type == "Long" and fib_price < reference_price: is_relevant_for_direction = True
                            elif position_type == "Short" and fib_price > reference_price: is_relevant_for_direction = True
                            
                            if is_relevant_for_direction:
                                 all_potential_levels.append({
                                    "price": fib_price, "type": key, 
                                    "source_tf": fib_tf_id,
                                    "base_weight": self._get_base_weight_for_level_type(key), 
                                    "reason": f"{key}({fib_tf_id})"
                                })
        
        if hasattr(self.app, 'add_sim_log'):
            self.app.add_sim_log(f"LevelCollection: Collected {len(all_potential_levels)} raw potential levels.")

        if not all_potential_levels: return []
        all_potential_levels.sort(key=lambda x: x["price"], reverse=(position_type == "Long"))

        aggregated_zones = []
        aggregation_conf = level_sourcing_conf.get("level_aggregation", {})
        if not aggregation_conf.get("enabled", False) or not all_potential_levels:
            if hasattr(self.app, 'add_sim_log'):
                self.app.add_sim_log("LevelAggregation: Disabled or no potential levels. Using raw levels as zones.")
            for level in all_potential_levels:
                aggregated_zones.append({
                    "price_zone_center": level["price"], 
                    "aggregated_weight": level["base_weight"], 
                    "confluence_score": Decimal("1.0"), 
                    "zone_strength_score": level["base_weight"], 
                    "source_details": level["reason"], 
                    "original_levels_in_zone": [level] 
                })
        else: 
            if hasattr(self.app, 'add_sim_log'):
                self.app.add_sim_log("LevelAggregation: Starting aggregation.")
            proximity_threshold_type = aggregation_conf.get("proximity_threshold_type", "atr_multiplier")
            proximity_threshold_value = Decimal(0)
            
            primary_tf_atr_id = aggregation_conf.get("primary_tf_for_atr_proximity", DEFAULT_M15_TF_ID)
            primary_tf_analysis = self.analyzer.get_analysis_for_tf(primary_tf_atr_id)
            proximity_atr = primary_tf_analysis.get('atr', reference_price * Decimal('0.001')) if primary_tf_analysis and not primary_tf_analysis.get("error") else reference_price * Decimal('0.001')
            if proximity_atr <= Decimal(0): proximity_atr = reference_price * Decimal('0.001') 

            if proximity_threshold_type == "percentage_of_price":
                threshold_val_perc = Decimal(str(aggregation_conf.get("proximity_threshold_percentage_of_price", "0.1")))
                proximity_threshold_value = reference_price * (threshold_val_perc / Decimal(100))
            elif proximity_threshold_type == "atr_multiplier":
                threshold_val_atr_mult = Decimal(str(aggregation_conf.get("proximity_threshold_value_atr_mult", "0.2")))
                proximity_threshold_value = proximity_atr * threshold_val_atr_mult
            
            if hasattr(self.app, 'add_sim_log'):
                self.app.add_sim_log(f"LevelAggregation: Proximity ATR ({primary_tf_atr_id}): {proximity_atr:.4f}, Threshold Type: {proximity_threshold_type}, Proximity Value: {proximity_threshold_value:.4f}")

            if proximity_threshold_value <= Decimal(0): 
                 if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log("LevelAggregation: Proximity threshold is zero or negative. Using raw levels as zones.")
                 for level in all_potential_levels: 
                    aggregated_zones.append({
                        "price_zone_center": level["price"], "aggregated_weight": level["base_weight"],
                        "confluence_score": Decimal("1.0"), "zone_strength_score": level["base_weight"],
                        "source_details": level["reason"], "original_levels_in_zone": [level]
                    })
            else: 
                current_zone_levels = []
                for level in all_potential_levels:
                    if not current_zone_levels: 
                        current_zone_levels.append(level)
                    else:
                        boundary_price_in_zone = current_zone_levels[-1]["price"] 

                        if abs(level["price"] - boundary_price_in_zone) <= proximity_threshold_value:
                            current_zone_levels.append(level) 
                        else: 
                            if current_zone_levels: 
                                zone_data = self._process_aggregated_zone(current_zone_levels, aggregation_conf, confluence_conf, strong_level_types_for_confluence)
                                aggregated_zones.append(zone_data)
                                if hasattr(self.app, 'add_sim_log'):
                                    self.app.add_sim_log(f"LevelAggregation: Processed zone around {zone_data['price_zone_center']:.4f}, Strength: {zone_data['zone_strength_score']:.2f}, Levels: {len(current_zone_levels)}")
                            current_zone_levels = [level] 
                
                if current_zone_levels: 
                    zone_data = self._process_aggregated_zone(current_zone_levels, aggregation_conf, confluence_conf, strong_level_types_for_confluence)
                    aggregated_zones.append(zone_data)
                    if hasattr(self.app, 'add_sim_log'):
                        self.app.add_sim_log(f"LevelAggregation: Processed final zone around {zone_data['price_zone_center']:.4f}, Strength: {zone_data['zone_strength_score']:.2f}, Levels: {len(current_zone_levels)}")
        
        min_strength_filter = Decimal(str(aggregation_conf.get("min_zone_strength_to_place_order", "0.0")))
        if min_strength_filter > 0:
            zones_before_filter_count = len(aggregated_zones)
            aggregated_zones = [zone for zone in aggregated_zones if zone.get("zone_strength_score", Decimal(0)) >= min_strength_filter]
            if hasattr(self.app, 'add_sim_log'):
                self.app.add_sim_log(f"LevelFiltering: Filtered by min_strength_score ({min_strength_filter}). Zones count: {zones_before_filter_count} -> {len(aggregated_zones)}")
                for idx, zone in enumerate(aggregated_zones[:3]): 
                     self.app.add_sim_log(f"  Top Zone {idx+1}: Price {zone['price_zone_center']:.4f}, Strength {zone['zone_strength_score']:.2f}, Details: {zone['source_details']}")

        if position_type == "Long": 
            aggregated_zones.sort(key=lambda x: (x.get("zone_strength_score", Decimal(0)), -x["price_zone_center"]), reverse=True)
        else: 
            aggregated_zones.sort(key=lambda x: (x.get("zone_strength_score", Decimal(0)), x["price_zone_center"]), reverse=True)
        
        if hasattr(self.app, 'add_sim_log'):
            self.app.add_sim_log(f"LevelPrioritization: Final {len(aggregated_zones)} zones prioritized.")
            if aggregated_zones:
                 self.app.add_sim_log(f"  Top prioritized zone: Price {aggregated_zones[0]['price_zone_center']:.4f}, Strength {aggregated_zones[0]['zone_strength_score']:.2f}, Details: {aggregated_zones[0]['source_details']}")

        return aggregated_zones

    def _process_aggregated_zone(self, zone_levels: List[Dict[str, Any]], aggregation_conf: Dict[str, Any], confluence_conf: Dict[str, Any], strong_level_types: set) -> Dict[str, Any]:
        if hasattr(self.app, 'add_sim_log') and len(zone_levels) > 1:
            level_details_for_log = ", ".join([f"{lvl['type']}({lvl['source_tf']})@{lvl['price']:.4f}" for lvl in zone_levels])
            self.app.add_sim_log(f"ZoneProcess: Aggregating {len(zone_levels)} levels: [{level_details_for_log}]")

        total_weight_in_zone = sum(l['base_weight'] for l in zone_levels)
        zone_center = sum(l['price'] * l['base_weight'] for l in zone_levels) / total_weight_in_zone if total_weight_in_zone > 0 else zone_levels[0]['price']
        
        agg_weight_method = aggregation_conf.get("weight_aggregation_method", "sum")
        aggregated_weight = Decimal(0)
        if agg_weight_method == "sum": aggregated_weight = total_weight_in_zone
        elif agg_weight_method == "max": aggregated_weight = max(l['base_weight'] for l in zone_levels) if zone_levels else Decimal(0)
        elif agg_weight_method == "sum_capped":
            aggregated_weight = total_weight_in_zone
            max_cap = Decimal(str(aggregation_conf.get("max_aggregated_weight", "1.5")))
            aggregated_weight = min(aggregated_weight, max_cap)
        
        confluence_score_multiplier = Decimal("1.0") 
        if confluence_conf.get("enabled", False):
            unique_strong_types_in_zone = set()
            for lvl in zone_levels:
                level_type = lvl.get("type", "")
                normalized_lvl_type = level_type
                if level_type.startswith("sma_"): normalized_lvl_type = "sma_*"
                elif level_type.startswith("bb_"): normalized_lvl_type = "bb_*"
                elif level_type.startswith("Fibo_"): normalized_lvl_type = "Fibo_*"
                elif level_type.startswith("ichimoku_"): normalized_lvl_type = "ichimoku_*"
                elif level_type.startswith("extremum_local_"): normalized_lvl_type = "extremum_local_*"
                
                if level_type in strong_level_types or normalized_lvl_type in strong_level_types:
                    unique_strong_types_in_zone.add(level_type) 
            
            min_unique_for_boost = confluence_conf.get("min_unique_level_types_for_boost", 2)
            if len(unique_strong_types_in_zone) >= min_unique_for_boost:
                confluence_score_multiplier = Decimal(str(confluence_conf.get("confluence_boost_factor", "1.2")))
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"  ZoneConfluence: Achieved boost x{confluence_score_multiplier} with types: {unique_strong_types_in_zone}")

        zone_strength_score = aggregated_weight * confluence_score_multiplier

        source_details_str = "; ".join(f"{l['reason']}(w:{l['base_weight']:.2f})" for l in zone_levels)
        return {
            "price_zone_center": zone_center, 
            "aggregated_weight": aggregated_weight, 
            "confluence_score": confluence_score_multiplier, 
            "zone_strength_score": zone_strength_score, 
            "source_details": source_details_str[:100], 
            "original_levels_in_zone": deepcopy(zone_levels) 
        }

    def _get_macd_action_and_modifiers(self, position_type: str) -> tuple[List[str], Decimal]:
        macd_ctrl_conf = self.strategy_settings.get("macd_control_config", {})
        actions_to_take = []
        margin_modifier = Decimal("1.0")

        if not macd_ctrl_conf.get("enabled", False):
            return actions_to_take, margin_modifier

        control_tf_id = macd_ctrl_conf.get("control_timeframe_id", DEFAULT_M15_TF_ID)
        macd_analysis = self.analyzer.get_analysis_for_tf(control_tf_id)

        if not macd_analysis or macd_analysis.get("error"):
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"MACD Control: No analysis for {control_tf_id}. Error: {macd_analysis.get('error', 'N/A') if macd_analysis else 'None'}")
            return actions_to_take, margin_modifier

        macd_line = macd_analysis.get('macd_line', Decimal(0))
        macd_signal_line = macd_analysis.get('macd_signal', Decimal(0))
        macd_hist = macd_analysis.get('macd_hist', Decimal(0))
        
        for condition_entry in macd_ctrl_conf.get("conditions", []):
            signal_type_config = condition_entry.get("macd_line_vs_signal")
            hist_thresh_abs = Decimal(str(condition_entry.get("histogram_threshold_absolute", "0")))
            
            signal_detected = False
            if signal_type_config == "cross_down_strong": 
                if macd_line < macd_signal_line and macd_hist < -hist_thresh_abs: 
                    signal_detected = True
                    if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"MACD Signal: Tentative 'cross_down_strong' on {control_tf_id} (MACD: {macd_line:.4f}, Sig: {macd_signal_line:.4f}, Hist: {macd_hist:.4f})")
            elif signal_type_config == "cross_up_strong": 
                if macd_line > macd_signal_line and macd_hist > hist_thresh_abs: 
                    signal_detected = True
                    if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"MACD Signal: Tentative 'cross_up_strong' on {control_tf_id} (MACD: {macd_line:.4f}, Sig: {macd_signal_line:.4f}, Hist: {macd_hist:.4f})")
            
            if signal_detected:
                action_key = "action_if_signal_for_long" if position_type == "Long" else "action_if_signal_for_short"
                action_str_config = condition_entry.get(action_key)
                if action_str_config:
                    if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"MACD Action for {position_type}: '{action_str_config}' based on '{condition_entry.get('condition_name')}'")
                    parsed_actions = action_str_config.split('_') 
                    for pa_token in parsed_actions:
                        actions_to_take.append(pa_token) 
                        if ":" in pa_token: 
                            action_name, action_value_str = pa_token.split(":", 1)
                            try:
                                action_value_dec = Decimal(action_value_str)
                                if "margin_factor" in action_name: 
                                    margin_modifier *= action_value_dec 
                                    if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"  MACD: Margin modifier updated to {margin_modifier} by '{action_name}' (value: {action_value_dec})")
                            except InvalidOperation:
                                if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"  MACD: Non-decimal value for action '{action_name}': {action_value_str}")
        
        if not actions_to_take and hasattr(self.app, 'add_sim_log'): 
            self.app.add_sim_log(f"MACD Control: No specific MACD actions triggered on {control_tf_id}.")
            
        return actions_to_take, margin_modifier.quantize(Decimal("0.01"))

    # --- ПОЧАТОК ЗМІН ---
    def _calculate_dynamic_order_price(self, base_price: Decimal, level_type_pattern: str, 
                                       position_type: str, atr_for_level_tf: Decimal,
                                       is_strong_uptrend: bool, is_strong_downtrend: bool, 
                                       is_high_volatility: bool,
                                       current_market_price_for_calc: Decimal # Доданий аргумент
                                       ) -> Decimal:
    # --- КІНЕЦЬ ЗМІН ---
        dyn_placement_conf = self.strategy_settings.get("dynamic_level_placement", {})
        if not dyn_placement_conf.get("enabled", False) or atr_for_level_tf <= Decimal(0):
            return base_price 

        final_offset_atr_multiplier = Decimal(str(dyn_placement_conf.get("default_offset_percent_atr", "0.0")))
        factors_conf = dyn_placement_conf.get("factors_config_by_level_type_pattern", {})
        
        pattern_key_to_use = None
        if level_type_pattern in factors_conf: 
            pattern_key_to_use = level_type_pattern
        else: 
            if level_type_pattern.startswith("sma_") and "SMA_*" in factors_conf: pattern_key_to_use = "SMA_*" 
            elif (level_type_pattern.startswith("bb_") or level_type_pattern == "bb_middle") and "BB_*" in factors_conf: pattern_key_to_use = "BB_*"
            elif level_type_pattern.startswith("Fibo_") and "Fibo_*" in factors_conf: pattern_key_to_use = "Fibo_*"
            elif level_type_pattern.startswith("Extremum_") and "Extremum_*" in factors_conf: pattern_key_to_use = "Extremum_*"
        
        if pattern_key_to_use: 
            pattern_mods = factors_conf[pattern_key_to_use]
            trend_shift_str = str(pattern_mods.get("strong_trend_modifier_atr_shift", "0"))
            vol_shift_str = str(pattern_mods.get("high_volatility_modifier_atr_shift", "0"))
            
            if (position_type == "Long" and is_strong_uptrend) or \
               (position_type == "Short" and is_strong_downtrend): 
                final_offset_atr_multiplier += Decimal(trend_shift_str) 
            
            if is_high_volatility:
                final_offset_atr_multiplier += Decimal(vol_shift_str) 
        
        price_offset = atr_for_level_tf * final_offset_atr_multiplier
        adjusted_price = base_price - price_offset if position_type == "Long" else base_price + price_offset
        
        price_prec = self.get_price_precision(self.app.selected_symbol)
        final_price = quantize_decimal(max(Decimal(0), adjusted_price), price_prec) 

        if final_price != base_price and hasattr(self.app, 'add_sim_log'):
             self.app.add_sim_log(f"DynamicPrice: Level '{level_type_pattern}' price {base_price} adjusted to {final_price} (OffsetATRMult: {final_offset_atr_multiplier:.3f}, Level ATR: {atr_for_level_tf:.4f}, Trend: U:{is_strong_uptrend}/D:{is_strong_downtrend}, Vol: H:{is_high_volatility})")
        return final_price

    def _calculate_dynamic_margin_for_zone(self, 
                                           base_margin_for_this_step: Decimal, 
                                           zone_data: dict, 
                                           current_market_price: Decimal, 
                                           position_type: str,
                                           macd_overall_margin_modifier: Decimal, 
                                           hrz_margin_reduction_factor: Decimal 
                                           ) -> Decimal:
        dyn_margin_conf = self.strategy_settings.get("dynamic_margin_logic", {})
        if not dyn_margin_conf.get("enabled", False): 
            modified_margin = base_margin_for_this_step * macd_overall_margin_modifier * hrz_margin_reduction_factor
            return modified_margin.quantize(Decimal("0.01"))

        calculated_margin = base_margin_for_this_step 

        strength_influence_conf = dyn_margin_conf.get("level_strength_influence", {})
        if strength_influence_conf.get("enabled", False):
            zone_strength = zone_data.get("zone_strength_score", Decimal(0.5)) 
            strength_bands = strength_influence_conf.get("strength_bands_multipliers", [])
            
            applied_strength_multiplier = Decimal("1.0")
            sorted_strength_bands = sorted(strength_bands, key=lambda x: Decimal(str(x.get("strength_threshold", "0"))))

            for band in sorted_strength_bands:
                threshold = Decimal(str(band.get("strength_threshold", "0")))
                multiplier = Decimal(str(band.get("margin_multiplier", "1.0")))
                if zone_strength <= threshold: 
                    applied_strength_multiplier = multiplier
                    break 
            else: 
                if sorted_strength_bands: 
                    applied_strength_multiplier = Decimal(str(sorted_strength_bands[-1].get("margin_multiplier", "1.0")))
            
            if applied_strength_multiplier != Decimal("1.0"):
                calculated_margin *= applied_strength_multiplier
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"DynamicMargin (Strength): Zone '{zone_data.get('source_details', 'N/A')[:30]}' strength {zone_strength:.2f}, margin modified by x{applied_strength_multiplier:.2f}. New: {calculated_margin:.2f}")

        dist_influence_conf = dyn_margin_conf.get("distance_from_market_influence", {})
        if dist_influence_conf.get("enabled", False):
            zone_center_price = zone_data.get("price_zone_center", current_market_price)
            distance_abs = abs(current_market_price - zone_center_price)
            distance_metric = Decimal(0) 

            if dist_influence_conf.get("use_atr_for_distance_bands", True):
                atr_tf_id = dist_influence_conf.get("distance_atr_source_tf", DEFAULT_M15_TF_ID)
                atr_analysis = self.analyzer.get_analysis_for_tf(atr_tf_id)
                atr_val = atr_analysis.get('atr', current_market_price * Decimal('0.01')) if atr_analysis and not atr_analysis.get("error") else current_market_price * Decimal('0.01')
                if atr_val <= Decimal(0): atr_val = current_market_price * Decimal('0.001') 
                if atr_val > 0 : distance_metric = distance_abs / atr_val 
            else: 
                if current_market_price > 0: distance_metric = (distance_abs / current_market_price) * Decimal(100)
            
            distance_bands = dist_influence_conf.get("distance_bands_multipliers", [])
            applied_distance_multiplier = Decimal("1.0")
            sorted_dist_bands = sorted(distance_bands, key=lambda x: Decimal(str(x.get("distance_threshold", "0"))))
            
            for band in sorted_dist_bands:
                threshold = Decimal(str(band.get("distance_threshold", "0")))
                multiplier = Decimal(str(band.get("margin_multiplier", "1.0")))
                if distance_metric <= threshold: 
                    applied_distance_multiplier = multiplier
                    break
            else: 
                if sorted_dist_bands: applied_distance_multiplier = Decimal(str(sorted_dist_bands[-1].get("margin_multiplier", "1.0")))

            if applied_distance_multiplier != Decimal("1.0"):
                calculated_margin *= applied_distance_multiplier
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"DynamicMargin (Distance): Zone '{zone_data.get('source_details', 'N/A')[:30]}' dist metric {distance_metric:.2f}, margin modified by x{applied_distance_multiplier:.2f}. New: {calculated_margin:.2f}")
        
        calculated_margin *= macd_overall_margin_modifier
        calculated_margin *= hrz_margin_reduction_factor
        
        min_margin_group = Decimal(str(dyn_margin_conf.get("min_margin_per_group_usd", "3.0"))) 
        max_margin_group = Decimal(str(dyn_margin_conf.get("max_margin_per_group_usd", "100.0")))
        calculated_margin = max(min_margin_group, calculated_margin)
        calculated_margin = min(max_margin_group, calculated_margin)

        return calculated_margin.quantize(Decimal("0.01"))

    def _create_orders_for_one_group(self,
                                    group_id_str: str,
                                    center_price_or_levels_list: Any,
                                    base_reason_for_group: str,
                                    margin_for_group_total: Decimal, leverage: Decimal, position_type: str,
                                    current_tf_atr: Decimal,
                                    dynamic_cluster_spread_multiplier: Decimal,
                                    price_velocity_percent: Decimal,
                                    tf_config_for_group: Optional[Dict[str, Any]] = None,
                                    tf_id_source: Optional[str] = None
                                    ) -> List[Dict[str, Any]]:
        orders_in_group = []
        
        current_symbol = getattr(self.app, 'selected_symbol', None)
        if not current_symbol:
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: No selected symbol. Cannot create orders for group {group_id_str}.")
            return []
        
        precision_info = get_symbol_precision_info(current_symbol, self.asset_precisions_data)
        if not precision_info:
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: Precision info not found for {current_symbol}. Cannot create orders for group {group_id_str}.")
            return []
        
        price_precision_str_from_info = precision_info.get('price_precision_str', '0.00000001')
        quantity_precision_str_from_info = precision_info.get('quantity_precision_str', '0.001')
        min_quantity_str_from_info = precision_info.get('min_quantity_str', '0.001')
        min_notional_str_from_info = precision_info.get('min_notional_str', '1.0')

        is_list_of_predefined_levels = isinstance(center_price_or_levels_list, list) and \
                                       all(isinstance(item, dict) and 'price' in item for item in center_price_or_levels_list)

        if not is_list_of_predefined_levels and (not isinstance(center_price_or_levels_list, Decimal) or center_price_or_levels_list <= Decimal(0)):
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: Invalid center_price {center_price_or_levels_list} for group {group_id_str}.")
            return []
        
        dyn_margin_conf = self.strategy_settings.get("dynamic_margin_logic", {})
        min_margin_per_order_usd = Decimal(str(dyn_margin_conf.get("min_margin_per_order_usd", "0.01")))
        gen_grid_conf = self.strategy_settings.get("general_grid_settings", {})
        
        cluster_orders_count_config = gen_grid_conf.get("cluster_orders_count", 3)
        cluster_orders_count = cluster_orders_count_config

        if is_list_of_predefined_levels:
            cluster_orders_count = len(center_price_or_levels_list)

        if cluster_orders_count == 0: return []

        if margin_for_group_total < (min_margin_per_order_usd * cluster_orders_count):
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: Margin for group {group_id_str} (${margin_for_group_total}) is too low for {cluster_orders_count} orders (min per order: ${min_margin_per_order_usd}). Skipping group.")
            return []
        if margin_for_group_total <= Decimal("0.001"):
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: Margin for group {group_id_str} too low (${margin_for_group_total}).")
            return []
        
        order_specs = [] 
        final_tf_id_source = tf_id_source 
        if not final_tf_id_source and is_list_of_predefined_levels and center_price_or_levels_list:
             final_tf_id_source = center_price_or_levels_list[0].get("source_tf", "aggregated_zone")
        if not final_tf_id_source : final_tf_id_source = "unknown_tf_source"


        center_price_for_cluster = center_price_or_levels_list 
        if is_list_of_predefined_levels: 
            num_orders_in_group = len(center_price_or_levels_list)
            for i, level_data in enumerate(center_price_or_levels_list): 
                order_specs.append({
                    "price": level_data['price'], 
                    "margin_share": (Decimal(1)/num_orders_in_group if num_orders_in_group > 0 else Decimal(0)), 
                    "reason_from_level": level_data.get('reason', base_reason_for_group), 
                    "reason_suffix": "" 
                })
        else: 
            if not isinstance(center_price_for_cluster, Decimal) or center_price_for_cluster <= Decimal(0) : return [] 
            
            cluster_weights_conf = gen_grid_conf.get("cluster_order_weights_by_count", {})
            base_weights_str_list = cluster_weights_conf.get(str(cluster_orders_count))
            if not base_weights_str_list or len(base_weights_str_list) != cluster_orders_count:
                if cluster_orders_count == 1: base_weights_str_list = ["1.0"]
                elif cluster_orders_count == 2: base_weights_str_list = ["0.45", "0.55"] 
                elif cluster_orders_count == 3: base_weights_str_list = ["0.25", "0.35", "0.40"] 
                else: base_weights_str_list = [str(Decimal("1.0") / Decimal(cluster_orders_count))] * cluster_orders_count 
            
            base_cluster_weights = []
            try: 
                base_cluster_weights = [Decimal(str(w)) for w in base_weights_str_list] 
                s = sum(base_cluster_weights)
                if abs(s - Decimal("1.0")) > Decimal("1e-9") and s > Decimal("1e-9"): 
                    base_cluster_weights = [w/s for w in base_cluster_weights] 
                elif s <= Decimal("1e-9") and cluster_orders_count > 0 : 
                    base_cluster_weights = [Decimal(1)/cluster_orders_count] * cluster_orders_count 
            except Exception: 
                base_cluster_weights = [Decimal("1.0")] if cluster_orders_count == 1 else ([Decimal(1)/cluster_orders_count] * cluster_orders_count if cluster_orders_count > 0 else [])
            
            current_cluster_weights = list(base_cluster_weights) 
            dyn_cw_conf = self.strategy_settings.get("volatility_reaction", {}).get("dynamic_cluster_weights_on_strong_move", {})
            pvc_conf = self.strategy_settings.get("volatility_reaction", {}).get("price_velocity_config", {})
            if dyn_cw_conf.get("enabled", False) and cluster_orders_count > 1 and len(current_cluster_weights) == cluster_orders_count:
                strong_move_thresh = Decimal(str(pvc_conf.get("strong_move_threshold_percent", "0.3")))
                weight_shift_abs = Decimal(str(dyn_cw_conf.get("weight_shift_percent", "0.15"))) 
                shift_behavior = dyn_cw_conf.get("behavior_on_adverse_move", "shift_to_closer") 

                is_strong_move_against_pos = (position_type == "Long" and price_velocity_percent < -strong_move_thresh) or \
                                             (position_type == "Short" and price_velocity_percent > strong_move_thresh)
                
                if is_strong_move_against_pos: 
                    idx_hit_first = len(current_cluster_weights) - 1 
                    idx_hit_last = 0 
                    
                    source_idx, target_idx = -1, -1
                    if shift_behavior == "shift_to_further": 
                        source_idx, target_idx = idx_hit_first, idx_hit_last
                    elif shift_behavior == "shift_to_closer": 
                        source_idx, target_idx = idx_hit_last, idx_hit_first
                    
                    if source_idx != -1 and target_idx != -1 and source_idx != target_idx:
                        original_weight_source = current_cluster_weights[source_idx]
                        reduction = min(original_weight_source * Decimal("0.5"), weight_shift_abs) 
                        reduction = min(reduction, original_weight_source - (original_weight_source * Decimal("0.05"))) 
                        reduction = max(Decimal('0'), reduction) 
                        if reduction > 0:
                            current_cluster_weights[source_idx] -= reduction
                            current_cluster_weights[target_idx] += reduction 
            
            if current_tf_atr <= Decimal(0) and cluster_orders_count > 1: 
                cluster_orders_count = 1; current_cluster_weights = [Decimal("1.0")]
            cluster_spread_abs = current_tf_atr * dynamic_cluster_spread_multiplier if cluster_orders_count > 1 else Decimal(0)

            if cluster_orders_count == 1:
                order_specs.append({"price": center_price_for_cluster, "margin_share": current_cluster_weights[0] if current_cluster_weights else Decimal(1), "reason_suffix": "(центр)"})
            elif cluster_orders_count == 2:
                half_s = cluster_spread_abs / Decimal("2")
                p1 = center_price_for_cluster - half_s 
                p2 = center_price_for_cluster + half_s 
                prices_sorted = sorted([p1,p2], reverse=(position_type == "Long")) 
                order_specs.append({"price": prices_sorted[1], "margin_share": current_cluster_weights[0], "reason_suffix": "(далі)"}) 
                order_specs.append({"price": prices_sorted[0], "margin_share": current_cluster_weights[1], "reason_suffix": "(ближче)"})
            elif cluster_orders_count >= 3: 
                p_farthest = center_price_for_cluster - cluster_spread_abs 
                p_center_actual = center_price_for_cluster 
                p_closest = center_price_for_cluster + cluster_spread_abs 
                prices_for_3 = sorted([p_farthest, p_center_actual, p_closest], reverse=(position_type == "Long"))
                order_specs.append({"price": prices_for_3[2], "margin_share": current_cluster_weights[0], "reason_suffix": "(далі)"}) 
                order_specs.append({"price": prices_for_3[1], "margin_share": current_cluster_weights[1], "reason_suffix": "(центр)"}) 
                order_specs.append({"price": prices_for_3[0], "margin_share": current_cluster_weights[2], "reason_suffix": "(ближче)"})

        for idx, spec in enumerate(order_specs):
            order_price_candidate = spec.get('price')
            if not isinstance(order_price_candidate, Decimal) or order_price_candidate <= Decimal(0):
                if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: Invalid price in spec for group {group_id_str}. Skipping order spec.")
                continue
            
            quantized_order_price = quantize_decimal(order_price_candidate, price_precision_str_from_info)
            if quantized_order_price <= Decimal(0):
                if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: Quantized price became zero or less for group {group_id_str} (Original: {order_price_candidate}). Skipping order spec.")
                continue

            margin_share_val = spec.get('margin_share', Decimal(0))
            if not isinstance(margin_share_val, Decimal):
                try: margin_share_val = Decimal(str(margin_share_val))
                except: margin_share_val = Decimal(1) / len(order_specs) if order_specs else Decimal(0)

            desired_notional_for_this_order = margin_for_group_total * margin_share_val * leverage
            
            if desired_notional_for_this_order <= Decimal(0):
                if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"GridGen CreateOrders: Desired notional for order in group {group_id_str} is zero or less. Skipping order.")
                continue

            base_amt_quantized, actual_quote_for_order = calculate_base_from_quote(
                desired_notional_for_this_order,
                quantized_order_price,
                quantity_precision_str_from_info,
                min_quantity_str_from_info,
                price_precision_str_from_info,
                min_notional_str_from_info
            )

            if base_amt_quantized is None or base_amt_quantized <= Decimal(0) or actual_quote_for_order is None or actual_quote_for_order <= Decimal(0):
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"GridGen CreateOrders: Order for group {group_id_str} @ {quantized_order_price} skipped. Reason: base_amt_quantized or actual_quote_for_order is invalid. (BaseQ: {base_amt_quantized}, ActualQuote: {actual_quote_for_order}, DesiredNotional: {desired_notional_for_this_order})")
                continue

            margin_ord = actual_quote_for_order / leverage
            margin_ord = margin_ord.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            if margin_ord < min_margin_per_order_usd:
                if hasattr(self.app, 'add_sim_log'):
                     self.app.add_sim_log(f"GridGen CreateOrders: Calculated margin ${margin_ord} for order in group {group_id_str} is less than min_margin_per_order_usd (${min_margin_per_order_usd}). Skipping order.")
                continue
            
            order_reason_final = (spec.get('reason_from_level') or base_reason_for_group) + f" {spec.get('reason_suffix','')}"
            base_amount_with_sign = base_amt_quantized if position_type == "Long" else -base_amt_quantized

            orders_in_group.append({
                "price": quantized_order_price,
                "margin": margin_ord,
                "reason": order_reason_final.strip(),
                "base_amount": base_amount_with_sign, 
                "notional_leveraged": actual_quote_for_order,
                "status": "pending", "order_id_sim": str(uuid.uuid4()),
                "group_id": group_id_str, "is_main_grid_order": True,
                "timestamp_created": time.time(), "order_in_group_index": idx,
                "tf_id_source": final_tf_id_source
            })
        return orders_in_group

    def generate_dynamic_grid(self,
                              current_market_price: Decimal, position_type: str,
                              leverage: Decimal,
                              initial_overall_margin: Decimal,
                              margin_increase_factor_from_sim_logic: Decimal,
                              symbol: Optional[str] = None,
                              current_pnl_percent: Optional[Decimal] = None,
                              is_initial_fill: bool = False,
                              existing_orders_for_adjustment: Optional[List[Dict[str, Any]]] = None,
                              existing_avg_price: Optional[Decimal] = None,
                              existing_total_base_amount: Optional[Decimal] = None,
                              custom_group_id_prefix: str = "DG"
                             ) -> List[Dict[str, Any]]:
        all_new_orders: List[Dict[str, Any]] = []
        if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"DynamicGrid: Starting generation for {position_type} {symbol} at {current_market_price}")

        if not symbol:
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log("DynamicGrid ERROR: Symbol not provided.")
            return []
        precision_info = get_symbol_precision_info(symbol, self.asset_precisions_data)
        if not precision_info:
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"DynamicGrid ERROR: Precision info not found for {symbol}.")
            return []

        level_gen_ref_type = self.strategy_settings.get("general_grid_settings", {}).get("level_generation_reference_price", "current_market_price")
        ref_price_for_levels = current_market_price
        if level_gen_ref_type == "average_entry_price" and existing_avg_price and existing_avg_price > Decimal(0):
            ref_price_for_levels = existing_avg_price
            if hasattr(self.app, 'add_sim_log'):
                self.app.add_sim_log(f"DynamicGrid: Using Average Entry Price ({existing_avg_price}) as reference for level generation.")
        else:
            if hasattr(self.app, 'add_sim_log'):
                self.app.add_sim_log(f"DynamicGrid: Using Current Market Price ({current_market_price}) as reference for level generation.")

        hrz_active = False
        hrz_action_settings = {}
        hrz_margin_reduction_factor = Decimal("1.0")
        allowed_hrz_level_types = None

        hrz_conf = self.strategy_settings.get("high_risk_zone_adjustment", {})
        if hrz_conf.get("enabled", False) and current_pnl_percent is not None:
            hrz_pnl_threshold = Decimal(str(hrz_conf.get("pnl_threshold_percent", "-1000")))
            if current_pnl_percent <= hrz_pnl_threshold:
                hrz_active = True
                hrz_action_settings = hrz_conf.get("actions", {})
                hrz_margin_reduction_factor = Decimal(str(hrz_action_settings.get("reduce_new_order_margin_multiplier", "1.0")))
                if hrz_action_settings.get("use_only_conservative_levels", False):
                    allowed_hrz_level_types = hrz_action_settings.get("allowed_level_types_in_hrz", [])
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"DynamicGrid: High-Risk Zone ACTIVE (PNL: {current_pnl_percent:.2f}%). Margin reduction: x{hrz_margin_reduction_factor}. Allowed levels: {allowed_hrz_level_types or 'All'}")
                if hrz_action_settings.get("allow_only_rescue_or_stabilization_orders", False):
                    if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log("DynamicGrid: HRZ - Regular grid generation skipped (allow_only_rescue_or_stabilization_orders).")
                    return []

        prioritized_zones = self._collect_and_prioritize_all_levels(ref_price_for_levels, position_type, symbol, allowed_hrz_level_types)

        if not prioritized_zones:
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log("DynamicGrid: No prioritized zones/levels found" + (" (possibly due to HRZ filter)." if hrz_active and allowed_hrz_level_types else "."))
            return []
        if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"DynamicGrid: Found {len(prioritized_zones)} potential zones/levels after filtering.")

        macd_actions, macd_overall_margin_modifier = self._get_macd_action_and_modifiers(position_type)

        if "cancel_all_buys" in macd_actions and position_type == "Long":
            logger.info(f"DynamicGrid: [{position_type}] MACD action 'cancel_all_buys' triggered. Skipping grid generation.")
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log("DynamicGrid: MACD action 'cancel_all_buys' triggered. Skipping.")
            return []
        if "cancel_all_sells" in macd_actions and position_type == "Short":
            logger.info(f"DynamicGrid: [{position_type}] MACD action 'cancel_all_sells' triggered. Skipping grid generation.")
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log("DynamicGrid: MACD action 'cancel_all_sells' triggered. Skipping.")
            return []

        effective_margin_accumulator = initial_overall_margin 

        dyn_cluster_mult, _, _ = self._get_dynamic_multipliers_for_grid()

        max_groups_limit_config = self.strategy_settings.get("general_grid_settings",{}).get("max_total_active_groups", 5)
        max_groups_limit = hrz_action_settings.get("max_new_groups_to_place_override", max_groups_limit_config) if hrz_active else max_groups_limit_config

        num_groups_placed = 0

        dyn_placement_conf = self.strategy_settings.get("dynamic_level_placement", {})
        trend_tf_id = dyn_placement_conf.get("trend_determination_sma_tf", DEFAULT_H1_TF_ID)
        trend_analysis = self.analyzer.get_analysis_for_tf(trend_tf_id)
        is_strong_uptrend, is_strong_downtrend = False, False
        if trend_analysis and not trend_analysis.get("error"):
            sma_short_p = dyn_placement_conf.get("trend_determination_sma_short_period", 20)
            sma_long_p = dyn_placement_conf.get("trend_determination_sma_long_period", 50)
            sma_short_val = trend_analysis.get(f"sma_{sma_short_p}")
            sma_long_val = trend_analysis.get(f"sma_{sma_long_p}")
            try:
                # --- ПОЧАТОК ЗМІНИ ---
                # Використовуємо `current_market_price` з аргументів замість `self.app.current_price_var`
                market_price_dec = current_market_price 
                # --- КІНЕЦЬ ЗМІНИ ---
                if sma_short_val and sma_long_val and market_price_dec > 0: 
                    if sma_short_val > sma_long_val and market_price_dec > sma_long_val : is_strong_uptrend = True
                    if sma_short_val < sma_long_val and market_price_dec < sma_long_val : is_strong_downtrend = True
            except (InvalidOperation, TypeError): pass 

        price_velocity_abs = abs(getattr(self.app, 'current_price_velocity_percent', Decimal(0)))
        pvc_conf = self.strategy_settings.get("volatility_reaction", {}).get("price_velocity_config", {})
        strong_move_thresh_abs = abs(Decimal(str(pvc_conf.get("strong_move_threshold_percent", "0.6"))))
        volatility_factor = Decimal(str(dyn_placement_conf.get("volatility_threshold_for_dynamic_offset_factor", "1.5")))
        is_high_volatility = price_velocity_abs > (strong_move_thresh_abs * volatility_factor)


        for zone_data in prioritized_zones:
            if num_groups_placed >= max_groups_limit:
                if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"DynamicGrid: Reached group limit ({max_groups_limit}).")
                break

            base_zone_price = zone_data["price_zone_center"]
            main_tf_in_zone = zone_data.get("original_levels_in_zone",[{}])[0].get("source_tf", DEFAULT_M15_TF_ID)
            zone_tf_analysis = self.analyzer.get_analysis_for_tf(main_tf_in_zone)
            atr_for_zone = zone_tf_analysis.get('atr', current_market_price * Decimal('0.001')) if zone_tf_analysis and not zone_tf_analysis.get("error") else current_market_price * Decimal('0.001')
            if atr_for_zone <= Decimal(0): atr_for_zone = current_market_price * Decimal('0.001') 

            final_order_price_center = self._calculate_dynamic_order_price(
                base_zone_price,
                zone_data.get("original_levels_in_zone",[{}])[0].get("type","unknown_zone_type"), 
                position_type, atr_for_zone,
                is_strong_uptrend, is_strong_downtrend, is_high_volatility,
                current_market_price_for_calc=current_market_price # Передаємо ціну
            )

            base_margin_for_this_group = initial_overall_margin if num_groups_placed == 0 else effective_margin_accumulator

            margin_for_this_specific_group_target = self._calculate_dynamic_margin_for_zone(
                base_margin_for_this_group,
                zone_data, current_market_price, position_type,
                macd_overall_margin_modifier, 
                hrz_margin_reduction_factor  
            )

            final_margin_for_this_group = check_and_adjust_margin_for_capital_limit(
                self.app,
                margin_for_this_specific_group_target,
                is_emergency_order=False 
            )

            if final_margin_for_this_group <= Decimal(0):
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"DynamicGrid: Margin for zone {zone_data.get('source_details', 'N/A')[:30]} is zero or less after capital check (Target: ${margin_for_this_specific_group_target}). Skipping group.")
                if num_groups_placed == 0: 
                    effective_margin_accumulator = initial_overall_margin * margin_increase_factor_from_sim_logic
                else:
                    effective_margin_accumulator *= margin_increase_factor_from_sim_logic
                continue


            gid_str = f"{custom_group_id_prefix}_{self.app.sim_next_group_id_num()}"
            orders_for_this_zone = self._create_orders_for_one_group(
                gid_str, final_order_price_center, zone_data["source_details"],
                final_margin_for_this_group, leverage, position_type,
                atr_for_zone,
                dyn_cluster_mult, 
                getattr(self.app, 'current_price_velocity_percent', Decimal(0)), 
                tf_id_source=main_tf_in_zone 
            )

            if orders_for_this_zone:
                all_new_orders.extend(orders_for_this_zone)
                
                current_group_margin_added_to_pending = Decimal(0)
                for new_order_in_zone in orders_for_this_zone:
                    current_group_margin_added_to_pending += new_order_in_zone.get("margin", Decimal(0))
                
                self.app.sim_pending_orders_margin += current_group_margin_added_to_pending
                
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"DynamicGrid DEBUG: Group {gid_str} margin {current_group_margin_added_to_pending} ADDED. app.sim_pending_orders_margin is now {self.app.sim_pending_orders_margin}")
                
                if num_groups_placed == 0: 
                    effective_margin_accumulator = initial_overall_margin * margin_increase_factor_from_sim_logic
                else: 
                    effective_margin_accumulator *= margin_increase_factor_from_sim_logic
                num_groups_placed += 1
        
        if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"DynamicGrid: Generated {len(all_new_orders)} orders across {num_groups_placed} groups/zones.")

        coh_conf = self.strategy_settings.get("close_orders_handling", {})
        if coh_conf.get("enabled", False) and all_new_orders:
            current_pending_orders_to_check_against = []
            if existing_orders_for_adjustment is not None: 
                current_pending_orders_to_check_against = existing_orders_for_adjustment
            elif coh_conf.get("check_scope") == "all_pending_orders": 
                 current_pending_orders_to_check_against = [
                     o for o in self.app.sim_orders_data if o.get("status") == "pending" and o.get("is_main_grid_order")
                 ]
            
            filter_tf_id = self.strategy_settings.get("level_aggregation",{}).get("primary_tf_for_atr_proximity", DEFAULT_M15_TF_ID)
            
            finalized_orders = self._filter_and_adjust_close_orders(
                all_new_orders, 
                current_pending_orders_to_check_against, 
                position_type,
                current_market_price,
                filter_tf_id 
            )
            if hasattr(self.app, 'add_sim_log'): self.app.add_sim_log(f"DynamicGrid: After filtering close orders, {len(finalized_orders)} orders remain.")
            return finalized_orders 
        
        return all_new_orders

    def _filter_and_adjust_close_orders(self,
                                        candidate_orders: List[Dict[str, Any]],
                                        existing_pending_orders: List[Dict[str, Any]],
                                        position_type: str,
                                        current_market_price: Decimal,
                                        current_tf_id_for_atr: str
                                        ) -> List[Dict[str, Any]]:
        # ... (цей метод залишається без змін) ...
        return candidate_orders

    def generate_grid_for_current_tf(self, # Застаріла функція
                                     current_market_price: Decimal, position_type: str,
                                     num_groups_to_target_on_this_tf: int,
                                     margin_per_group_initial_for_this_tf: Decimal,
                                     leverage: Decimal, margin_increase_factor: Decimal,
                                     current_tf_id: str,
                                     existing_avg_price: Optional[Decimal] = None,
                                     existing_total_base_amount: Optional[Decimal] = None,
                                     symbol: Optional[str] = None, is_rescue_attempt: bool = False,
                                     custom_group_id_prefix: str = "G",
                                     _start_group_id_num_placeholder: int = 1,
                                     price_velocity_for_weights: Decimal = Decimal(0)) -> List[Dict[str,Any]]:
        # ... (цей метод залишається без змін) ...
        return []

    def get_potential_grid_preview(self,
                                   current_market_price: Decimal, position_type: str, leverage: Decimal,
                                   symbol: Optional[str], current_overall_group_counter: int,
                                   price_velocity_for_weights: Decimal 
                                   ) -> List[Dict[str,Any]]:
        preview_descs = []
        if not self.app: return []

        gen_grid_conf = self.strategy_settings.get("general_grid_settings", {})
        initial_margin_preview_str = self.app.sim_initial_group_margin_input_var.get()
        initial_margin_preview = Decimal(initial_margin_preview_str if initial_margin_preview_str.strip() else str(gen_grid_conf.get("initial_group_margin_usd", "10.0")))
        
        margin_inc_f_preview_str = self.app.sim_margin_increase_factor_input_var.get()
        margin_inc_f_preview = Decimal(margin_inc_f_preview_str if margin_inc_f_preview_str.strip() else str(gen_grid_conf.get("margin_increase_factor_per_group", "1.2")))
        
        price_prec = self.get_price_precision(symbol)
        
        level_gen_ref_type = self.strategy_settings.get("general_grid_settings", {}).get("level_generation_reference_price", "current_market_price")
        ref_price_for_levels_preview = current_market_price
        if level_gen_ref_type == "average_entry_price" and hasattr(self.app, 'sim_avg_entry_price') and self.app.sim_avg_entry_price and self.app.sim_avg_entry_price > Decimal(0):
            ref_price_for_levels_preview = self.app.sim_avg_entry_price
        
        prioritized_zones_preview = self._collect_and_prioritize_all_levels(ref_price_for_levels_preview, position_type, symbol, None)
        
        num_groups_for_preview = self.strategy_settings.get("general_grid_settings",{}).get("max_total_active_groups_preview", 3)

        current_preview_group_id_num = current_overall_group_counter + 1
        current_preview_margin_base = initial_margin_preview

        dyn_placement_conf = self.strategy_settings.get("dynamic_level_placement", {})
        trend_tf_id = dyn_placement_conf.get("trend_determination_sma_tf", DEFAULT_H1_TF_ID)
        trend_analysis = self.analyzer.get_analysis_for_tf(trend_tf_id)
        is_strong_uptrend, is_strong_downtrend = False, False
        if trend_analysis and not trend_analysis.get("error"):
            sma_short_p = dyn_placement_conf.get("trend_determination_sma_short_period", 20)
            sma_long_p = dyn_placement_conf.get("trend_determination_sma_long_period", 50)
            sma_short_val = trend_analysis.get(f"sma_{sma_short_p}")
            sma_long_val = trend_analysis.get(f"sma_{sma_long_p}")
            try:
                # --- ПОЧАТОК ЗМІНИ ---
                # Використовуємо `current_market_price` з аргументів
                market_price_dec = current_market_price
                # --- КІНЕЦЬ ЗМІНИ ---
                if sma_short_val and sma_long_val and market_price_dec > 0:
                    if sma_short_val > sma_long_val and market_price_dec > sma_long_val : is_strong_uptrend = True
                    if sma_short_val < sma_long_val and market_price_dec < sma_long_val : is_strong_downtrend = True
            except (InvalidOperation, TypeError): pass
        
        price_velocity_abs = abs(getattr(self.app, 'current_price_velocity_percent', Decimal(0)))
        pvc_conf = self.strategy_settings.get("volatility_reaction", {}).get("price_velocity_config", {})
        strong_move_thresh_abs = abs(Decimal(str(pvc_conf.get("strong_move_threshold_percent", "0.6"))))
        volatility_factor = Decimal(str(dyn_placement_conf.get("volatility_threshold_for_dynamic_offset_factor", "1.5")))
        is_high_volatility = price_velocity_abs > (strong_move_thresh_abs * volatility_factor)
        
        _, macd_overall_margin_modifier_preview = self._get_macd_action_and_modifiers(position_type)


        for zone_idx, zone_data in enumerate(prioritized_zones_preview):
            if zone_idx >= num_groups_for_preview: break
            
            gid_str = f"PG{current_preview_group_id_num}"
            base_zone_price = zone_data["price_zone_center"]
            reason_for_desc = zone_data["source_details"]
            
            main_tf_in_zone_preview = zone_data.get("original_levels_in_zone",[{}])[0].get("source_tf", DEFAULT_M15_TF_ID)
            zone_tf_analysis_preview = self.analyzer.get_analysis_for_tf(main_tf_in_zone_preview)
            atr_for_zone_preview = zone_tf_analysis_preview.get('atr', current_market_price * Decimal('0.001')) if zone_tf_analysis_preview and not zone_tf_analysis_preview.get("error") else current_market_price * Decimal('0.001')
            if atr_for_zone_preview <= Decimal(0): atr_for_zone_preview = current_market_price * Decimal('0.001')

            display_price = self._calculate_dynamic_order_price(
                base_zone_price,
                zone_data.get("original_levels_in_zone",[{}])[0].get("type","unknown"),
                position_type,
                atr_for_zone_preview,
                is_strong_uptrend,
                is_strong_downtrend,
                is_high_volatility,
                current_market_price_for_calc=current_market_price # Передаємо ціну
            )

            margin_for_preview_group = self._calculate_dynamic_margin_for_zone(
                current_preview_margin_base,
                zone_data,
                current_market_price,
                position_type,
                macd_overall_margin_modifier_preview,
                Decimal("1.0") 
            )

            orders_in_cluster_preview_count = gen_grid_conf.get("cluster_orders_count", 3)
            if display_price <= Decimal(0): continue

            preview_descs.append({
                "group_id": gid_str,
                "timeframe_source": main_tf_in_zone_preview,
                "center_price": quantize_decimal(display_price, price_prec),
                "reason": reason_for_desc[:60] + ('...' if len(reason_for_desc) > 60 else ''),
                "total_margin_for_group": quantize_decimal(margin_for_preview_group, '0.01'),
                "orders_in_cluster": orders_in_cluster_preview_count,
                "aggregated_weight": zone_data.get("aggregated_weight", Decimal(0)),
                "confluence_score": zone_data.get("confluence_score", Decimal(0)),
                "zone_strength_score": zone_data.get("zone_strength_score", Decimal(0))
            })
            current_preview_group_id_num += 1
            current_preview_margin_base *= margin_inc_f_preview
            
        return preview_descs