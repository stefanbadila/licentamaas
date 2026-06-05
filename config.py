import os
import threading
import json
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MAAS_API_KEY = os.getenv("MAAS_API_KEY")
MAAS_URL = os.getenv("MAAS_URL")
MAAS_PROFILE = "admin"
USER_DATA_PATH = os.path.join(BASE_DIR, "rpi_cloudinit.yaml")
JSON_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

OUTLETS = ["p1", "p2", "p3"]
assignments = {outlet: None for outlet in OUTLETS}
inactive_nodes = {}

DASHBOARD = os.getenv("DASHBOARD_PASSWORD_HASH")
scan_now_event = threading.Event()

# Lock-uri pentru thread-safety
config_lock = threading.Lock()
state_lock = threading.Lock()

# MEMORY CACHE GLOBAL PENTRU ELIMINAREA LAG-ULUI
global_cached_data = {}

def load_dynamic_config():
    with config_lock:
        try:
            if not os.path.exists(JSON_CONFIG_PATH):
                return {}
            with open(JSON_CONFIG_PATH, "r") as f:
                data = json.load(f)
                # Ne asiguram ca datele incarcate sunt intotdeauna un dictionar
                return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"Eroare la citirea config.json: {e}")
            return {}

def save_dynamic_config(new_config):
    if not new_config or not isinstance(new_config, dict):
        print("Eroare: Payload-ul de configurare este invalid sau gol.")
        return False
    with config_lock:
        try:
            with open(JSON_CONFIG_PATH, "w") as f:
                json.dump(new_config, f, indent=2)
            return True
        except Exception as e:
            print(f"Eroare la scrierea in config.json: {e}")
            return False