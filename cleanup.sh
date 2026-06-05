#!/bin/bash

# Global variables (Trebuie să coincidă cu cele din scriptul de instalare)
POSTGRES_PORT="10542"
MAAS_DBNAME="maas_db"
MAAS_DBUSER="maas_admin"
LOG_FILE="/var/log/maas-uninstall.log"

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

#############
# CLEANUP   #
#############

check_system() {
    if [ "$EUID" -ne 0 ]; then
        log_error "Te rog rulează ca root (sudo)."
        exit 1
    fi
    touch "${LOG_FILE}"
}

cleanup_maas() {
    log_info "Oprire și eliminare MAAS via Snap..."
    sudo snap remove --purge maas || log_warn "MAAS snap nu a fost găsit sau a fost deja șters."

    log_info "Curățare directoare reziduale MAAS..."
    rm -rf /var/snap/maas
    rm -rf /var/lib/maas
    rm -rf /etc/maas
}

cleanup_postgres_db() {
    log_info "Eliminare bază de date și utilizator din PostgreSQL (Port $POSTGRES_PORT)..."

    # Forțăm închiderea conexiunilor active la baza de date MAAS pentru a o putea șterge
    sudo -i -u postgres psql -p "$POSTGRES_PORT" -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$MAAS_DBNAME';" > /dev/null 2>&1

    # Ștergere DB
    sudo -i -u postgres dropdb -p "$POSTGRES_PORT" --if-exists "$MAAS_DBNAME" && log_info "Baza de date $MAAS_DBNAME a fost ștearsă."
    
    # Ștergere User
    sudo -i -u postgres psql -p "$POSTGRES_PORT" -c "DROP USER IF EXISTS \"$MAAS_DBUSER\";" && log_info "Utilizatorul $MAAS_DBUSER a fost șters."

    # Curățare fișier pg_hba.conf de liniile MAAS
    log_info "Curățare configurări acces în pg_hba.conf..."
    sudo sed -i "/$MAAS_DBNAME/d" /etc/postgresql/16/main/pg_hba.conf
    
    log_info "Restart PostgreSQL pentru aplicare modificări..."
    sudo systemctl restart postgresql
}

cleanup_network_configs() {
    log_info "Curățare configurații de rețea (opțional)..."
    # Notă: Scriptul de instalare nu a modificat /etc/network/interfaces permanent, 
    # dar dacă s-au creat rute sau reguli iptables manual, aici e locul pentru curățare.
    log_warn "Verifică manual dacă interfața enx0c37964c30ce mai are IP-uri reziduale."
}
Am modificat scriptul pentru a include eliminarea completă a pachetului PostgreSQL, a directoarelor de date și a fișierelor de configurare.

Am adăugat o funcție nouă uninstall_postgres_full care se ocupă de oprirea serviciului, eliminarea binarelor și ștergerea folderului /var/lib/postgresql.
Bash

#!/bin/bash

# Global variables
POSTGRES_PORT="10542"
MAAS_DBNAME="maas_db"
MAAS_DBUSER="maas_admin"
LOG_FILE="/var/log/maas-uninstall.log"

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

#############
# CLEANUP   #
#############

check_system() {
    if [ "$EUID" -ne 0 ]; then
        log_error "Te rog rulează ca root (sudo)."
        exit 1
    fi
    touch "${LOG_FILE}"
}

cleanup_maas() {
    log_info "Oprire și eliminare MAAS via Snap..."
    sudo snap remove --purge maas || log_warn "MAAS snap nu a fost găsit sau a fost deja șters."

    log_info "Curățare directoare reziduale MAAS..."
    rm -rf /var/snap/maas
    rm -rf /var/lib/maas
    rm -rf /etc/maas
}

uninstall_postgres_full() {
    log_info "Începere dezinstalare completă PostgreSQL..."

    # Oprire serviciu
    sudo systemctl stop postgresql || log_warn "Serviciul PostgreSQL nu rulează."

    # Eliminare pachete (detectează versiunile instalate)
    log_info "Eliminare pachete binare PostgreSQL..."
    sudo apt-get purge -y postgresql* postgresql-client* postgresql-common postgresql-contrib
    
    # Autoremove pentru dependențe nefolosite
    sudo apt-get autoremove -y
    sudo apt-get autoclean

    # Ștergere directoare de date și configurare
    log_info "Ștergere directoare de date și configurări PostgreSQL (/var/lib/postgresql, /etc/postgresql)..."
    rm -rf /etc/postgresql/
    rm -rf /etc/postgresql-common/
    rm -rf /var/lib/postgresql/
    rm -rf /var/log/postgresql/

    # Ștergere utilizator de sistem postgres
    if id "postgres" &>/dev/null; then
        userdel -r postgres 2>/dev/null || log_warn "Nu s-a putut șterge utilizatorul 'postgres' (posibil procese active)."
    fi

    log_info "PostgreSQL a fost eliminat complet din sistem."
}

########
# MAIN #
########

echo -e "${YELLOW}Atenție! Acest script va șterge complet datele MAAS.${NC}"
read -p "Ești sigur că vrei să continui? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    log_info "Abortat de utilizator."
    exit 1
fi

check_system
cleanup_maas
cleanup_postgres_db

cleanup_network_configs

log_info "--------------------------------------------------------"
log_info "Dezinstalare finalizată cu succes!"
log_info "PostgreSQL a rămas instalat pe portul $POSTGRES_PORT."
log_info "--------------------------------------------------------"
