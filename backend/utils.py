# utils.py
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from copy import deepcopy
import json
import os
# Немає прямих залежностей від constants.py на рівні модуля,
# але ADVANCED_CONFIG_FILE використовується в load_and_apply_profile_settings

def quantize_decimal(d, precision_str='0.00000001'): #
    if not isinstance(d, Decimal): #
        try: #
            d = Decimal(str(d)) #
        except InvalidOperation: #
            return Decimal('0') #
    return d.quantize(Decimal(precision_str), rounding=ROUND_HALF_UP) #

def load_json_file(filename: str, default_data=None) -> dict: #
    if default_data is None: #
        default_data = {} #
    if os.path.exists(filename): #
        try: #
            with open(filename, 'r', encoding='utf-8') as f: #
                content = f.read() #
                if not content.strip(): #
                    print(f"Warning: File {filename} is empty. Using default data.") #
                    return deepcopy(default_data) #
                return json.loads(content) #
        except (json.JSONDecodeError, IOError) as e: #
            print(f"Error loading JSON file {filename}: {e}. Using default data.") #
            return deepcopy(default_data) #
    print(f"File {filename} not found. Using default data.") #
    return deepcopy(default_data) #

# { /* NEW FUNCTION */ }
def save_json_file(filename: str, data_to_save, ensure_ascii=False, indent=4) -> bool:
    """
    Зберігає дані у файл JSON.
    :param filename: Ім'я файлу для збереження.
    :param data_to_save: Дані для збереження (словник або список).
    :param ensure_ascii: Якщо False, дозволяє не-ASCII символи (напр. кирилицю).
    :param indent: Відступ для форматування JSON.
    :return: True, якщо збереження успішне, інакше False.
    """
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=ensure_ascii, indent=indent)
        # print(f"Data successfully saved to {filename}")
        return True
    except (IOError, TypeError) as e:
        print(f"Error saving JSON file {filename}: {e}")
        return False
# { /* END NEW FUNCTION */ }

def set_nested_value(config_dict: dict, path_str: str, value_to_set): #
    keys = path_str.split('.') #
    current_level = config_dict #
    for i, key_part in enumerate(keys): #
        is_last_key = (i == len(keys) - 1) #
        array_name, index = None, None #
        processed_key = key_part #
        if '[' in key_part and ']' in key_part: #
            try: #
                array_name = key_part[:key_part.index('[')] #
                index_str = key_part[key_part.index('[')+1:key_part.index(']')] #
                index = int(index_str) #
                processed_key = array_name #
            except (ValueError, IndexError) as e_parse: #
                print(f"Error parsing path part '{key_part}' in '{path_str}': {e_parse}"); return False #
        
        if is_last_key: #
            if index is not None: #
                if not isinstance(current_level, dict) or processed_key not in current_level or \
                   not isinstance(current_level.get(processed_key), list) or index >= len(current_level[processed_key]): #
                    print(f"Error: Cannot set list element for path '{path_str}'. Array '{processed_key}' invalid or index '{index}' out of bounds."); return False #
                current_level[processed_key][index] = value_to_set #
            else: #
                if not isinstance(current_level, dict): #
                    print(f"Error: Cannot set key '{processed_key}' on non-dict in path '{path_str}'."); return False #
                current_level[processed_key] = value_to_set #
        else: #
            if index is not None: #
                if not isinstance(current_level, dict) or processed_key not in current_level or \
                   not isinstance(current_level.get(processed_key), list) or index >= len(current_level[processed_key]) or \
                   not isinstance(current_level[processed_key][index], (dict, list)): #
                    print(f"Error: Invalid array path or element type for traversal in '{path_str}' at '{processed_key}[{index}]'."); return False #
                current_level = current_level[processed_key][index] #
            else: #
                if not isinstance(current_level, dict): #
                     print(f"Error: Cannot traverse into non-dict for key '{processed_key}' in path '{path_str}'."); return False #

                if processed_key not in current_level or not isinstance(current_level[processed_key], dict): #
                    if not is_last_key : current_level[processed_key] = {} #
                current_level = current_level[processed_key] #
    return True #

def load_and_apply_profile_settings(profile_to_activate_name: str = None) -> dict: #
    from constants import ADVANCED_CONFIG_FILE # Імпорт тут, щоб уникнути циклічних залежностей #
    
    base_advanced_settings = load_json_file(ADVANCED_CONFIG_FILE) #
    if not base_advanced_settings or "error" in base_advanced_settings : #
        print(f"CRITICAL: Failed to load or parse {ADVANCED_CONFIG_FILE}. Advanced strategy settings will be unavailable.") #
        return {"error": f"Failed to load {ADVANCED_CONFIG_FILE}", "_applied_profile_name": "error_loading_config"} #

    working_settings = deepcopy(base_advanced_settings) #
    presets_config = base_advanced_settings.get("user_interface_presets", {}) #
    active_profile_name_to_apply = profile_to_activate_name or presets_config.get("default_aggression_profile_name", "") #

    active_profile_data = None #
    if active_profile_name_to_apply and presets_config.get("aggression_profiles"): #
        for profile in presets_config.get("aggression_profiles", []): #
            if profile.get("profile_name") == active_profile_name_to_apply: #
                active_profile_data = profile; break #

    if active_profile_data: #
        profile_display_name = active_profile_data.get('display_name', active_profile_name_to_apply) #
        print(f"Applying aggression profile: '{active_profile_name_to_apply}' (Display: '{profile_display_name}')") #
        overrides = active_profile_data.get("overrides", {}) #
        if overrides: #
            for path_str, value in overrides.items(): #
                if not set_nested_value(working_settings, path_str, value): #
                    print(f"  Warning (Profile Application): Failed to apply override for path '{path_str}' with value '{value}'.") #
    elif profile_to_activate_name: #
         default_profile_name_from_config = presets_config.get("default_aggression_profile_name", "") #
         if profile_to_activate_name != default_profile_name_from_config and default_profile_name_from_config: #
             print(f"Warning: Profile '{profile_to_activate_name}' not found. Attempting to load default: '{default_profile_name_from_config}'.") #
             return load_and_apply_profile_settings(default_profile_name_from_config) #
         else: #
             print(f"Warning: Profile '{profile_to_activate_name}' not found, and no valid default profile to fallback. Using base settings.") #
    
    working_settings["_applied_profile_name"] = active_profile_name_to_apply if active_profile_data else "base_settings" #
    if "user_interface_presets" in working_settings: #
        del working_settings["user_interface_presets"] #
    return working_settings #