#!/bin/bash

# Global variables
HOSTNAME="localhost"
POSTGRES_PORT="10542"
LOG_FILE="/var/log/maas-install.log"
MAAS_DBNAME="maas_db"
MAAS_DBUSER="maas_admin" # Poți schimba sau lăsa din machine-id
MAAS_PASSWORD="Licenta2026" # Schimbă această parolă
POSTGRES_PASSWORD="Licenta2026" # Schimbă această parolă
INTERFACE="enx0c37964c30ce"
MAAS_URL="http://192.168.0.100:5240/MAAS"
MAAS_IP="192.168.0.100"
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

########
# MAAS #
########

check_system() {
    if [ "$EUID" -ne 0 ]; then
        log_error "Te rog rulează ca root (sudo)."
        exit 1
    fi
    mkdir -p "$(dirname "${LOG_FILE}")"
    touch "${LOG_FILE}"
}

maas_install() {
    log_info "Instalare Ubuntu MAAS via Snap..."
    sudo snap install maas || { log_error "Eșec instalare MAAS"; return 1; }

    export PATH=$PATH:/snap/bin

    log_info "Adăugare repository PostgreSQL..."
    wget -qO- https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --yes --dearmor -o /usr/share/keyrings/postgres-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/postgres-archive-keyring.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" | sudo tee /etc/apt/sources.list.d/pgdg.list > /dev/null
    
    log_info "Instalare PostgreSQL 16..."
    sudo apt update > /dev/null
    sudo apt install -y postgresql-16 jq ipcalc > /dev/null || { log_error "Eșec instalare PostgreSQL"; return 1; }

    log_info "Configurare port PostgreSQL la $POSTGRES_PORT..."
    sudo sed -i "s/^port = .*/port = $POSTGRES_PORT/" /etc/postgresql/16/main/postgresql.conf
    sudo systemctl restart postgresql

    log_info "Creare bază de date și user pentru MAAS..."
    sudo -i -u postgres psql -c "CREATE USER \"$MAAS_DBUSER\" WITH ENCRYPTED PASSWORD '$POSTGRES_PASSWORD'"
    sudo -i -u postgres createdb -O "$MAAS_DBUSER" "$MAAS_DBNAME"
    
    echo "host $MAAS_DBNAME $MAAS_DBUSER 0/0 md5" | sudo tee -a /etc/postgresql/16/main/pg_hba.conf > /dev/null
    sudo systemctl restart postgresql
    
    log_info "Inițializare MAAS Region + Rack Controller..."
    maas init region+rack --maas-url http://$HOSTNAME:5240/MAAS --database-uri "postgres://$MAAS_DBUSER:$POSTGRES_PASSWORD@$HOSTNAME:$POSTGRES_PORT/$MAAS_DBNAME" || return 1

    log_info "Așteptare 15s pentru inițializare servicii..."
    sleep 15

    log_info "Creare cont admin MAAS..."
    sudo maas createadmin --username="admin" --email=admin@example.com --password="$MAAS_PASSWORD" || return 1

    log_info "Obținere API Key..."
    API_KEY=$(sleep 5 && sudo maas apikey --username="admin")
    log_info "MAAS API Key: $API_KEY"

    # Login pentru configurări automate
    maas login admin http://$HOSTNAME:5240/MAAS/api/2.0/ "$API_KEY" > /dev/null

    log_info "Configurare DNS upstream (8.8.8.8)..."
    maas admin maas set-config name=upstream_dns value="8.8.8.8"

    log_info "Importare resurse de boot (Ubuntu Jammy)..."
    maas admin boot-source-selections create 1 os="ubuntu" release="jammy" arches="amd64" subarches="*" labels="*"
    maas admin boot-resources import
}

configure_networking() {


    log_info "Configurare rețea MAAS pentru interfața USB..."
    
    # 1. Identificăm interfața USB exactă
    INTERFACE="enx0c37964c30ce"
    
    # 2. Obținem RACK_ID
    log_info "Interogăm MAAS pentru Rack Controllers..."
    
    # Încercăm să-l detectăm
    RACK_ID=$(maas admin rack-controllers read | jq -r '.[0].system_id // empty')
    
    # Dacă e tot gol, folosim ID-ul pe care l-am găsit anterior în log-uri
    if [[ -z "$RACK_ID" || "$RACK_ID" == "null" ]]; then
        log_warn "Detecția automată a eșuat. Folosesc ID-ul cunoscut: mgf63n"
        RACK_ID="mgf63n"
    fi

    # 3. Activăm PXE pe interfața USB (ID: 283 conform log-ului tău)
    INT_ID=$(maas admin interfaces read "$RACK_ID" | jq -r --arg name "$INTERFACE" '.[] | select(.name==$name) | .id')

    if [ -n "$INT_ID" ]; then
        log_info "Activăm PXE pe interfața $INTERFACE (ID: $INT_ID)..."
        maas admin interface update "$RACK_ID" "$INT_ID" bootable=true > /dev/null
    else
        log_error "Interfața $INTERFACE nu a fost găsită în MAAS pentru rack-ul $RACK_ID."
        return 1
    fi

    # 4. Configurare Subnet și DHCP
    # Luăm datele direct din sistemul tău (Fabric 103, VLAN 5104)
    FABRIC_ID="103"
    VLAN_ID="5104"
    
    # Detectăm IP-ul real de pe interfață pentru a seta range-ul DHCP
    PHYS_ADDR=$(ip -o -f inet addr show "$INTERFACE" | awk '{print $4}' | cut -d'/' -f1)
    
    if [ -z "$PHYS_ADDR" ]; then
        log_warn "Interfața $INTERFACE nu are IP pe sistem. DHCP nu poate fi activat corect fără un IP static pe host."
        return 1
    fi

    BASE_IP=$(echo $PHYS_ADDR | cut -d'.' -f1-3)
    DHCP_START="$BASE_IP.100"
    DHCP_END="$BASE_IP.200"

    log_info "Activăm DHCP pe Fabric $FABRIC_ID, VLAN $VLAN_ID (Range: $DHCP_START - $DHCP_END)..."
    
    # Creăm range-ul de IP-uri
    maas admin ipranges create type=dynamic start_ip="$DHCP_START" end_ip="$DHCP_END" > /dev/null
    
    # Pornim DHCP-ul pe VLAN
    maas admin vlan update "$FABRIC_ID" "$VLAN_ID" dhcp_on=True primary_rack="$RACK_ID" > /dev/null
    
    log_info "Configurare finalizată cu succes."
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

########
# MAIN #
########

check_system
maas_install
configure_ztp_networking

echo "--------------------------------------------------------"
echo -e "${GREEN}Instalare MAAS completă!${NC}"
echo "URL: http://$HOSTNAME:5240/MAAS"
echo "Utilizator: admin"
echo "Parolă: $MAAS_PASSWORD"
echo "--------------------------------------------------------"
