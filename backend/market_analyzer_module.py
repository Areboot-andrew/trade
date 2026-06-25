# market_analyzer_module.py
import pandas as pd
try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False
    import logging
    logging.getLogger(__name__).warning("pandas-ta not installed. Technical indicators will use fallback (zeros).")
from decimal import Decimal, InvalidOperation
from binance.client import Client
from constants import MIN_KLINE_DATA_FOR_ANALYSIS, DEFAULT_M15_TF_ID, DEFAULT_M1_TF_ID #

class MarketAnalyzer:
    def __init__(self, strategy_settings: dict, app_instance=None):
        self.analysis_results_by_tf = {}
        self.strategy_settings = strategy_settings
        self.current_symbol_klines_dfs = {}
        self.app = app_instance

    def update_config(self, new_strategy_settings: dict):
        self.strategy_settings = new_strategy_settings
        self.analysis_results_by_tf.clear()

    def _safe_decimal_convert(self, series_value, default_val=Decimal(0)):
        if series_value is None or (isinstance(series_value, float) and pd.isna(series_value)):
            return default_val
        try:
            return Decimal(str(series_value))
        except InvalidOperation:
            return default_val

# Фрагмент файлу BOTV1/market_analyzer_module.py

    def _calculate_fibonacci_levels(self, df_input: pd.DataFrame, tf_id: str) -> dict:
        """
        Розраховує рівні Фібоначчі на основі локальних екстремумів.
        """
        fib_levels_results = {}
        fib_config = self.strategy_settings.get("level_sourcing_and_processing", {}).get("fibonacci_levels", {}) #
        if not fib_config.get("enabled", False): #
            return fib_levels_results

        lookback_candles = fib_config.get("lookback_candles_for_extremums", 100) #
        min_range_percent = Decimal(str(fib_config.get("min_price_range_percent_for_relevance", "1.5"))) #
        levels_to_use_str = fib_config.get("levels_to_use", ["0.382", "0.5", "0.618"]) #
        
        levels_to_use_decimal = []
        for level_str in levels_to_use_str:
            try:
                levels_to_use_decimal.append(Decimal(level_str))
            except InvalidOperation:
                if hasattr(self.app, 'add_sim_log'):
                    self.app.add_sim_log(f"Fibonacci Calc ({tf_id}): Invalid Fibo level string '{level_str}'. Skipping.")
                continue

        if df_input.empty or len(df_input) < lookback_candles or len(df_input) < 2:
            if hasattr(self.app, 'add_sim_log'):
                 self.app.add_sim_log(f"Fibonacci Calc ({tf_id}): Not enough data for Fibo calculation ({len(df_input)} candles, need {max(lookback_candles, 2)}).")
            return fib_levels_results

        relevant_df = df_input.tail(lookback_candles)
        
        # ВИПРАВЛЕННЯ: Конвертуємо min/max в Decimal перед операціями
        low_price_raw = relevant_df['low'].min()
        high_price_raw = relevant_df['high'].max()

        low_price = self._safe_decimal_convert(low_price_raw)
        high_price = self._safe_decimal_convert(high_price_raw)

        if pd.isna(low_price_raw) or pd.isna(high_price_raw) or low_price <= Decimal(0) or high_price <= Decimal(0): # Перевірка після конвертації
            return fib_levels_results

        price_range = high_price - low_price # Тепер це операція Decimal - Decimal
        if high_price == low_price: 
            return fib_levels_results
            
        # Тепер всі компоненти є Decimal, помилки типу не буде
        price_range_percent = (price_range / low_price) * Decimal(100)

        if price_range_percent < min_range_percent:
            # if hasattr(self.app, 'add_sim_log'):
            #      self.app.add_sim_log(f"Fibonacci Calc ({tf_id}): Price range {price_range_percent:.2f}% is less than threshold {min_range_percent}%. Fibo levels not calculated.")
            return fib_levels_results
        
        for level_dec in levels_to_use_decimal:
            fibo_price_down_move = high_price - (price_range * level_dec)
            fib_levels_results[f'Fibo_{str(level_dec).replace(".","_")}_RetrDown'] = self._safe_decimal_convert(fibo_price_down_move)

        for level_dec in levels_to_use_decimal:
            fibo_price_up_move = low_price + (price_range * level_dec)
            fib_levels_results[f'Fibo_{str(level_dec).replace(".","_")}_RetrUp'] = self._safe_decimal_convert(fibo_price_up_move)
        
        return fib_levels_results

    def _calculate_indicators_for_one_tf(self, df_input: pd.DataFrame, tf_config: dict) -> dict:
        df = df_input.copy()
        res_indicators = {}
        tf_id = tf_config.get("timeframe_id", "UNKNOWN_TF")
        res_indicators["tf_id"] = tf_id
        res_indicators["binance_interval"] = tf_config.get("binance_interval_notation", "N/A")

        if df.empty:
            return {"error": "DataFrame is empty", **res_indicators}

        periods_to_check = []
        # Додаємо періоди з основних індикаторів
        for key in ["atr_period", "bb_period", "rsi_period", "macd_slow",
                    "ichimoku_senkou_b_period", "stoch_rsi_length"]:
            period = tf_config.get(key)
            if isinstance(period, (int, float)) and period > 0:
                if key == "stoch_rsi_length": # Для stoch_rsi додаємо також stoch_rsi_k
                    stoch_k = tf_config.get("stoch_rsi_k", 3)
                    if isinstance(stoch_k, (int, float)) and stoch_k > 0:
                        periods_to_check.append(period + stoch_k)
                    else:
                        periods_to_check.append(period)
                else:
                    periods_to_check.append(period)
            elif key == "macd_slow" and period is None: # Дефолт для macd_slow
                 periods_to_check.append(26)
            # Додайте інші дефолти, якщо потрібно

        # Додаємо періоди SMA зі списку sma_periods
        sma_periods_list = tf_config.get("sma_periods", [])
        if sma_periods_list:
            for p in sma_periods_list:
                if isinstance(p, (int, float)) and p > 0:
                    periods_to_check.append(p)
        else: # Fallback, якщо sma_periods не задано
            for key in ["sma_short_period", "sma_long_period", "sma_200_period"]:
                period = tf_config.get(key)
                if isinstance(period, (int, float)) and period > 0:
                    periods_to_check.append(period)

        # Видаляємо дублікати та None, залишаємо тільки позитивні числа
        numeric_periods = sorted(list(set(p for p in periods_to_check if isinstance(p, (int, float)) and p > 0)))

        if not numeric_periods:
            min_len_for_calc = MIN_KLINE_DATA_FOR_ANALYSIS + 30 #
        else:
            min_len_for_calc = max(numeric_periods) + 30

        if len(df) < min_len_for_calc:
            error_msg = f"Not enough data ({len(df)} candles, need ~{min_len_for_calc}) for {tf_id}"
            if hasattr(self.app, 'add_sim_log') and callable(self.app.add_sim_log):
                 self.app.add_sim_log(f"TA Error ({tf_id}): {error_msg}")
            return {"error": error_msg, **res_indicators}

        atr_p = tf_config.get("atr_period", 14)
        bb_p = tf_config.get("bb_period", 20); bb_std = float(str(tf_config.get("bb_std_dev", "2.0")))
        rsi_p = tf_config.get("rsi_period", 14)
        macd_f = tf_config.get("macd_fast", 12); macd_s = tf_config.get("macd_slow", 26); macd_sig = tf_config.get("macd_signal", 9)
        st_len = tf_config.get("stoch_rsi_length", 14); st_rsi_rsi_len = tf_config.get("stoch_rsi_rsi_length", 14)
        st_k = tf_config.get("stoch_rsi_k", 3); st_d = tf_config.get("stoch_rsi_d", 3)
        ichi_t = tf_config.get("ichimoku_tenkan", 9); ichi_k = tf_config.get("ichimoku_kijun", 26)
        ichi_sb_p = tf_config.get("ichimoku_senkou_b_period", 52)
        ichi_cs_offset = tf_config.get("ichimoku_chikou_span_offset", 26)
        ichi_sa_sb_offset = tf_config.get("ichimoku_senkou_span_a_b_offset", 26)

        for col_name in ['open', 'high', 'low', 'close', 'volume']:
            if col_name not in df.columns: df[col_name] = 0.0
            df[col_name] = pd.to_numeric(df[col_name], errors='coerce')
        df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
        if len(df) < 5:
            return {"error": f"Not enough valid data after NaN drop for {tf_id} ({len(df)})", **res_indicators}

        try: res_indicators['atr'] = self._safe_decimal_convert(df.ta.atr(length=atr_p, append=False).iloc[-1])
        except Exception: res_indicators['atr'] = Decimal(0)

        sma_periods_to_calc = tf_config.get("sma_periods", [])
        if not sma_periods_to_calc: # Fallback, якщо sma_periods не задано
            temp_sma_periods = []
            if tf_config.get("sma_short_period"): temp_sma_periods.append(tf_config.get("sma_short_period"))
            if tf_config.get("sma_long_period"): temp_sma_periods.append(tf_config.get("sma_long_period"))
            if tf_config.get("sma_200_period") and (200 in numeric_periods or "sma_200" in tf_config.get("level_type_priority", [])) :
                temp_sma_periods.append(tf_config.get("sma_200_period"))
            sma_periods_to_calc = sorted(list(set(p for p in temp_sma_periods if isinstance(p, int) and p > 0)))


        for period in sma_periods_to_calc:
            if isinstance(period, int) and period > 0:
                try:
                    sma_value = df.ta.sma(length=period, append=False).iloc[-1]
                    res_indicators[f'sma_{period}'] = self._safe_decimal_convert(sma_value)
                except Exception:
                    res_indicators[f'sma_{period}'] = Decimal(0)

        try: res_indicators['rsi'] = self._safe_decimal_convert(df.ta.rsi(length=rsi_p, append=False).iloc[-1], default_val=Decimal(50))
        except Exception: res_indicators['rsi'] = Decimal(50)

        try:
            bb_df = df.ta.bbands(length=bb_p, std=bb_std, append=False)
            if bb_df is not None and not bb_df.empty:
                res_indicators['bb_lower'] = self._safe_decimal_convert(bb_df[f'BBL_{bb_p}_{bb_std:.1f}'].iloc[-1])
                res_indicators['bb_middle'] = self._safe_decimal_convert(bb_df[f'BBM_{bb_p}_{bb_std:.1f}'].iloc[-1])
                res_indicators['bb_upper'] = self._safe_decimal_convert(bb_df[f'BBU_{bb_p}_{bb_std:.1f}'].iloc[-1])
            else: raise ValueError("BBands result is None or empty")
        except Exception: res_indicators.update({'bb_lower':Decimal(0), 'bb_middle':Decimal(0), 'bb_upper':Decimal(0)})

        try:
            macd_df = df.ta.macd(fast=macd_f, slow=macd_s, signal=macd_sig, append=False)
            if macd_df is not None and not macd_df.empty:
                res_indicators['macd_line'] = self._safe_decimal_convert(macd_df[f'MACD_{macd_f}_{macd_s}_{macd_sig}'].iloc[-1])
                res_indicators['macd_signal'] = self._safe_decimal_convert(macd_df[f'MACDs_{macd_f}_{macd_s}_{macd_sig}'].iloc[-1])
                res_indicators['macd_hist'] = self._safe_decimal_convert(macd_df[f'MACDh_{macd_f}_{macd_s}_{macd_sig}'].iloc[-1])
            else: raise ValueError("MACD result is None or empty")
        except Exception: res_indicators.update({'macd_line':Decimal(0), 'macd_signal':Decimal(0), 'macd_hist':Decimal(0)})

        try:
            st_df = df.ta.stochrsi(length=st_len, rsi_length=st_rsi_rsi_len, k=st_k, d=st_d, append=False)
            if st_df is not None and not st_df.empty:
                k_col_name_exact = f'STOCHRSIk_{st_len}_{st_rsi_rsi_len}_{st_k}_{st_d}'
                d_col_name_exact = f'STOCHRSId_{st_len}_{st_rsi_rsi_len}_{st_k}_{st_d}'
                k_col_actual = next((c for c in st_df.columns if c.lower().startswith(f'stochrsik_{st_len}_{st_rsi_rsi_len}_{st_k}')), k_col_name_exact)
                d_col_actual = next((c for c in st_df.columns if c.lower().startswith(f'stochrsid_{st_len}_{st_rsi_rsi_len}_{st_k}')), d_col_name_exact)

                res_indicators['stoch_rsi_k'] = self._safe_decimal_convert(st_df[k_col_actual].iloc[-1], Decimal(50)) if k_col_actual in st_df.columns else Decimal(50)
                res_indicators['stoch_rsi_d'] = self._safe_decimal_convert(st_df[d_col_actual].iloc[-1], Decimal(50)) if d_col_actual in st_df.columns else Decimal(50)
            else: raise ValueError("StochRSI result is None or empty")
        except Exception: res_indicators.update({'stoch_rsi_k': Decimal(50), 'stoch_rsi_d': Decimal(50)})

        try:
            ichi_data_tuple = df.ta.ichimoku(tenkan=ichi_t, kijun=ichi_k, senkou=ichi_sb_p,
                                           chikou_period=ichi_cs_offset, senkou_period=ichi_sa_sb_offset,
                                           append=False)
            if ichi_data_tuple and isinstance(ichi_data_tuple, tuple) and len(ichi_data_tuple) >= 1:
                ichi_df = ichi_data_tuple[0]
                span_df = ichi_data_tuple[1] if len(ichi_data_tuple) > 1 else ichi_df

                tenkan_col = next((col for col in ichi_df.columns if col.startswith(f'ITS_{ichi_t}')), None)
                kijun_col = next((col for col in ichi_df.columns if col.startswith(f'IKS_{ichi_k}')), None)
                span_a_col = next((col for col in span_df.columns if col.startswith(f'ISA_{ichi_t}_{ichi_k}')), None)
                if span_a_col is None: span_a_col = next((col for col in span_df.columns if col.startswith(f'ISA_{ichi_t}')), None)
                span_b_col = next((col for col in span_df.columns if col.startswith(f'ISB_{ichi_sb_p}')), None)
                chikou_col = next((col for col in ichi_df.columns if col.startswith(f'ICS_{ichi_cs_offset}')), None)

                if tenkan_col and not ichi_df[tenkan_col].dropna().empty: res_indicators['ichimoku_tenkan'] = self._safe_decimal_convert(ichi_df[tenkan_col].dropna().iloc[-1])
                if kijun_col and not ichi_df[kijun_col].dropna().empty: res_indicators['ichimoku_kijun'] = self._safe_decimal_convert(ichi_df[kijun_col].dropna().iloc[-1])
                if span_a_col and not span_df[span_a_col].dropna().empty: res_indicators['ichimoku_span_a'] = self._safe_decimal_convert(span_df[span_a_col].dropna().iloc[-1])
                if span_b_col and not span_df[span_b_col].dropna().empty: res_indicators['ichimoku_span_b'] = self._safe_decimal_convert(span_df[span_b_col].dropna().iloc[-1])
                if chikou_col and not ichi_df[chikou_col].dropna().empty: res_indicators['ichimoku_chikou_span'] = self._safe_decimal_convert(ichi_df[chikou_col].dropna().iloc[-1])
            else: raise ValueError("Ichimoku result is not as expected or empty")
        except Exception as e:
            res_indicators.update({'ichimoku_tenkan':Decimal(0), 'ichimoku_kijun':Decimal(0), 'ichimoku_span_a':Decimal(0), 'ichimoku_span_b':Decimal(0), 'ichimoku_chikou_span':Decimal(0)})

        el_lookback = tf_config.get("extremum_local_lookback_candles", 24)
        if len(df) >= el_lookback and el_lookback > 0 :
            rdf = df.tail(el_lookback)
            res_indicators['extremum_local_high'] = self._safe_decimal_convert(rdf['high'].max())
            res_indicators['extremum_local_low'] = self._safe_decimal_convert(rdf['low'].min())
        elif len(df) > 0 :
            res_indicators['extremum_local_high'] = self._safe_decimal_convert(df['high'].max())
            res_indicators['extremum_local_low'] = self._safe_decimal_convert(df['low'].min())
        else:
            res_indicators.update({'extremum_local_high':Decimal(0), 'extremum_local_low':Decimal(0)})

        # Додаємо розрахунок рівнів Фібоначчі
        fib_levels = self._calculate_fibonacci_levels(df, tf_id) #
        res_indicators.update(fib_levels)

        if not df.empty:
            last_row = df.iloc[-1]
            res_indicators['last_close_price'] = self._safe_decimal_convert(last_row['close'])
            res_indicators['last_open_price'] = self._safe_decimal_convert(last_row['open'])
            res_indicators['last_high_price'] = self._safe_decimal_convert(last_row['high'])
            res_indicators['last_low_price'] = self._safe_decimal_convert(last_row['low'])
        else:
            res_indicators.update({'last_close_price':Decimal(0),'last_open_price':Decimal(0),'last_high_price':Decimal(0),'last_low_price':Decimal(0)})

        return res_indicators

    def analyze_symbol_all_configured_tfs(self, symbol: str) -> dict:
        self.analysis_results_by_tf.clear()

        if not self.app:
            print("MarketAnalyzer Error: app_instance is not set. Cannot perform analysis.")
            return {"error": "app_instance not set"}

        tf_list_to_analyze_configs_final = []
        all_tf_configs_in_strategy = []
        gte_conf = self.strategy_settings.get("grid_timeframe_escalation", {}) #
        if gte_conf.get("timeframes_config"): #
            all_tf_configs_in_strategy.extend(gte_conf.get("timeframes_config", [])) #

        # Перевірка, чи потрібен M1 для EMG_DCA
        edca_conf = self.strategy_settings.get("volatility_reaction",{}).get("emergency_dca_on_extreme_fall",{}) #
        dca_price_step_conf = edca_conf.get("price_step_dca", {}) #
        if edca_conf.get("enabled") and dca_price_step_conf.get("type") == "atr_m1": #
            m1_tf_id_common = self.app.get_tf_id_by_common_name("M1") #
            m1_tf_id_to_use = m1_tf_id_common or DEFAULT_M1_TF_ID #

            is_m1_already_present = any(
                (tc.get("timeframe_id") == m1_tf_id_to_use if tc else False) or \
                (tc.get("binance_interval_notation") == Client.KLINE_INTERVAL_1MINUTE if tc else False)
                for tc in all_tf_configs_in_strategy if isinstance(tc, dict)
            )
            if not is_m1_already_present:
                m1_config_resolved = self.app._get_tf_config_by_id(m1_tf_id_to_use) #
                if m1_config_resolved:
                    all_tf_configs_in_strategy.append(m1_config_resolved)
                else:
                    print(f"MarketAnalyzer Warning: Could not resolve config for M1 TF ('{m1_tf_id_to_use}') needed for DCA.")

        # Перевірка ТФ для стабілізації
        stab_conf = self.strategy_settings.get("stabilization_logic", {}) #
        if stab_conf.get("enabled_after_volatility_event", False): #
            stab_tf_notation = stab_conf.get("monitoring_tf_binance_notation", "5m") #
            stab_tf_id_common = self.app.get_tf_id_by_binance_interval(stab_tf_notation) #
            if stab_tf_id_common:
                 is_stab_tf_present = any((tc.get("timeframe_id") == stab_tf_id_common if tc else False) for tc in all_tf_configs_in_strategy if isinstance(tc, dict))
                 if not is_stab_tf_present:
                     stab_tf_config_resolved = self.app._get_tf_config_by_id(stab_tf_id_common) #
                     if stab_tf_config_resolved: all_tf_configs_in_strategy.append(stab_tf_config_resolved)

        # Перевірка ТФ для MACD Control
        macd_ctrl_conf = self.strategy_settings.get("macd_control_config", {}) #
        if macd_ctrl_conf.get("enabled", False): #
            macd_tf_id_ctrl = macd_ctrl_conf.get("control_timeframe_id") #
            if macd_tf_id_ctrl:
                is_macd_tf_present = any((tc.get("timeframe_id") == macd_tf_id_ctrl if tc else False) for tc in all_tf_configs_in_strategy if isinstance(tc, dict))
                if not is_macd_tf_present:
                    macd_tf_config_resolved = self.app._get_tf_config_by_id(macd_tf_id_ctrl) #
                    if macd_tf_config_resolved: all_tf_configs_in_strategy.append(macd_tf_config_resolved)
        
        # Перевірка ТФ для Фібоначчі
        fib_conf = self.strategy_settings.get("level_sourcing_and_processing", {}).get("fibonacci_levels", {}) #
        if fib_conf.get("enabled", False): #
            for fib_tf_id in fib_conf.get("source_timeframes", []): #
                if fib_tf_id:
                    is_fib_tf_present = any((tc.get("timeframe_id") == fib_tf_id if tc else False) for tc in all_tf_configs_in_strategy if isinstance(tc, dict))
                    if not is_fib_tf_present:
                        fib_tf_config_resolved = self.app._get_tf_config_by_id(fib_tf_id) #
                        if fib_tf_config_resolved: all_tf_configs_in_strategy.append(fib_tf_config_resolved)


        if not all_tf_configs_in_strategy:
            # Якщо список порожній, використовуємо дефолтний ТФ
            base_tf_list = gte_conf.get("timeframes_config", []) #
            first_tf_from_gte = base_tf_list[0] if base_tf_list else {}
            default_tf_id_to_analyze = first_tf_from_gte.get("timeframe_id", DEFAULT_M15_TF_ID) #
            resolved_default_tf = self.app._get_tf_config_by_id(default_tf_id_to_analyze) #
            if resolved_default_tf:
                tf_list_to_analyze_configs_final.append(resolved_default_tf)
        else:
            # Створюємо унікальний список конфігурацій ТФ
            unique_tf_configs = {}
            for tf_c in all_tf_configs_in_strategy:
                if isinstance(tf_c, dict) and tf_c.get("timeframe_id") and tf_c.get("timeframe_id") not in unique_tf_configs:
                    unique_tf_configs[tf_c.get("timeframe_id")] = tf_c # Використовуємо оригінальний конфіг, якщо він вже повний
            tf_list_to_analyze_configs_final = list(unique_tf_configs.values())


        if not tf_list_to_analyze_configs_final:
            print("MarketAnalyzer: No timeframe configurations found to analyze.")
            return self.analysis_results_by_tf

        for tf_conf_item in tf_list_to_analyze_configs_final:
            tf_id_item = tf_conf_item.get("timeframe_id")
            if not tf_id_item:
                print(f"MarketAnalyzer: Skipping TF due to missing timeframe_id in config: {tf_conf_item}")
                continue

            df_for_tf_item = self.current_symbol_klines_dfs.get(tf_id_item)
            if df_for_tf_item is None or df_for_tf_item.empty:
                self.analysis_results_by_tf[tf_id_item] = {"error": f"No kline DataFrame for {tf_id_item}", "tf_id": tf_id_item}
                continue

            self.analysis_results_by_tf[tf_id_item] = self._calculate_indicators_for_one_tf(df_for_tf_item, tf_conf_item)

        return self.analysis_results_by_tf

    def get_analysis_for_tf(self, timeframe_id: str) -> dict | None:
        return self.analysis_results_by_tf.get(timeframe_id)

    def get_klines_df_for_tf(self, timeframe_id: str) -> pd.DataFrame | None:
        return self.current_symbol_klines_dfs.get(timeframe_id)