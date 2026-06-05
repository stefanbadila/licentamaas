import os
import time
import base64
import threading
import requests
import config
import k8s_logic
from maas_api import MaasAPIClient

maas = MaasAPIClient(config.MAAS_URL, config.MAAS_API_KEY)

def sync_initial_state():
    print("[DEEP SYNC] Sincronizare stare initiala din parametrii de Power...")
    try:
        machines = maas.get_machines()
    except Exception as err:
        print(f"[WARNING] Eroare la citirea listei de masini prin API: {err}")
        return

    for m in machines:
        sid = m.get('system_id')
        hostname = m.get('hostname', 'Unknown')
        
        try:
            url = f"{maas.base_url}/machines/{sid}/"
            response = maas.session.get(url, params={"op": "power_parameters"})
            response.raise_for_status()
            p_params = response.json()
        except Exception as err:
            print(f"[WARNING] Nu s-au putut citi detaliile de power pentru {hostname} ({sid}): {err}")
            continue

        if not isinstance(p_params, dict):
            continue

        found_for_node = False
        for val in p_params.values():
            if not isinstance(val, str):
                continue
            for outlet in config.OUTLETS:
                if f"/{outlet}/" in val or f"/{outlet}" in val:
                    config.assignments[outlet] = sid
                    print(f"[DETECTAT] {hostname} ({sid}) este alocat pe priza {outlet}")
                    found_for_node = True
                    break
            if found_for_node:
                break
                
    print(f"[STATUS] Stare finala dupa sincronizare: {config.assignments}")

def get_power_params(outlet_name):
    dyn_cfg = config.load_dynamic_config()
    pw = dyn_cfg.get("power_webhook", {})
    base_url = pw.get("base_url", "http://localhost:5000/maaspower")
    user = pw.get("user", "stefan")
    password = pw.get("pass") or os.getenv("POWER_WEBHOOK_PASS") 
    
    return {
        "power_type": "webhook",
        "power_parameters_power_on_uri": f"{base_url}/{outlet_name}/on",
        "power_parameters_power_off_uri": f"{base_url}/{outlet_name}/off",
        "power_parameters_power_query_uri": f"{base_url}/{outlet_name}/query",
        "power_parameters_power_on_regex": ".*running.*",
        "power_parameters_power_off_regex": ".*stopped.*",
        "power_parameters_power_user": user,
        "power_parameters_power_pass": password
    }

def format_active_node_payload(maas_node, outlet_name, node_metrics, pod_metrics):
    hostname = maas_node.get("hostname", "Unknown")
    status = maas_node.get("status_name", "Unknown")
    os_name = maas_node.get("osystem", "linux").capitalize()
    distro_series = maas_node.get("distro_series", "unknown")
    hwe_kernel = maas_node.get("hwe_kernel", "")
    
    if hwe_kernel and "ga-" in hwe_kernel:
        version_number = hwe_kernel.replace("ga-", "")
    else:
        version_number = "20.xxx"
        
    if status == "Deployed":
        software_version = f"{os_name} {version_number} ({distro_series.capitalize()}) | Kernel: {hwe_kernel}"
    else:
        software_version = None
    
    power_type = maas_node.get("power_type", "Unknown")
    ip_list = maas_node.get("ip_addresses", [])
    ip_address = ip_list[0] if ip_list and isinstance(ip_list, list) else "N/A"
    hw_info = maas_node.get("hardware_info") or {}
    hardware_product = hw_info.get("system_product") or maas_node.get("architecture", "Raspberry Pi")
    
    node_res = node_metrics.get(hostname, None)
    node_pods = pod_metrics.get(hostname, [])

    if node_res:
        metrics_payload = {
            "cpu_percentage": node_res["cpu_percentage"],
            "memory_percentage": node_res["memory_percentage"],
            "memory_used": node_res["memory_used"],
            "memory_max": node_res["memory_max"],
            "pods_count": len(node_pods),
            "pods": node_pods
        }
    else:
        metrics_payload = {
            "cpu_percentage": None, "memory_percentage": None, "memory_used": None, 
            "memory_max": None, "pods_count": 0, "pods": []
        }

    return {
        "allocated": True,
        "system_id": maas_node.get("system_id"),
        "hostname": hostname,
        "status": status,
        "software_version": software_version,
        "power_type": power_type,
        "hardware_product": hardware_product,
        "ip_address": ip_address,
        "power_state": maas_node.get("power_state", "Unknown"),
        "memory": maas_node.get("memory"),
        "metrics": metrics_payload
    }

def _async_node_commission_pipeline(sid, free_outlet, pool_name, pw):
    try:
        print(f"[ASYNC-SCAN] S-a lansat configurarea in fundal pentru nodul {sid} pe priza {free_outlet}")
        params = get_power_params(free_outlet)
        params["pool"] = pool_name
        
        maas.update_machine(sid, params)

        print(f"[ASYNC-SCAN] Oprire via Webhook pentru aliniere pe slotul {free_outlet}...")
        off_url = f"{pw.get('base_url')}/{free_outlet}/off"
        
        try:
            requests.post(off_url, auth=(pw.get('user'), pw.get('pass')), timeout=5)
        except Exception as e:
            print(f"[WARNING] Webhook-ul de oprire rapida a esuat: {e}")

        time.sleep(10)
        
        try:
            state_info = maas.perform_machine_action(sid, "query_power_state")
            if isinstance(state_info, dict) and state_info.get("state", "unknown") == "off":
                print(f"[ASYNC-SCAN] Stare stinsa confirmata prin MAAS API pentru {sid}.")
        except Exception:
            pass
        
        try:
            maas.perform_machine_action(sid, "commission", {"enable_ssh": "1"})
            print(f"[ASYNC-SCAN] Commission initiat cu succes in MAAS pentru {sid}.")
        except Exception as e:
            print(f"[ASYNC-SCAN] [ERROR] Nu s-a putut porni operatia de commission pentru {sid}: {e}")
            
    except Exception as ex:
        print(f"[ASYNC-SCAN] [CRITICAL] Eroare neprevazuta in pipeline-ul asincron: {ex}")

def perform_scan():
    with config.config_lock:
        # Siguranta: Daca serviciul abia a pornit si cache-ul este gol, oprim scanarea
        # Asteptam ca maas_cache_collector sa faca prima interogare si sa umple memoria
        if not config.global_cached_data:
            return
        cached_devices = list(config.global_cached_data.values())

    dyn_cfg = config.load_dynamic_config()
    allocation_mode = dyn_cfg.get("policies", {}).get("allocation_mode", "auto")
    pool_name = dyn_cfg.get("maas_deployment", {}).get("resource_pool", "rpi-pool")
    pw = dyn_cfg.get("power_webhook", {})

    current_sids = [dev['system_id'] for dev in cached_devices]
    with config.state_lock:
        for out, assigned_sid in list(config.assignments.items()):
            if assigned_sid and assigned_sid not in current_sids:
                print(f"[CLEANUP] Slotul {out} s-a eliberat in mod confirmat.")
                config.assignments[out] = None

    if allocation_mode == "manual":
        return

    pending_nodes = [
        dev for dev in cached_devices 
        if dev['status'] in ["Ready", "New"] and dev['system_id'] not in config.assignments.values()
    ]

    for node in pending_nodes:
        with config.state_lock:
            free_outlet = next((o for o in config.OUTLETS if config.assignments[o] is None), None)
            if free_outlet:
                sid = node['system_id']
                hostname = node['hostname']
                
                config.assignments[free_outlet] = sid
                
                print(f"[MATCH DETECTAT] Nodul '{hostname}' ({sid}) a trecut in asteptare. Alocare automata pe priza: {free_outlet}")
                
                threading.Thread(
                    target=_async_node_commission_pipeline,
                    args=(sid, free_outlet, pool_name, pw),
                    daemon=True
                ).start()
            else:
                print("[SCAN] Resurse epuizate. Exista noduri noi, dar toate prizele fizice sunt ocupate.")
                break


def deploy_ready_nodes():
    with config.config_lock:
        if not config.global_cached_data:
            return
        cached_devices = list(config.global_cached_data.values())

    dyn_cfg = config.load_dynamic_config()
    default_kernel = dyn_cfg.get("maas_deployment", {}).get("default_kernel", "generic")
    
    with config.state_lock:
        active_sids = [sid for sid in config.assignments.values() if sid is not None]

    for dev in cached_devices:
        sid = dev['system_id']
        status = dev['status']
        hostname = dev['hostname']
        
        if status == "Ready" and sid in active_sids:
            deploy_params = {"hwe_kernel": default_kernel}
            if os.path.exists(config.USER_DATA_PATH):
                with open(config.USER_DATA_PATH, "rb") as f:
                    encoded_data = base64.b64encode(f.read()).decode('utf-8')
                deploy_params["user_data"] = encoded_data
            
            print(f"[STATE CHANGE -> DEPLOY] Nodul '{hostname}' ({sid}) este pregatit! Se initiaza instalarea automata cu Kernel: {default_kernel}")
            try:
                maas.perform_machine_action(sid, "deploy", deploy_params)
            except Exception as e:
                print(f"[ERROR] Eroare la trimiterea comenzii de deploy pentru {hostname}: {e}")

def maas_sync_loop():
    # Initializam etichetarea nodurilor in cluster la pornirea daemonului
    k8s_logic.auto_label_rpi_nodes()
    
    label_counter = 0
    while True:
        dyn_cfg = config.load_dynamic_config()
        sleep_time = dyn_cfg.get("polling_intervals", {}).get("maas_scan_seconds", 60)
        
        perform_scan()
        deploy_ready_nodes()
        
        label_counter += 1
        if label_counter >= 5:
            k8s_logic.auto_label_rpi_nodes()
            label_counter = 0
            
        time.sleep(sleep_time)

def k8s_autoscaler_loop():
    scale_up_in_progress = False
    while True:
        dyn_cfg = config.load_dynamic_config()
        sleep_time = dyn_cfg.get("polling_intervals", {}).get("k8s_autoscaler_seconds", 30)
        
        print("[THREAD AUTOSCALER] Verificare metrici cluster...")
        is_pod_pending = k8s_logic.has_pending_notebook_pods()

        if is_pod_pending:
            scale_up_in_progress = True
            power_on_next_available_node()
        else:
            if scale_up_in_progress:
                scale_up_in_progress = False

        if not scale_up_in_progress:
            check_and_scale_down()

        time.sleep(sleep_time)

def power_on_next_available_node():
    try:
        machines = maas.get_machines()
    except Exception:
        return False

    for outlet, sid in config.assignments.items():
        if sid:
            maas_node = next((m for m in machines if m['system_id'] == sid), None)
            if maas_node and maas_node.get("power_state") == "off":
                hostname = maas_node.get("hostname", "Unknown")
                print(f"[AUTOSCALER] Pornire automata ON-DEMAND pentru {hostname} ({sid}) pe priza {outlet}...")
                
                try:
                    maas.perform_machine_action(sid, "power_on")
                except Exception as e:
                    print(f"Eroare power-on API: {e}")
                    return False
                
                print(f"[AUTOSCALER] Asteptam stabilizarea metrics.k8s.io pentru {hostname} (Maxim 5 minute)...")
                
                # Securizarea buclei infinite: limita la 100 de incercari (100 * 3 secunde = 5 minute maxim)
                max_attempts = 100
                attempts = 0
                metrics_online = False
                
                while attempts < max_attempts:
                    try:
                        pod_metrics = k8s_logic.get_k8s_pod_metrics()
                        if pod_metrics and hostname in pod_metrics:
                            print(f"[AUTOSCALER] Metrics API este online pentru {hostname}!")
                            metrics_online = True
                            break
                    except:
                        pass
                    attempts += 1
                    time.sleep(3)
                
                if not metrics_online:
                    print(f"[AUTOSCALER] [TIMEOUT] Alarma: Nodul {hostname} nu a pornit corect sau are agentul K3s blocat!")
                    return False
                
                print(f"[AUTOSCALER] Executam UNCORDON pe {hostname}.")
                k8s_logic.uncordon_node(hostname)
                return True
    return False

def check_and_scale_down():
    with config.config_lock:
        if not config.global_cached_data:
            return
        cached_devices = list(config.global_cached_data.values())

    dyn_cfg = config.load_dynamic_config()
    grace_cycles = dyn_cfg.get("polling_intervals", {}).get("inactive_grace_cycles", 6)
    min_active_nodes = dyn_cfg.get("policies", {}).get("min_active_nodes", 1)

    # Calculam nodurile active direct din cache-ul RAM
    active_nodes_in_maas = 0
    with config.state_lock:
        assigned_sids = set(sid for sid in config.assignments.values() if sid)

    for dev in cached_devices:
        if dev.get("system_id") in assigned_sids:
            # In cache field-ul se numeste 'status', iar 'power_state' este fortat cu UPPERCASE in daemon
            if dev.get("status") == "Deployed" and str(dev.get("power_state")).upper() == "ON":
                active_nodes_in_maas += 1

    jupyter_counts = k8s_logic.count_jupyter_pods_per_node()

    # Verificam conditiile de scale down pentru fiecare nod alocat curent
    with config.state_lock:
        current_assignments = list(config.assignments.items())

    for outlet, sid in current_assignments:
        if not sid: 
            continue
            
        # Gasim nodul in datele optimizate din cache dupa ID-ul de sistem
        maas_node = next((dev for dev in cached_devices if dev.get("system_id") == sid), None)
        if not maas_node: 
            continue
        
        hostname = maas_node.get("hostname")
        status = maas_node.get("status")  
        power_state = str(maas_node.get("power_state")).upper()

        if status == "Deployed" and power_state == "ON":
            if hostname not in jupyter_counts and hostname not in k8s_logic.get_k8s_node_metrics():
                config.inactive_nodes[hostname] = 0
                continue

            active_pods = jupyter_counts.get(hostname, 0)
            if active_pods == 0:
                if active_nodes_in_maas <= min_active_nodes:
                    config.inactive_nodes[hostname] = 0
                    continue

                config.inactive_nodes[hostname] = config.inactive_nodes.get(hostname, 0) + 1
                if config.inactive_nodes[hostname] >= grace_cycles:
                    k8s_logic.cordon_node(hostname)
                    try:
                        maas.perform_machine_action(sid, "power_off")
                        print(f"[AUTOSCALER] Comanda power-off trimisa pentru {hostname} ({sid})")
                    except Exception as e:
                        print(f"[AUTOSCALER] [ERROR] Eroare power-off API pentru {hostname}: {e}")
                    config.inactive_nodes[hostname] = 0
                    active_nodes_in_maas -= 1
            else:
                config.inactive_nodes[hostname] = 0

def get_available_maas_kernels():
    options = [{"id": "generic", "label": "Generic (Implicit MAAS)"}]
    seen_keys = set()

    try:
        url = f"{maas.base_url}/boot-resources/"
        response = maas.session.get(url, timeout=10)
        response.raise_for_status()
        resources = response.json()

        if isinstance(resources, list):
            for res in resources:
                if not isinstance(res, dict):
                    continue

                name = res.get("name", "")
                architecture_raw = res.get("architecture", "")

                if "grub" in name or "pxelinux" in name or not architecture_raw:
                    continue

                if "/" in architecture_raw:
                    arch, kernel_id = architecture_raw.split("/", 1)
                else:
                    arch = architecture_raw
                    kernel_id = "generic"

                noise_keywords = ["edge", "lowlatency", "uboot", "mustang", "xgene", "generic"]
                if any(noise in str(kernel_id).lower() for noise in noise_keywords):
                    continue

                clean_arch = str(arch).split("/")[0]

                if "focal" in name:
                    os_title = "Ubuntu 20.04 LTS"
                elif "jammy" in name:
                    os_title = "Ubuntu 22.04 LTS"
                elif "noble" in name:
                    os_title = "Ubuntu 24.04 LTS"
                else:
                    continue

                unique_key = f"{kernel_id}-{clean_arch}"
                if unique_key in seen_keys:
                    continue
                seen_keys.add(unique_key)

                friendly_label = f"{os_title} ({clean_arch}) | Kernel: {kernel_id}"
                options.append({
                    "id": kernel_id,  
                    "label": friendly_label
                })

    except Exception as e:
        print(f"[WARNING] Eroare la preluarea sau parsarea resurselor din MAAS: {e}")
        return [{"id": "generic", "label": "Generic (Implicit MAAS)"}]

    return options

def update_all_assigned_power_params():
    print("[SETTINGS UPDATE] Se propaga noile credentiale de Webhook catre nodurile active din MAAS...")
    
    with config.state_lock:
        current_assignments = list(config.assignments.items())
        
    for outlet, sid in current_assignments:
        if sid:
            print(f"[INFO] Se reconfigureaza electric slotul {outlet.upper()} (System ID: {sid})...")
            new_params = get_power_params(outlet)
            
            try:
                maas.update_machine(sid, new_params)
                print(f"[SUCCESS] Webhook actualizat cu succes in MAAS pentru {sid}.")
            except Exception as err:
                print(f"[ERROR] Esecuri la actualizarea parametrilor in MAAS pentru {sid}: {err}")