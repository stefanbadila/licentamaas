#!/bin/bash

# Global variables
HOSTNAME="localhost"
POSTGRES_PORT="10542"
LOG_FILE="/var/log/maas-install.log"
MAAS_DBNAME="maas_db"
MAAS_DBUSER="maas_admin" 
MAAS_PASSWORD="Licenta2026" 
POSTGRES_PASSWORD="Licenta2026" 
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
        log_error "Te rog ruleaza ca root (sudo)."
        exit 1
    fi
    mkdir -p "$(dirname "${LOG_FILE}")"
    touch "${LOG_FILE}"
}

maas_install() {
    log_info "Instalare Ubuntu MAAS via Snap..."
    sudo snap install maas || { log_error "Esec instalare MAAS"; return 1; }

    export PATH=$PATH:/snap/bin

    log_info "Adaugare repository PostgreSQL..."
    wget -qO- https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --yes --dearmor -o /usr/share/keyrings/postgres-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/postgres-archive-keyring.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" | sudo tee /etc/apt/sources.list.d/pgdg.list > /dev/null
    
    log_info "Instalare PostgreSQL 16..."
    sudo apt update > /dev/null
    sudo apt install -y postgresql-16 jq ipcalc > /dev/null || { log_error "Esec instalare PostgreSQL"; return 1; }

    log_info "Configurare port PostgreSQL la $POSTGRES_PORT..."
    sudo sed -i "s/^port = .*/port = $POSTGRES_PORT/" /etc/postgresql/16/main/postgresql.conf
    sudo systemctl restart postgresql

    log_info "Creare baza de date si user pentru MAAS..."
    sudo -i -u postgres psql -c "CREATE USER \"$MAAS_DBUSER\" WITH ENCRYPTED PASSWORD '$POSTGRES_PASSWORD'"
    sudo -i -u postgres createdb -O "$MAAS_DBUSER" "$MAAS_DBNAME"
    
    echo "host $MAAS_DBNAME $MAAS_DBUSER 0/0 md5" | sudo tee -a /etc/postgresql/16/main/pg_hba.conf > /dev/null
    sudo systemctl restart postgresql
    
    log_info "Initializare MAAS Region + Rack Controller..."
    maas init region+rack --maas-url http://$HOSTNAME:5240/MAAS --database-uri "postgres://$MAAS_DBUSER:$POSTGRES_PASSWORD@$HOSTNAME:$POSTGRES_PORT/$MAAS_DBNAME" || return 1

    log_info "Asteptare 15s pentru initializare servicii..."
    sleep 15

    log_info "Creare cont admin MAAS..."
    sudo maas createadmin --username="admin" --email=admin@example.com --password="$MAAS_PASSWORD" || return 1

    log_info "Obtinere API Key..."
    API_KEY=$(sleep 5 && sudo maas apikey --username="admin")
    log_info "MAAS API Key: $API_KEY"

    # Login pentru configurari automate
    maas login admin http://$HOSTNAME:5240/MAAS/api/2.0/ "$API_KEY" > /dev/null

    log_info "Configurare DNS upstream (8.8.8.8)..."
    maas admin maas set-config name=upstream_dns value="8.8.8.8"

    log_info "Importare resurse de boot (Ubuntu Jammy)..."
    maas admin boot-source-selections create 1 os="ubuntu" release="jammy" arches="amd64" subarches="*" labels="*"
    maas admin boot-resources import
}


configure_ztp_networking() {
  log_info "Corectie configurare ZTP pe $INTERFACE..."

  # 1. Identificam Rack Controller-ul (esential pentru DHCP)
  PRIMARY_RACK=$(maas admin rack-controllers read | jq -r '.[0].system_id // empty')
  if [[ -z "$PRIMARY_RACK" ]]; then
    log_error "Rack controller negasit. MAAS este pornit?"; return 1
  fi

  # 2. Cream Fabric-ul (daca nu exista)
  FABRIC_NAME="ztp-fabric"
  FABRIC_ID=$(maas admin fabrics read | jq -r --arg name "$FABRIC_NAME" '.[] | select(.name==$name) | .id')
  if [[ -z "$FABRIC_ID" ]]; then
    log_info "Creare Fabric nou..."
    FABRIC_ID=$(maas admin fabrics create name="$FABRIC_NAME" | jq -r '.id')
  fi

  # 3. Identificam sau cream VLAN-ul Untagged (vid=0) in acest Fabric
  VLAN_ID=$(maas admin vlans read "$FABRIC_ID" | jq -r '.[] | select(.vid==0) | .id')
  if [[ -z "$VLAN_ID" ]]; then
    VLAN_ID=$(maas admin vlans create "$FABRIC_ID" name="ztp-vlan" vid=0 | jq -r '.id')
  fi

  # 4. Cream Subnet-ul legat direct de VLAN-ul proaspat creat
  PHYS_CIDR="192.168.0.0/24"
  EXISTING_SUBNET_ID=$(maas admin subnets read | jq -r --arg cidr "$PHYS_CIDR" '.[] | select(.cidr==$cidr) | .id')

  if [[ -z "$EXISTING_SUBNET_ID" ]]; then
    log_info "Creare Subnet $PHYS_CIDR..."
    maas admin subnets create cidr="$PHYS_CIDR" gateway_ip="$MAAS_IP" vlan="$VLAN_ID" dns_servers="8.8.8.8" > /dev/null
  else
    log_info "Subnet-ul exista deja. Actualizam legatura cu VLAN-ul..."
    maas admin subnet update "$EXISTING_SUBNET_ID" vlan="$VLAN_ID" gateway_ip="$MAAS_IP" > /dev/null
  fi

  # 5. Configurare IP Range si activare DHCP
  log_info "Activare DHCP pe VLAN ID: $VLAN_ID"
  
  # Stergem range-uri vechi daca exista pentru a evita suprapunerea
  maas admin ipranges create type=dynamic start_ip="192.168.0.101" end_ip="192.168.0.200" || true
  
  # Activam DHCP-ul pe acest VLAN folosind Rack-ul gasit
  maas admin vlan update "$FABRIC_ID" "$VLAN_ID" dhcp_on=True primary_rack="$PRIMARY_RACK" > /dev/null

  # 6. Setari ZTP (Enlistment automat)
  maas admin maas set-config name=enlist_commissioning value=true
  # Foarte important pentru RPi: permite arhitecturile ARM64 daca e cazul
  maas admin boot-source-selections create 1 os="ubuntu" release="jammy" arches="arm64" subarches="*" labels="*" || true

  log_info "ZTP gata! Conecteaza Raspberry Pi la portul USB-Ethernet."
}

########
# MAIN #
########

check_system
maas_install
configure_ztp_networking

echo "--------------------------------------------------------"
echo -e "${GREEN}Instalare MAAS completa!${NC}"
echo "URL: http://$HOSTNAME:5240/MAAS"
echo "Utilizator: admin"
echo "Parola: $MAAS_PASSWORD"
echo "--------------------------------------------------------"