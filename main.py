import subprocess
import json
import time
import base64
import os
import threading
from flask import Flask, jsonify

# --- CONFIGURARE ---
MAAS_API_KEY = os.getenv("MAAS_API_KEY", "CHEIA_TA_AICI")
MAAS_URL = os.getenv("MAAS_URL", "http://localhost:5240/MAAS")
MAAS_PROFILE = "admin"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_PATH = os.path.join(BASE_DIR, "rpi_cloudinit.yaml")

OUTLETS = ["p1", "p2", "p3"]
assignments = {outlet: None for outlet in OUTLETS}

scan_now_event = threading.Event()
app = Flask(__name__)

# --- FUNCTII MAAS ---
def run_cmd(cmd_list):
    result = subprocess.run(cmd_list, capture_output=True, text=True)
    return result

def run_maas_cmd(cmd_list):
    full_cmd = ["maas", MAAS_PROFILE] + cmd_list
    result = run_cmd(full_cmd)
    if result.returncode != 0:
        return None, result.stderr
    try:
        return json.loads(result.stdout), None
    except:
        return result.stdout, None

def setup_maas():
    print("Autentificare la MAAS...")
    run_cmd(["maas", "login", MAAS_PROFILE, MAAS_URL, MAAS_API_KEY])

def sync_initial_state():
    global assignments
    print("[DEEP SYNC] Sincronizare stare initiala din parametrii de Power...")
    
    # 1. Obtinem lista de masini
    machines, err = run_maas_cmd(["machines", "read"])
    if err or not isinstance(machines, list):
        print(f"Eroare la citirea listei de masini: {err}")
        return

    for m in machines:
        sid = m.get('system_id')
        hostname = m.get('hostname')
        
        # 2. Interogam MAAS pentru parametrii de power detaliati
        p_params, p_err = run_maas_cmd(["machine", "power-parameters", sid])
        
        if p_err or not isinstance(p_params, dict):
            continue

        # 3. Cautam prizele p1, p2, p3 in valorile returnate
        found_for_node = False
        for val in p_params.values():
            if not isinstance(val, str):
                continue
            
            for outlet in OUTLETS:
                if f"/maaspower/{outlet}" in val:
                    assignments[outlet] = sid
                    print(f"[DETECTAT] {hostname} ({sid}) este alocat pe priza {outlet}")
                    found_for_node = True
                    break
            
            if found_for_node:
                break

    print(f"Stare finala dupa sincronizare: {assignments}")

def get_power_params(outlet_name):
    base_url = f"http://localhost:5000/maaspower/{outlet_name}"
    return {
        "power_type": "webhook",
        "power_parameters_power_on_uri": f"{base_url}/on",
        "power_parameters_power_off_uri": f"{base_url}/off",
        "power_parameters_power_query_uri": f"{base_url}/query",
        "power_parameters_power_on_regex": ".*running.*",
        "power_parameters_power_off_regex": ".*stopped.*",
        "power_parameters_power_user": "stefan",
        "power_parameters_power_pass": "Licenta2026"
    }


def perform_scan():
    """Functia principala de verificare si alocare cu Hard Reset."""
    global assignments
    print(f"\n[{time.strftime('%H:%M:%S')}] Scanare in curs...")
    
    machines, err = run_maas_cmd(["machines", "read"])
    if err:
        print(f"Eroare MAAS: {err}")
        return

    # 1. Curatam prizele eliberate
    current_sids = [m['system_id'] for m in machines]
    for out, assigned_sid in assignments.items():
        if assigned_sid and assigned_sid not in current_sids:
            print(f"Slotul {out} s-a eliberat.")
            assignments[out] = None

    # 2. Identificam noduri noi
    pending_nodes = [m for m in machines if m['status_name'] in ["Ready", "New"] 
                     and m['system_id'] not in assignments.values()]

    for node in pending_nodes:
        free_outlet = next((o for o in OUTLETS if assignments[o] is None), None)
        if free_outlet:
            sid = node['system_id']
            print(f"[ALOCARE] Nod {sid} -> Priza {free_outlet}")
            
            # A. Update Power Settings in MAAS
            params = get_power_params(free_outlet)
            update_cmd = ["machine", "update", sid]
            for key, value in params.items():
                update_cmd.append(f"{key}={value}")
            run_maas_cmd(update_cmd)

            # B. [HARD RESET] Power OFF
            print(f"[POWER OFF] Trimitere comanda oprire catre {free_outlet}...")
            off_url = f"http://localhost:5000/maaspower/{free_outlet}/off"
            run_cmd(["curl", "-s", "-u", "stefan:Licenta2026", "-X", "POST", off_url])

            # C. Asteptam 10 secunde
            print("Pauza 10 secunde pentru power cycle...")
            time.sleep(10)

            # --- PASUL NOU: CHECK POWER VIA MAAS ---
            print(f"[MAAS QUERY] Interogam MAAS despre starea nodului {sid}...")
            state_info, p_err = run_maas_cmd(["machine", "query-power-state", sid])
            
            if state_info and isinstance(state_info, dict):
                actual_state = state_info.get("state", "unknown")
                print(f"MAAS raporteaza status: {actual_state}")
                
                if actual_state == "off":
                    print("Confirmare MAAS: Nodul este stins.")
                else:
                    print(f"Atentie: MAAS inca vede nodul ca '{actual_state}'.")
            else:
                print(f"Nu am putut obtine starea power de la MAAS: {p_err}")

            # D. Lansam Commissioning
            print(f"[COMMISSION] Lansam procedura de commissioning pentru {sid}...")
            run_maas_cmd([
                "machine", "commission", sid,  "enable_ssh=1"])

            assignments[free_outlet] = sid
        else:
            print("Atentie: Toate prizele sunt ocupate.")
            break

def deploy_ready_nodes():
    global assignments
    machines, err = run_maas_cmd(["machines", "read"])
    if err: return

    active_sids = [sid for sid in assignments.values() if sid is not None]

    for m in machines:
        sid = m['system_id']
        status = m['status_name']
        
        if sid in active_sids and status == "Ready":
            print(f"Nodul {sid} este Ready. Pregatim Deploy cu Cloud-Init...")

            if os.path.exists(USER_DATA_PATH):
                with open(USER_DATA_PATH, "rb") as f:
                    encoded_data = base64.b64encode(f.read()).decode('utf-8')
                
                print(f"Trimitere user_data (Base64) pentru {sid}...")
                run_maas_cmd([
                    "machine", "deploy", sid, 
                    f"user_data={encoded_data}"
                ])
            else:
                print(f"FISIERUL {USER_DATA_PATH} NU A FOST GASIT! Deploy simplu...")
                run_maas_cmd(["machine", "deploy", sid])
                
def monitor_loop():
    while True:
        perform_scan()
        deploy_ready_nodes()

        print(f"[{time.strftime('%H:%M:%S')}] Asteptam 60s pentru urmatoarea interogare...")
        interrupted = scan_now_event.wait(timeout=300)
        if interrupted:
            scan_now_event.clear()
            print("Scanare fortata detectata via API!")

# --- ENDPOINT-URI API ---
@app.route('/outlets', methods=['GET'])
def get_outlets():
    return jsonify({
        "status": "success",
        "data": assignments,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/search', methods=['POST', 'GET'])
def force_search():
    print(f"[{time.strftime('%H:%M:%S')}] API Request: Fortare scanare completa...")
    
    try:
        sync_initial_state()
        perform_scan()
        deploy_ready_nodes()
        scan_now_event.set()
        
        return jsonify({
            "status": "success",
            "message": "Scanare si procesare finalizate cu succes.",
            "timestamp": time.strftime("%H:%M:%S"),
            "active_assignments": assignments
        }), 200

    except Exception as e:
        print(f"Eroare in timpul executiei /search: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"A aparut o eroare: {str(e)}"
        }), 500

@app.route('/favicon.ico')
def favicon():
    return '', 204

if __name__ == "__main__":
    setup_maas()
    sync_initial_state()
    
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5001)