#!/bin/bash

# Global variables
HOSTNAME="localhost"
POSTGRES_PORT="10542"
LOG_FILE="/var/log/maas-install.log"
MAAS_DBNAME="maas_db"
MAAS_DBUSER="maas_admin" # Poți schimba sau lăsa din machine-id
MAAS_PASSWORD="Licenta2026" # Schimbă această parolă
POSTGRES_PASSWORD="Licenta2026" # Schimbă această parolă
MAAS_URL="http://localhost:5240/MAAS"
# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

###########
# LOGGING #
###########

log_info() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') [INFO] ${GREEN}$1${NC}" | tee -a "${LOG_FILE}"; }
log_warn() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') [WARN] ${YELLOW}$1${NC}" | tee -a "${LOG_FILE}"; }
log_error() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] ${RED}$1${NC}" | tee -a "${LOG_FILE}"; }

# FUNCȚIE NOUĂ: MAAS LOGIN
maas_login() {
    log_info "Verificăm autentificarea MAAS CLI..."
    
    # Încercăm să vedem dacă suntem deja logați
    if maas admin rack-controllers read >/dev/null 2>&1; then
        log_info "Deja autentificat ca admin."
        return 0
    fi

    log_warn "Sesiune MAAS inexistentă. Încercăm login automat..."

    # Extragere automată a cheii API pentru user-ul 'admin' 
    # (Funcționează doar dacă rulezi cu sudo, deoarece citește baza de date MAAS)
    API_KEY=$(sudo maas apikey --username admin 2>/dev/null)

    if [ -z "$API_KEY" ]; then
        log_error "Nu am putut recupera cheia API. Te-ai asigurat că user-ul 'admin' există?"
        log_info "Rulează manual: sudo maas init admin"
        exit 1
    fi

    # Executăm login-ul efectiv
    maas login admin "$MAAS_URL" "$API_KEY" > /dev/null
    
    if [ $? -eq 0 ]; then
        log_info "Login MAAS reușit cu succes!"
    else
        log_error "Eroare la autentificarea în MAAS API."
        exit 1
    fi
}

check_system() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "\e[31m[ERROR] Te rog rulează ca root (sudo).\e[0m"
        exit 1
    fi
    mkdir -p "$(dirname "${LOG_FILE}")"
    touch "${LOG_FILE}"
    
    # Apelăm login-ul înainte de orice altceva
    maas_login
}
configure_networking() { 
    log_info "Inițiem configurarea dinamică MAAS..." 

    # 1. Detectare automată RACK_ID (Controller ID)
    # Căutăm primul controller disponibil în sistem
    RACK_ID=$(maas admin rack-controllers read | jq -r '.[0].system_id // empty')

    if [[ -z "$RACK_ID" || "$RACK_ID" == "null" ]]; then
        log_error "Nu s-a putut detecta niciun Rack Controller ID. Verifică autentificarea MAAS."
        return 1
    fi
    log_info "Controller detectat: $RACK_ID"

    # 2. Identificare interfețe
    # Luăm toate interfețele de pe acest rack controller
    INTERFACES_JSON=$(maas admin interfaces read "$RACK_ID")

    # Detectăm numele exact al interfeței care începe cu 'enx'
    ENX_INT_NAME=$(echo "$INTERFACES_JSON" | jq -r '.[] | select(.name | startswith("enx")) | .name' | head -n 1)
    ENX_INT_ID=$(echo "$INTERFACES_JSON" | jq -r --arg name "$ENX_INT_NAME" '.[] | select(.name==$name) | .id')

    # Detectăm interfața 'wsp' pentru a o dezactiva de la boot
    WSP_INT_NAME=$(echo "$INTERFACES_JSON" | jq -r '.[] | select(.name | startswith("wsp")) | .name' | head -n 1)
    WSP_INT_ID=$(echo "$INTERFACES_JSON" | jq -r --arg name "$WSP_INT_NAME" '.[] | select(.name==$name) | .id')

    # 3. Aplicare configurări PXE
    
    # Activăm PXE pe ENX
    if [ -n "$ENX_INT_ID" ] && [ "$ENX_INT_ID" != "null" ]; then
        log_info "Activăm PXE pe interfața principală: $ENX_INT_NAME (ID: $ENX_INT_ID)"
        maas admin interface update "$RACK_ID" "$ENX_INT_ID" bootable=true > /dev/null
    else
        log_warn "Interfața 'enx' nu a fost găsită. Verifică conexiunea fizică a adaptorului USB."
    fi

    # Dezactivăm PXE pe WSP (pentru a evita conflictele)
    if [ -n "$WSP_INT_ID" ] && [ "$WSP_INT_ID" != "null" ]; then
        log_info "Dezactivăm PXE pe interfața secundară: $WSP_INT_NAME (ID: $WSP_INT_ID)"
        maas admin interface update "$RACK_ID" "$WSP_INT_ID" bootable=false > /dev/null
    fi

    # 4. Configurare Subnet și DHCP (bazat pe interfața ENX)
    if [ -z "$ENX_INT_NAME" ]; then return 1; fi

    # Obținem datele despre Fabric și VLAN pentru această interfață
    FABRIC_ID=$(echo "$INTERFACES_JSON" | jq -r --arg id "$ENX_INT_ID" '.[] | select(.id==($id|tonumber)) | .vlan.fabric_id')
    VLAN_TAG=$(echo "$INTERFACES_JSON" | jq -r --arg id "$ENX_INT_ID" '.[] | select(.id==($id|tonumber)) | .vlan.vid')
    
    # Detectăm IP-ul curent de pe interfața fizică a sistemului
    PHYS_ADDR=$(ip -o -f inet addr show "$ENX_INT_NAME" | awk '{print $4}' | cut -d'/' -f1 | head -n 1)

    if [ -z "$PHYS_ADDR" ]; then
        log_error "Interfața $ENX_INT_NAME nu are IP alocat pe host. Nu pot configura DHCP."
        return 1
    fi

    BASE_IP=$(echo $PHYS_ADDR | cut -d'.' -f1-3)
    DHCP_START="$BASE_IP.100"
    DHCP_END="$BASE_IP.200"

    log_info "Activăm DHCP pe Fabric $FABRIC_ID, VLAN $VLAN_TAG (Range: $DHCP_START - $DHCP_END)"
    
    # Creăm range-ul (ignorați eroarea dacă există deja)
    maas admin ipranges create type=dynamic start_ip="$DHCP_START" end_ip="$DHCP_END" 2>/dev/null || log_warn "Range-ul IP există deja sau a apărut o eroare la creare."
    
    # Activăm DHCP pe VLAN
    maas admin vlan update "$FABRIC_ID" "$VLAN_TAG" dhcp_on=True primary_rack="$RACK_ID" > /dev/null
    
    log_info "Configurarea rețelei pentru $ENX_INT_NAME a fost finalizată."
}
configure_ztp_networking() {
  log_info "Corecție configurare ZTP pe $INTERFACE..."

  # 1. Identificăm Rack Controller-ul (esențial pentru DHCP)
  PRIMARY_RACK=$(maas admin rack-controllers read | jq -r '.[0].system_id // empty')
  if [[ -z "$PRIMARY_RACK" ]]; then
    log_error "Rack controller negăsit. MAAS este pornit?"; return 1
  fi

  # 2. Creăm Fabric-ul (dacă nu există)
  FABRIC_NAME="ztp-fabric"
  FABRIC_ID=$(maas admin fabrics read | jq -r --arg name "$FABRIC_NAME" '.[] | select(.name==$name) | .id')
  if [[ -z "$FABRIC_ID" ]]; then
    log_info "Creare Fabric nou..."
    FABRIC_ID=$(maas admin fabrics create name="$FABRIC_NAME" | jq -r '.id')
  fi

  # 3. Identificăm sau creăm VLAN-ul Untagged (vid=0) în acest Fabric
  VLAN_ID=$(maas admin vlans read "$FABRIC_ID" | jq -r '.[] | select(.vid==0) | .id')
  if [[ -z "$VLAN_ID" ]]; then
    VLAN_ID=$(maas admin vlans create "$FABRIC_ID" name="ztp-vlan" vid=0 | jq -r '.id')
  fi

  # 4. Creăm Subnet-ul legat direct de VLAN-ul proaspăt creat
  PHYS_CIDR="192.168.0.0/24"
  EXISTING_SUBNET_ID=$(maas admin subnets read | jq -r --arg cidr "$PHYS_CIDR" '.[] | select(.cidr==$cidr) | .id')

  if [[ -z "$EXISTING_SUBNET_ID" ]]; then
    log_info "Creare Subnet $PHYS_CIDR..."
    # Aici a fost eroarea: vlan-ul trebuie trimis ca ID valid
    maas admin subnets create cidr="$PHYS_CIDR" gateway_ip="$MAAS_IP" vlan="$VLAN_ID" dns_servers="8.8.8.8" > /dev/null
  else
    log_info "Subnet-ul există deja. Actualizăm legătura cu VLAN-ul..."
    maas admin subnet update "$EXISTING_SUBNET_ID" vlan="$VLAN_ID" gateway_ip="$MAAS_IP" > /dev/null
  fi

  # 5. Configurare IP Range și activare DHCP
  log_info "Activare DHCP pe VLAN ID: $VLAN_ID"
  
  # Ștergem range-uri vechi dacă există pentru a evita suprapunerea
  # maas admin ipranges create...
  maas admin ipranges create type=dynamic start_ip="192.168.0.101" end_ip="192.168.0.200" || true
  
  # Activăm DHCP-ul pe acest VLAN folosind Rack-ul găsit
  maas admin vlan update "$FABRIC_ID" "$VLAN_ID" dhcp_on=True primary_rack="$PRIMARY_RACK" > /dev/null

  # 6. Setări ZTP (Enlistment automat)
  maas admin maas set-config name=enlist_commissioning value=true
  # Foarte important pentru RPi: permite arhitecturile ARM64 dacă e cazul
  maas admin boot-source-selections create 1 os="ubuntu" release="jammy" arches="arm64" subarches="*" labels="*" || true

  log_info "ZTP gata! Conectează Raspberry Pi la portul USB-Ethernet."
}
check_system
configure_ztp_networking
