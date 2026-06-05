from kubernetes import client, config as k8s_config, watch
import threading
import time
import requests
import config

try:
    k8s_config.load_kube_config()
    k8s_available = True
    print("[INFO] Conexiunea la clusterul K3s a fost initializata cu succes.")
except Exception as e:
    print(f"[WARNING] Nu s-a putut incarca configuratia K8s ({e}). Metricile K8s vor fi indisponibile.")
    k8s_available = False

# Structura in RAM pentru starea pod-urilor
k8s_pod_cache_lock = threading.Lock()
jupyter_counts_cache = {}  # hostname -> count
has_pending_cache = False

def parse_cpu_to_cores(cpu_str):
    if not cpu_str: return 1.0
    if cpu_str.endswith('n'):
        return float(cpu_str.replace('n', '')) / 1000000000
    if cpu_str.endswith('m'):
        return float(cpu_str.replace('m', '')) / 1000
    return float(cpu_str)

def parse_mem_to_mib(mem_str):
    if not mem_str: return 1.0
    if mem_str.endswith('Ki'):
        return float(mem_str.replace('Ki', '')) / 1024
    if mem_str.endswith('Mi'):
        return float(mem_str.replace('Mi', ''))
    if mem_str.endswith('Gi'):
        return float(mem_str.replace('Gi', '')) * 1024
    if mem_str.isdigit():
        return float(mem_str) / (1024 * 1024)
    return float(mem_str)

def start_k8s_pod_watcher():
    if not k8s_available:
        return False
    threading.Thread(target=_k8s_watch_loop, daemon=True).start()
    return True

def _k8s_watch_loop():
    global has_pending_cache, jupyter_counts_cache
    w = watch.Watch()
    core_api = client.CoreV1Api()
    
    print("[K8S WATCHER] Stream-ul de evenimente pentru Pod-uri a pornit...")
    tracked_pods = {} # (namespace, name) -> {"node": str, "phase": str}

    while True:
        try:
            # Configuratia se va reincarca automat la fiecare 30 de secunde cand expira stream-ul
            dyn_cfg = config.load_dynamic_config()
            policies = dyn_cfg.get("policies", {})
            ignored_ns = policies.get("ignored_namespaces", ["kube-system", "kube-public", "kube-node-lease"])
            pod_prefix = policies.get("monitored_pod_prefix", "jupyter-")

            stream = w.stream(core_api.list_pod_for_all_namespaces, timeout_seconds=30)
            
            for event in stream:
                event_type = event['type']
                pod = event['object']
                
                ns = pod.metadata.namespace
                name = pod.metadata.name
                
                if ns in ignored_ns or pod_prefix not in name:
                    continue
                
                pod_key = (ns, name)
                phase = pod.status.phase
                node_name = pod.spec.node_name

                if event_type == 'DELETED' or phase in ['Succeeded', 'Failed']:
                    tracked_pods.pop(pod_key, None)
                else:
                    tracked_pods[pod_key] = {
                        "node": node_name,
                        "phase": phase
                    }

                with k8s_pod_cache_lock:
                    new_counts = {}
                    new_pending = False
                    
                    for p_info in tracked_pods.values():
                        if p_info["phase"] == "Running" and p_info["node"]:
                            h_name = p_info["node"]
                            new_counts[h_name] = new_counts.get(h_name, 0) + 1
                        
                        if p_info["phase"] == "Pending" and not p_info["node"]:
                            new_pending = True
                    
                    jupyter_counts_cache = new_counts
                    has_pending_cache = new_pending

        except Exception as e:
            print(f"[K8S WATCHER] Stream intrerupt sau expirat ({e}). Reinitializare si aplicare configuratie in 5s...")
            time.sleep(5)

def auto_label_rpi_nodes():
    if not k8s_available:
        return False
    try:
        core_api = client.CoreV1Api()
        nodes = core_api.list_node(timeout_seconds=5)
        
        for node in nodes.items:
            node_name = node.metadata.name
            labels = node.metadata.labels or {}
            
            # Verificam daca lipseste oricare dintre cele doua etichete cerute de YAML
            if node_name.startswith("rpi") and ("node-role.kubernetes.io/worker" not in labels or "node-role" not in labels):
                print(f"[AUTO-LABEL] Nodul {node_name} se eticheteaza complet cu ambele roluri de worker...")
                
                body = {
                    "metadata": {
                        "labels": {
                            "node-role.kubernetes.io/worker": "true",
                            "node-role": "worker"
                        }
                    }
                }
                core_api.patch_node(node_name, body)
        return True
    except Exception as e:
        print(f"Eroare la executarea Auto-Label pe noduri: {e}")
        return False

def get_k8s_node_metrics():
    if not k8s_available:
        return {}
    try:
        core_api = client.CoreV1Api()
        custom_api = client.CustomObjectsApi()
        
        k8s_nodes = core_api.list_node()
        allocatable_map = {}
        for n in k8s_nodes.items:
            n_name = n.metadata.name
            allocatable_map[n_name] = {
                "cpu": n.status.allocatable.get("cpu"),
                "memory": n.status.allocatable.get("memory")
            }
        
        resource = custom_api.list_cluster_custom_object(
            group="metrics.k8s.io", version="v1beta1", plural="nodes"
        )
        
        metrics_map = {}
        for node in resource.get("items", []):
            name = node["metadata"]["name"]
            usage_cpu = node["usage"]["cpu"]
            usage_memory = node["usage"]["memory"]
            
            used_cpu = parse_cpu_to_cores(usage_cpu)
            used_mem = parse_mem_to_mib(usage_memory)
            
            # ELIMINAT VALOARE MAGICA: Extragem capacitatea reala raportata de K3s API
            alloc = allocatable_map.get(name)
            
            if not alloc or not alloc.get("cpu") or not alloc.get("memory"):
                metrics_map[name] = {
                    "cpu_percentage": None,
                    "memory_percentage": None,
                    "memory_used": int(used_mem),
                    "memory_max": None,
                }
                continue
                
            max_cpu = parse_cpu_to_cores(alloc["cpu"])
            max_mem = parse_mem_to_mib(alloc["memory"])
            
            cpu_pct = (used_cpu / max_cpu) * 100 if max_cpu > 0 else 0
            mem_pct = (used_mem / max_mem) * 100 if max_mem > 0 else 0

            metrics_map[name] = {
                "cpu_percentage": round(cpu_pct, 1),
                "memory_percentage": round(mem_pct, 1),
                "memory_used": int(used_mem),
                "memory_max": int(max_mem),
            }
        return metrics_map
    except Exception as e:
        print(f"Eroare la calcularea metricilor procentuale in K3s: {e}")
        return {}

def get_k8s_pod_metrics():
    if not k8s_available:
        return {}
    try:
        core_api = client.CoreV1Api()
        custom_api = client.CustomObjectsApi()
        
        k8s_nodes = core_api.list_node()
        node_alloc_map = {}
        for n in k8s_nodes.items:
            node_alloc_map[n.metadata.name] = {
                "cpu": parse_cpu_to_cores(n.status.allocatable.get("cpu")) * 1000,
                "memory": parse_mem_to_mib(n.status.allocatable.get("memory"))
            }
        
        pod_list = core_api.list_pod_for_all_namespaces()
        pod_node_map = {}
        for pod in pod_list.items:
            pod_node_map[(pod.metadata.namespace, pod.metadata.name)] = pod.spec.node_name
            
        pod_resource = custom_api.list_cluster_custom_object(
            group="metrics.k8s.io", version="v1beta1", plural="pods"
        )
        
        pods_by_node = {}
        for pod_item in pod_resource.get("items", []):
            ns = pod_item["metadata"]["namespace"]
            p_name = pod_item["metadata"]["name"]
            assigned_node = pod_node_map.get((ns, p_name))
            if not assigned_node:
                continue
                
            if assigned_node not in pods_by_node:
                pods_by_node[assigned_node] = []
                
            total_cpu = 0
            total_mem = 0
            for container in pod_item.get("containers", []):
                c_cpu = container["usage"]["cpu"]
                c_mem = container["usage"]["memory"]
                if c_cpu.endswith('n'): total_cpu += int(c_cpu.replace('n', '')) / 1000000
                elif c_cpu.endswith('m'): total_cpu += int(c_cpu.replace('m', ''))
                if c_mem.endswith('Ki'): total_mem += int(c_mem.replace('Ki', '')) / 1024
                elif c_mem.endswith('Mi'): total_mem += int(c_mem.replace('Mi', ''))
            
            # ELIMINAT VALOARE MAGICA: Preluam din maparea reala a nodului
            alloc = node_alloc_map.get(assigned_node)
            
            if alloc:
                pod_cpu_pct = (total_cpu / alloc["cpu"]) * 100 if alloc["cpu"] > 0 else 0
                pod_mem_pct = (total_mem / alloc["memory"]) * 100 if alloc["memory"] > 0 else 0
                cpu_str = f"({round(pod_cpu_pct, 1)}%)"
                mem_str = f"{int(total_mem)} MB ({round(pod_mem_pct, 1)}%)"
            else:
                cpu_str = f"{round(total_cpu / 1000, 2)} Cores"
                mem_str = f"{int(total_mem)} MB"
                
            pods_by_node[assigned_node].append({
                "name": p_name,
                "namespace": ns,
                "cpu": cpu_str,
                "memory": mem_str
            })
        return pods_by_node
    except Exception as e:
        print(f"Eroare la citirea Metrics API (Pod-uri): {e}")
        return {}

def count_jupyter_pods_per_node():
    with k8s_pod_cache_lock:
        return jupyter_counts_cache.copy()

def has_pending_notebook_pods():
    with k8s_pod_cache_lock:
        return has_pending_cache

def cordon_node(node_name):
    if not k8s_available: return False
    try:
        core_api = client.CoreV1Api()
        body = {"spec": {"unschedulable": True}}
        core_api.patch_node(node_name, body)
        print(f"[K8S API] Nodul {node_name} a fost marcat ca UNSCHEDULABLE (Cordon).")
        return True
    except Exception as e:
        print(f"Eroare la executarea Cordon: {e}")
        return False

def uncordon_node(node_name):
    if not k8s_available: return False
    try:
        core_api = client.CoreV1Api()
        body = {"spec": {"unschedulable": False}}
        core_api.patch_node(node_name, body)
        print(f"[K8S API] Nodul {node_name} a fost marcat ca SCHEDULABLE (Uncordon).")
        return True
    except Exception as e:
        print(f"Eroare la executarea Uncordon: {e}")
        return False

def handle_pod_action(pod_name, namespace, action):
    if not k8s_available:
        return False, "[WARNING] Conexiunea la clusterul K3s este indisponibila."
        
    dyn_cfg = config.load_dynamic_config()
    policies = dyn_cfg.get("policies", {})
    ignored_ns = policies.get("ignored_namespaces", ["kube-system", "kube-public", "kube-node-lease"])
    pod_prefix = policies.get("monitored_pod_prefix", "jupyter-")
    
    jhub = dyn_cfg.get("jupyterhub", {})
    hub_api_url = jhub.get("api_url", "http://10.43.253.231:8081/hub/api")
    hub_token = jhub.get("token", "")

    try:
        core_api = client.CoreV1Api()
        apps_api = client.AppsV1Api()
        
        try:
            pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
            owner_references = pod.metadata.owner_references
        except Exception as e:
            return False, f"Pod-ul nu a putut fi gasit sau citit: {e}"

        has_controller = owner_references is not None and len(owner_references) > 0

        if action == "delete":
            if namespace.startswith("kube-") or namespace in ignored_ns:
                print(f"[SECURITATE] Stergere blocata in namespace protejat: {namespace}!")
                return False, f"Acces interzis! Namespace-ul '{namespace}' este protejat."
                
            if has_controller:
                owner = owner_references[0]
                if owner.kind == "StatefulSet":
                    apps_api.delete_namespaced_stateful_set(name=owner.name, namespace=namespace)
                elif owner.kind == "ReplicaSet":
                    try:
                        rs = apps_api.read_namespaced_replica_set(name=owner.name, namespace=namespace)
                        if rs.metadata.owner_references:
                            dep_name = rs.metadata.owner_references[0].name
                            apps_api.delete_namespaced_deployment(name=dep_name, namespace=namespace)
                        else:
                            apps_api.delete_namespaced_replica_set(name=owner.name, namespace=namespace)
                    except:
                        apps_api.delete_namespaced_replica_set(name=owner.name, namespace=namespace)
            
            core_api.delete_namespaced_pod(name=pod_name, namespace=namespace, body=client.V1DeleteOptions(grace_period_seconds=0))
            return True, None

        elif action == "restart":
            if has_controller:
                core_api.delete_namespaced_pod(name=pod_name, namespace=namespace, body=client.V1DeleteOptions(grace_period_seconds=5))
                return True, None
            
            if pod_prefix in pod_name:
                username = pod_name.replace(pod_prefix, "")
                print(f"[AUTOSCALER] Se initiaza restartul prin JupyterHub API pentru: {username}")
                
                headers = {
                    "Authorization": f"token {hub_token}",
                    "Content-Type": "application/json"
                }
                user_server_url = f"{hub_api_url}/users/{username}/server"
                
                try:
                    print(f"[JHUB API] Se trimite oprirea serverului pentru {username}...")
                    response_delete = requests.delete(user_server_url, headers=headers, timeout=10)
                    
                    if response_delete.status_code in [202, 204, 404]:
                        if response_delete.status_code != 404:
                            print(f"[JHUB API] Se asteapta eliberarea resurselor...")
                            for _ in range(15):
                                try:
                                    core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
                                    time.sleep(1)
                                except:
                                    break
                        
                        print(f"[JHUB API] Se trimite pornirea serverului pentru {username}...")
                        response_post = requests.post(user_server_url, headers=headers, timeout=10)
                        if response_post.status_code in [200, 201, 202]:
                            print(f"[JHUB API] Serverul utilizatorului {username} a fost repornit cu succes!")
                            return True, None
                        else:
                            return False, f"Eroare JHUB pornire ({response_post.status_code})"
                    else:
                        return False, f"Eroare JHUB oprire ({response_delete.status_code})"
                        
                except Exception as jhub_err:
                    print(f"[WARNING] Esec JupyterHub API ({jhub_err}). Se aplica fallback pe K8s clasic...")

            pod.metadata.resource_version = None
            pod.metadata.uid = None
            pod.metadata.creation_timestamp = None
            core_api.delete_namespaced_pod(name=pod_name, namespace=namespace, body=client.V1DeleteOptions(grace_period_seconds=0))
            for _ in range(10):
                try:
                    core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
                    time.sleep(1)
                except:
                    break
            core_api.create_namespaced_pod(namespace=namespace, body=pod)
            return True, None

    except Exception as e:
        print(f"Eroare la executia actiunii {action} pe pod-ul {pod_name}: {e}")
        return False, str(e)
