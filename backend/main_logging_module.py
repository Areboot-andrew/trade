# main_logging_module.py
import queue
import threading
import tkinter as tk # For tk.NORMAL, tk.END, tk.DISABLED
from datetime import datetime
import logging # Standard Python logging

# It's good practice to configure basic logging at the application entry point,
# but if this module is self-contained for file logging, it can have its own setup.
# However, for consistency, it's better if the main app handles the basicConfig.
# We will assume the main app might call an init function from here if needed.

LOG_FILE_PATH = "app_debug_price_flow.txt" # Default log file path

def initialize_logging(app_instance):
    """
    Initializes the logging queue and starts the log writer thread.
    This should be called once when the application starts.
    """
    if not hasattr(app_instance, 'log_queue') or app_instance.log_queue is None:
        app_instance.log_queue = queue.Queue()
        # Ensure _gui_queue_processing_active is available on app_instance
        if not hasattr(app_instance, '_gui_queue_processing_active'):
            app_instance._gui_queue_processing_active = True # Default to active

    if not hasattr(app_instance, 'log_thread') or \
       not (app_instance.log_thread and app_instance.log_thread.is_alive()):
        app_instance.log_thread = threading.Thread(
            target=lambda: _log_writer(app_instance.log_queue, app_instance), # Pass app_instance
            daemon=True,
            name="LogWriterThread"
        )
        app_instance.log_thread.start()
        log_message_to_file_internal(app_instance.log_queue, "Logging system initialized.")
    else:
        log_message_to_file_internal(app_instance.log_queue, "Logging system already initialized.")


def _log_writer(log_queue_instance: queue.Queue, app_instance):
    """
    Writes messages from the log_queue to a debug file.
    This function runs in a separate thread.
    It now checks app_instance._gui_queue_processing_active to know when to stop.
    """
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n--- Log Session Started: {datetime.now()} ---\n")
    except IOError as e:
        # Use standard print for critical errors if logging to file itself fails
        print(f"CRITICAL: Could not open log file {LOG_FILE_PATH}: {e}")
        # Optionally, could try logging to standard Python logger as a fallback
        logging.critical(f"Could not open log file {LOG_FILE_PATH}: {e}")
        return # Stop the thread if file can't be opened

    while True:
        try:
            log_type, message = log_queue_instance.get(timeout=0.5) # Use the passed queue instance
            if log_type == "STOP_LOGGING":
                with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                    f.write(f"--- Log Session Explicitly Stopped: {datetime.now()} ---\n")
                break
            
            # Ensure the message is a string
            message_str = str(message)

            with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now()} - {threading.current_thread().name} - {message_str}\n")
            log_queue_instance.task_done() # Mark task as done
        except queue.Empty:
            # Check if the main app is still running before breaking
            # This relies on app_instance having _gui_queue_processing_active attribute
            if hasattr(app_instance, '_gui_queue_processing_active') and \
               not app_instance._gui_queue_processing_active and \
               log_queue_instance.empty():
                break
        except IOError as e:
            print(f"Log writer IO error: {e} for message: {message_str if 'message_str' in locals() else 'N/A'}")
            logging.error(f"Log writer IO error: {e}")
            # Potentially pause and retry or stop if file becomes unwritable
            break # Stop if file becomes unwritable
        except Exception as e:
            # Generic error logging
            print(f"Log writer error: {e} for message: {message_str if 'message_str' in locals() else 'N/A'}")
            logging.error(f"Log writer error: {e}")
            # It's important to mark task_done even if there's an error processing it,
            # or the queue might block indefinitely on join() if that's used.
            if not log_queue_instance.empty(): # Check if get() was successful before task_done
                 try:
                    log_queue_instance.task_done()
                 except ValueError: # If task_done() is called too many times
                    pass


def log_message_to_file(app_instance, message: str, log_type: str = "app_log"):
    """
    Queues a message to be written to the debug log file.
    This is intended to be called from the MainApp or other modules.
    It now takes app_instance to access its log_queue.
    """
    if hasattr(app_instance, 'log_queue') and app_instance.log_queue is not None:
        app_instance.log_queue.put((log_type, message))
    else:
        # Fallback if log_queue is not initialized (e.g., early in startup or error)
        print(f"LOG_TO_FILE_ERROR (log_queue not initialized on app_instance): {message}")
        logging.warning(f"log_queue not initialized on app_instance. Message: {message}")

def log_message_to_file_internal(log_queue_instance: queue.Queue, message: str, log_type: str = "app_log"):
    """
    Internal helper to queue messages, used by initialize_logging before app_instance might be fully set up.
    """
    if log_queue_instance:
        log_queue_instance.put((log_type, message))
    else:
        print(f"LOG_TO_FILE_INTERNAL_ERROR (log_queue_instance is None): {message}")
        logging.warning(f"log_queue_instance is None in log_message_to_file_internal. Message: {message}")


def add_live_log(app_instance, message: str, level: str = "INFO"):
    """
    Adds a message to the live log display in the GUI and also logs it to the file.
    The textbox is now always in a "normal" state but with typing disabled via a binding.
    """
    timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    log_entry_for_gui = f"[{timestamp}] [{level.upper()}] {message}"
    log_entry_for_file = f"LIVE_LOG_MSG: {log_entry_for_gui}" # For file, to distinguish from other logs

    # Log to file via the queue
    log_message_to_file(app_instance, log_entry_for_file, log_type="live_event")

    # Update GUI
    if hasattr(app_instance, 'live_log_textbox') and app_instance.live_log_textbox.winfo_exists():
        try:
            # Текстове поле тепер завжди в стані NORMAL, тому не потрібно перемикати state
            app_instance.live_log_textbox.insert(tk.END, log_entry_for_gui + "\n")
            app_instance.live_log_textbox.see(tk.END)
        except Exception as e_log_gui:
            error_log_msg = f"LIVE_LOG_GUI_ERROR: {e_log_gui}. Original Msg: {log_entry_for_gui}"
            log_message_to_file(app_instance, error_log_msg, log_type="gui_error")
            print(error_log_msg)
    else:
        missing_gui_msg = f"LIVE_LOG_NO_TEXTBOX: GUI element 'live_log_textbox' not found or not visible. Original Msg: {log_entry_for_gui}"
        log_message_to_file(app_instance, missing_gui_msg, log_type="gui_warning")
        print(missing_gui_msg)


def stop_logging_thread(app_instance):
    """
    Signals the logging thread to stop and waits for it to join.
    """
    if hasattr(app_instance, 'log_queue') and app_instance.log_queue is not None:
        app_instance.log_queue.put(("STOP_LOGGING", None))
    
    if hasattr(app_instance, 'log_thread') and app_instance.log_thread and app_instance.log_thread.is_alive():
        log_message_to_file(app_instance, "Attempting to join logging thread...") # Log before join
        app_instance.log_thread.join(timeout=2.0) # Add a timeout
        if app_instance.log_thread.is_alive():
            log_message_to_file(app_instance, "Logging thread did not join in time.", log_type="warning")
            print("Warning: Logging thread did not join in time.")
        else:
            # Cannot log to file here as the thread is stopped.
            print("Logging thread joined successfully.")