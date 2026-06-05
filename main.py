import time
import threading
import json
import queue
from flask import Flask, jsonify, request, abort, Response
from flask_cors import CORS
from functools import wraps
import config
import k8s_logic
import maas_logic
from werkzeug.security import check_password_hash
import base64
import os
import jwt
import datetime

app = Flask(__name__)
CORS(app)

power_transitions = {}

JWT_SECRET = os.getenv("JWT_SECRET", " ")

# MOTORUL PUB/SUB: Impinge datele instantaneu catre React fara delay
class LiveBroadcaster:
    def __init__(self):
        self.listeners = []
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=10)
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def broadcast(self, data):
        payload = json.dumps(data)
        with self.lock:
            for q in self.listeners:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

broadcaster = LiveBroadcaster()

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        if 'Authorization' in request.headers:
            token = request.headers['Authorization']
            if token.startswith("Bearer "):
                token = token.split(" ")[1]

        if not token and 'token' in request.args:
            token = request.args.get('token')

        if not token:
            return jsonify({"status": "error", "message": "Token-ul lipseste!"}), 401

        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.user_identity = data.get("identity")
        except jwt.ExpiredSignatureError:
            return jsonify({"status": "error", "message": "Token-ul a expirat!"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"status": "error", "message": "Token invalid!"}), 401

        return f(*args, **kwargs)
    return decorated

# FUNCTIE DE INITIALIZARE K8S LA PORNIRE
def uncordon_all_assigned_nodes():
    print("[INIT] Se initializeaza deblocarea tuturor nodurilor din cluster...")
    try:
        machines = maas_logic.maas.get_machines()
        assigned_sids = set(sid for sid in config.assignments.values() if sid)

        for m in machines:
            sid = m.get('system_id')
            hostname = m.get('hostname')
            
            if sid in assigned_sids and hostname:
                print(f"[INIT] Trimitem comanda UNCORDON la pornire pentru: {hostname} ({sid})")
                k8s_logic.uncordon_node(hostname)
                
        print("[INIT] Procesul de uncordon initial a fost finalizat.")
    except Exception as e:
        print(f"[INIT] Eroare neasteptata la uncordon_all_assigned_nodes: {e}")


# DAEMON THREAD 1: Colecteaza date din MAAS 
def maas_cache_collector():
    print("[DAEMON MAAS] Colectorul hardware a pornit ... ")
    while True:
        try:
            machines = maas_logic.maas.get_machines()
            
            if isinstance(machines, list):
                with config.state_lock:
                    sid_to_outlet = {sid: outlet for outlet, sid in config.assignments.items() if sid}
                
                with config.config_lock:
                    old_metrics_cache = {dev["system_id"]: dev.get("metrics") for dev in config.global_cached_data.values() if "system_id" in dev}
                    
                    fresh_status = {}
                    has_structural_changes = False
                    
                    for maas_node in machines:
                        sid = maas_node.get('system_id')
                        hostname = maas_node.get('hostname', 'Unknown')
                        assigned_outlet = sid_to_outlet.get(sid)
                        
                        maas_reported_state = maas_node.get("power_state", "Unknown").upper()
                        final_power_state = maas_reported_state
                        
                        if sid in power_transitions:
                            target_state, timestamp = power_transitions[sid]
                            if time.time() - timestamp < 15:
                                final_power_state = target_state
                                if maas_reported_state == target_state:
                                    power_transitions.pop(sid, None)
                            else:
                                power_transitions.pop(sid, None)

                        existing_metrics = old_metrics_cache.get(sid, {
                            "cpu_percentage": None, "memory_percentage": None, "memory_used": None, 
                            "memory_max": None, "pods_count": 0, "pods": []
                        })
                        
                        # Generam payload-ul curent primit de la API
                        payload = {
                            "allocated": bool(assigned_outlet),
                            "system_id": sid,
                            "hostname": hostname,
                            "status": maas_node.get("status_name", "Unknown"),
                            "software_version": f"{maas_node.get('osystem', 'linux').capitalize()} ({maas_node.get('distro_series', 'unknown').capitalize()})" if maas_node.get("status_name") == "Deployed" else None,
                            "power_type": maas_node.get("power_type", "manual"),
                            "hardware_product": (maas_node.get("hardware_info") or {}).get("system_product") or maas_node.get("architecture", "Raspberry Pi"),
                            "ip_address": maas_node.get("ip_addresses", ["N/A"])[0] if maas_node.get("ip_addresses") else "N/A",
                            "power_state": final_power_state, 
                            "memory": maas_node.get("memory"),
                            "metrics": existing_metrics
                        }
                        
                        key = assigned_outlet if assigned_outlet else f"unallocated_{sid}"
                        fresh_status[key] = payload
                        
                        # Trimite doar daca se modifica date
                        old_dev = config.global_cached_data.get(key)
                        if old_dev:
                            changes = []
        
                            if old_dev.get("status") != payload["status"]:
                                changes.append(f"Status: {old_dev.get('status')} -> {payload['status']}")
                                
                            if old_dev.get("software_version") != payload["software_version"]:
                                changes.append(f"OS: {old_dev.get('software_version')} -> {payload['software_version']}")
                                
                            if old_dev.get("ip_address") != payload["ip_address"]:
                                changes.append(f"IP: {old_dev.get('ip_address')} -> {payload['ip_address']}")
                                
                            if old_dev.get("power_type") != payload["power_type"]:
                                changes.append(f"Power Type: {old_dev.get('power_type')} -> {payload['power_type']}")
                                
                            if old_dev.get("power_state") != payload["power_state"]:
                                changes.append(f"Power State: {old_dev.get('power_state')} -> {payload['power_state']}")
                            
                            # Daca avem cel putin o modificare structurala, o logam si marcam flag-ul
                            if changes:
                                print(f"[SCHIMBARE STARE] Nodul '{hostname}' s-a modificat: {', '.join(changes)}")
                                has_structural_changes = True
                        else:
                            # Dispozitiv nou aparut in retea sau proaspat alocat
                            print(f"[DISPOZITIV NOU] '{hostname}' a fost adaugat in cache pe slotul: {key}")
                            has_structural_changes = True
                    
                    # Verificam si scenariul in care un nod a fost sters complet din MAAS
                    if set(config.global_cached_data.keys()) != set(fresh_status.keys()):
                        print("[COMPONENT CLEANUP] Structura nodurilor din MAAS s-a modificat la nivel de identificatori.")
                        has_structural_changes = True
                        
                    # Salvam noua stare în cache-ul global din RAM
                    config.global_cached_data = fresh_status
                
                # Trimitem broadcast catre React DOAR daca s-a modificat un parametru critic
                if has_structural_changes:
                    broadcaster.broadcast(config.global_cached_data)
                    
        except Exception as e:
            print(f"[DAEMON MAAS] Eroare la citire prin MAAS API: {e}")
            
        time.sleep(3)

# DAEMON THREAD 2: Colecteaza lista de pod-uri din K3s asincron 
def k3s_pod_collector():
    print("[DAEMON K3s Pods] Colectorul de pod-uri a pornit...")
    while True:
        try:
            pod_metrics = k8s_logic.get_k8s_pod_metrics()
            with config.config_lock:
                for key, dev in config.global_cached_data.items():
                    hostname = dev.get("hostname")
                    power_state = str(dev.get("power_state", "UNKNOWN")).upper()
                    
                    if "metrics" in dev and dev["metrics"] is not None:
                        if power_state == "OFF":
                            dev["metrics"]["pods"] = []
                            dev["metrics"]["pods_count"] = 0
                            continue
                    
                    if hostname in pod_metrics:
                        if "metrics" not in dev or dev["metrics"] is None:
                            dev["metrics"] = {"cpu_percentage": 0, "memory_percentage": 0, "memory_used": 0, "memory_max": 0}
                        dev["metrics"]["pods"] = pod_metrics[hostname]
                        dev["metrics"]["pods_count"] = len(pod_metrics[hostname])
                        
            broadcaster.broadcast(config.global_cached_data)
        except Exception as e:
            print(f"[DAEMON K3s Pods] Eroare cluster: {e}")
            
        time.sleep(4)

@app.route('/api/outlets', methods=['GET'])
@token_required
def get_outlets():
    return jsonify({"status": "success", "data": config.assignments, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")})

@app.route('/api/search', methods=['POST', 'GET'])
@token_required
def force_search():
    try:
        maas_logic.sync_initial_state()
        maas_logic.perform_scan()
        maas_logic.deploy_ready_nodes()
        config.scan_now_event.set()
        return jsonify({"status": "success", "message": "Scanare finalizata.", "active_assignments": config.assignments}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/api/live', methods=['GET'])
@token_required
def get_live_data():
    with config.config_lock:
        return jsonify({"status": "success", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "outlets": config.global_cached_data}), 200

@app.route('/api/live-stream', methods=['GET'])
@token_required
def live_stream():
    def generate():
        print("[SSE Pipeline] Un client React s-a conectat la fluxul din RAM.")
        q = broadcaster.subscribe()
        with config.config_lock:
            yield f"data: {json.dumps(config.global_cached_data)}\n\n"
        try:
            while True:
                payload = q.get()
                yield f"data: {payload}\n\n"
        except GeneratorExit:
            print("[SSE Pipeline] Client deconectat.")
        finally:
            broadcaster.unsubscribe(q)
            
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/toggle-power', methods=['POST'])
@token_required
def toggle_power():
    try:
        data = request.get_json() or {}
        system_id = data.get('system_id')
        action = data.get('action')

        if not system_id or action not in ['on', 'off']: return abort(400)

        power_transitions[system_id] = (action.upper(), time.time())

        with config.config_lock:
            for key, dev in config.global_cached_data.items():
                if dev.get("system_id") == system_id:
                    dev["power_state"] = action.upper()
                    
                    dev["metrics"] = {
                        "cpu_percentage": 0,
                        "memory_percentage": 0,
                        "memory_used": 0,
                        "memory_max": 0,
                        "cpu_temperature": None,
                        "uptime": "0h 0m",
                        "download_speed_kbps": 0,
                        "upload_speed_kbps": 0,
                        "pods_count": 0,
                        "pods": []
                    }
        
        broadcaster.broadcast(config.global_cached_data)

        maas_action = "power_on" if action == "on" else "power_off"
        threading.Thread(
            target=maas_logic.maas.perform_machine_action, 
            args=(system_id, maas_action), 
            daemon=True
        ).start()
        
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/pod-action', methods=['POST'])
@token_required
def pod_action():
    try:
        data = request.get_json() or {}
        success, err = k8s_logic.handle_pod_action(data.get('pod_name'), data.get('namespace'), data.get('action'))
        if not success: return jsonify({"status": "error", "message": f"K8s Error: {err}"}), 500
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    if check_password_hash(config.DASHBOARD, data.get('password', '')):
        payload = {
            "identity": "admin",
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=12)
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
        return jsonify({"status": "success", "token": token}), 200
        
    return jsonify({"status": "error", "message": "Parola incorecta!"}), 401

@app.route('/api/settings', methods=['GET', 'POST'])
@token_required
def handle_settings():
    if request.method == 'GET':
        return jsonify({"status": "success", "settings": config.load_dynamic_config()}), 200

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Payload JSON lipsa sau malformat."}), 400
        
    try:
        if config.save_dynamic_config(data):
            maas_logic.update_all_assigned_power_params()
            return jsonify({"status": "success"}), 200
        else:
            return jsonify({"status": "error", "message": "Nu s-au putut salva datele pe disc. Verificati permisiunile."}), 500
    except Exception as route_err:
        print(f"[CRITICAL] Eroare neprevazuta in handle_settings: {route_err}")
        return jsonify({"status": "error", "message": str(route_err)}), 500

@app.route('/api/maas/kernels', methods=['GET'])
@token_required
def get_maas_kernels():
    return jsonify({"status": "success", "kernels": maas_logic.get_available_maas_kernels()}), 200

@app.route('/api/assign-outlet', methods=['POST'])
@token_required
def assign_outlet():
    try:
        data = request.get_json() or {}
        system_id = data.get('system_id')
        new_outlet = data.get('new_outlet')         
        
        if not system_id: return abort(400)

        if str(new_outlet).lower() == 'none':
            with config.state_lock:
                for out, sid in list(config.assignments.items()):
                    if sid == system_id: config.assignments[out] = None
            
            maas_logic.maas.update_machine(system_id, {"power_type": "manual"})
            return jsonify({"status": "success"}), 200

        with config.state_lock:
            if config.assignments.get(new_outlet) is not None: return abort(400)
            for out, sid in list(config.assignments.items()):
                if sid == system_id: config.assignments[out] = None
            config.assignments[new_outlet] = system_id
        
        params = maas_logic.get_power_params(new_outlet)
        maas_logic.maas.update_machine(system_id, params)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/agent-metrics', methods=['POST'])
def receive_agent_metrics():
    try:
        data = request.get_json()
        if not data: return jsonify({"status": "error"}), 400
            
        hostname = data.get("hostname")
        
        with config.config_lock:
            updated_any = False
            for key, dev in config.global_cached_data.items():
                if dev.get("hostname") == hostname:
                    if "metrics" not in dev or dev["metrics"] is None:
                        dev["metrics"] = {"pods_count": 0, "pods": []}
                        
                    dev["metrics"]["cpu_percentage"] = data.get("cpu_percentage")
                    dev["metrics"]["memory_percentage"] = data.get("memory_percentage")
                    dev["metrics"]["memory_used"] = data.get("memory_used")
                    dev["metrics"]["memory_max"] = data.get("memory_max")
                    
                    dev["metrics"]["cpu_temperature"] = data.get("cpu_temperature")
                    dev["metrics"]["uptime"] = data.get("uptime")
                    dev["metrics"]["download_speed_kbps"] = data.get("download_speed_kbps")
                    dev["metrics"]["upload_speed_kbps"] = data.get("upload_speed_kbps")
                    updated_any = True
            
            if updated_any:
                broadcaster.broadcast(config.global_cached_data)
                
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/release-machine', methods=['POST'])
@token_required
def release_machine():
    try:
        data = request.get_json() or {}
        system_id = data.get('system_id')

        if not system_id: 
            return jsonify({"status": "error", "message": "System ID lipsa."}), 400

        with config.config_lock:
            for key, dev in config.global_cached_data.items():
                if dev.get("system_id") == system_id:
                    dev["status"] = "Releasing..."
                    dev["metrics"] = {
                        "cpu_percentage": 0, "memory_percentage": 0, 
                        "memory_used": 0, "memory_max": 0, 
                        "pods_count": 0, "pods": []
                    }
        
        broadcaster.broadcast(config.global_cached_data)

        def usb_erase_and_release_pipeline(sid):
            try:
                print(f"[PIPE-RELEASE] Pasul 1: Scanam discurile prin API pentru nodul: {sid}")
                url_disks = f"{maas_logic.maas.base_url}/machines/{sid}/blockdevices/"
                res = maas_logic.maas.session.get(url_disks)
                res.raise_for_status()
                disks_list = res.json()
                
                usb_disk_id = None
                if isinstance(disks_list, list):
                    for disk in disks_list:
                        id_path = str(disk.get("id_path", "")).lower()
                        name = str(disk.get("name", "")).lower()
                        
                        if "usb" in id_path or name.startswith("sd"):
                            usb_disk_id = disk.get("id")
                            print(f"[PIPE-RELEASE] S-a detectat stocarea USB! ID MAAS: {usb_disk_id} ({disk.get('model')})")
                            break

                if usb_disk_id:
                    print(f"[PIPE-RELEASE] Pasul 2: Pornim nodul (Power ON) pentru {sid}...")
                    maas_logic.maas.perform_machine_action(sid, "power_on")
                    time.sleep(12)

                    print(f"[PIPE-RELEASE] Pasul 3: Executam stergerea pe stick-ul USB (ID: {usb_disk_id})...")
                    url_wipe = f"{maas_logic.maas.base_url}/machines/{sid}/blockdevices/{usb_disk_id}/"
                    wipe_res = maas_logic.maas.session.post(url_wipe, data={"op": "wipe"})
                    wipe_res.raise_for_status()
                    time.sleep(10)

                    print(f"[PIPE-RELEASE] Pasul 4: Inchidem nodul (Power OFF) pentru {sid}...")
                    maas_logic.maas.perform_machine_action(sid, "power_off")
                    time.sleep(12)
                else:
                    print("[PIPE-RELEASE] [WARNING] Nu s-a detectat niciun mediu USB. Se sare peste pasii de alimentare.")

                print(f"[PIPE-RELEASE] Pasul Final: Se trimite comanda simpla de Release pentru {sid}...")
                maas_logic.maas.perform_machine_action(sid, "release")
                print(f"[PIPE-RELEASE] [SUCCESS] Secventa completa de release pentru {sid} a fost finalizata!")

            except Exception as pipeline_err:
                print(f"[PIPE-RELEASE] [ERROR] Eroare in pipeline-ul de fundal: {pipeline_err}")

        threading.Thread(target=usb_erase_and_release_pipeline, args=(system_id,), daemon=True).start()
        return jsonify({"status": "success", "message": "Secventa automatizata custom de Release a pornit in background."}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/deploy-machine', methods=['POST'])
@token_required
def deploy_machine():
    try:
        data = request.get_json() or {}
        system_id = data.get('system_id')

        if not system_id: 
            return jsonify({"status": "error", "message": "System ID lipsa."}), 400

        dyn_cfg = config.load_dynamic_config()
        default_kernel = dyn_cfg.get("maas_deployment", {}).get("default_kernel", "generic")

        deploy_params = {"hwe_kernel": default_kernel}

        if os.path.exists(config.USER_DATA_PATH):
            with open(config.USER_DATA_PATH, "rb") as f:
                encoded_data = base64.b64encode(f.read()).decode('utf-8')
            deploy_params["user_data"] = encoded_data
        else:
            return jsonify({"status": "error", "message": "Fisierul cloud-init nu a fost gasit."}), 500

        with config.config_lock:
            for key, dev in config.global_cached_data.items():
                if dev.get("system_id") == system_id:
                    dev["status"] = "Deploying..."
        
        broadcaster.broadcast(config.global_cached_data)

        threading.Thread(
            target=maas_logic.maas.perform_machine_action, 
            args=(system_id, "deploy", deploy_params), 
            daemon=True
        ).start()

        return jsonify({"status": "success", "message": "Deploy manual initiat cu succes folosind scriptul unificat."}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
        
@app.route('/api/rename-machine', methods=['POST'])
@token_required
def rename_machine():
    try:
        data = request.get_json() or {}
        system_id = data.get('system_id')
        new_hostname = data.get('new_hostname')

        if not system_id or not new_hostname:
            return jsonify({"status": "error", "message": "Date incomplete pentru redenumire."}), 400

        maas_logic.maas.update_machine(system_id, {"hostname": new_hostname})

        with config.config_lock:
            for key, dev in config.global_cached_data.items():
                if dev.get("system_id") == system_id:
                    dev["hostname"] = new_hostname
                    
        broadcaster.broadcast(config.global_cached_data)
        return jsonify({"status": "success", "message": "Nodul a fost redenumit cu succes in MAAS."}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/machine-logs', methods=['GET'])
@token_required
def get_machine_logs():
    try:
        system_id = request.args.get('system_id')
        if not system_id: return jsonify({"status": "error", "message": "System ID lipsa"}), 400
        
        try:
            url_events = f"{maas_logic.maas.base_url}/events/"
            res = maas_logic.maas.session.get(url_events, params={"node": system_id, "limit": 25})
            res.raise_for_status()
            events_data = res.json()
        except Exception as err:
            return jsonify({"status": "success", "logs": [f"[{time.strftime('%H:%M:%S')}] [WARNING] Eroare MAAS API: {err}"]}), 200
        
        log_lines = []
        events = events_data
        if isinstance(events_data, dict) and "events" in events_data:
            events = events_data["events"]
            
        if isinstance(events, list) and len(events) > 0:
            for ev in events:
                if not isinstance(ev, dict): continue
                description = ev.get("description") or ev.get("type") or "Eveniment inregistrat in MAAS pipeline"
                
                created = ev.get("created", "")
                if created:
                    time_part = created.split(".")[0].replace("T", " ")
                else:
                    time_part = time.strftime("%Y-%m-%d %H:%M:%S")
                    
                current_line = f"[{time_part}] {description}"
                if current_line not in log_lines:
                    log_lines.append(current_line)
        else:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            log_lines.append(f"[{current_time}] [INFO] Pipeline initializat. Nu exista evenimente recente in MAAS pentru acest nod.")
            
        return jsonify({"status": "success", "logs": log_lines}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def start_background_threads():
    threading.Thread(target=maas_logic.maas_sync_loop, daemon=True).start()
    threading.Thread(target=maas_logic.k8s_autoscaler_loop, daemon=True).start()
    threading.Thread(target=maas_cache_collector, daemon=True).start()
    threading.Thread(target=k3s_pod_collector, daemon=True).start()
    k8s_logic.start_k8s_pod_watcher()

if __name__ == "__main__":
    maas_logic.sync_initial_state()
    uncordon_all_assigned_nodes()
    start_background_threads()
    app.run(host='0.0.0.0', port=5001, threaded=True)