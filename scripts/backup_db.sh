#!/usr/bin/env bash
# backup_db.sh — Daily online backup of the Boonsawat–Sendai ERP SQLite DB.
#
# Uses `sqlite3 ".backup"` (online backup API) — safe while the Flask app is
# running. Never `cp` a live SQLite DB.
#
# Layout:
#   data/backups/inventory-YYYY-MM-DD.db          (keep last 30)
#   data/backups/monthly/inventory-YYYY-MM.db     (1st of month, keep last 12)
#   data/backups/yearly/inventory-YYYY.db         (Jan 1, keep last 3)
#
# Logs to data/logs/backup.log (timestamp, file size, products+transactions row counts).
#
# Exit codes:
#   0 = success
#   non-zero = failure (sqlite3 backup failed, source missing, etc.)
#
# Run via launchd (see com.boonsawat.erp.backup.plist) or manually:
#   /Users/putty/Sendai-Boonsawat/sendy_erp/scripts/backup_db.sh
#
# macOS TCC note: ~/Documents is sandboxed. The terminal/launchd job needs
# Full Disk Access to read/write files there. If the daemon silently fails,
# grant FDA to /bin/bash (or the wrapping launchd service) in
# System Settings → Privacy & Security → Full Disk Access.

set -euo pipefail

ERP_ROOT="/Users/putty/Sendai-Boonsawat/sendy_erp"
SRC_DB="${ERP_ROOT}/inventory_app/instance/inventory.db"
BACKUP_DIR="${ERP_ROOT}/data/backups"
MONTHLY_DIR="${BACKUP_DIR}/monthly"
YEARLY_DIR="${BACKUP_DIR}/yearly"
LOG_DIR="${ERP_ROOT}/data/logs"
LOG_FILE="${LOG_DIR}/backup.log"

DATE_STAMP="$(date +%Y-%m-%d)"
MONTH_STAMP="$(date +%Y-%m)"
YEAR_STAMP="$(date +%Y)"
DAY_OF_MONTH="$(date +%d)"
MONTH_OF_YEAR="$(date +%m)"

DEST_DAILY="${BACKUP_DIR}/inventory-${DATE_STAMP}.db"
DEST_MONTHLY="${MONTHLY_DIR}/inventory-${MONTH_STAMP}.db"
DEST_YEARLY="${YEARLY_DIR}/inventory-${YEAR_STAMP}.db"

mkdir -p "${BACKUP_DIR}" "${MONTHLY_DIR}" "${YEARLY_DIR}" "${LOG_DIR}"

log() {
    local msg="$1"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${msg}" >> "${LOG_FILE}"
}

fail() {
    log "ERROR: $1"
    exit 1
}

if [ ! -f "${SRC_DB}" ]; then
    fail "source DB not found: ${SRC_DB}"
fi

# 1. Daily online backup
log "starting backup -> ${DEST_DAILY}"
if ! sqlite3 "${SRC_DB}" ".backup '${DEST_DAILY}'"; then
    fail "sqlite3 .backup failed for ${DEST_DAILY}"
fi

if [ ! -f "${DEST_DAILY}" ]; then
    fail "backup file missing after .backup: ${DEST_DAILY}"
fi

# Verify integrity of the backup
INTEGRITY="$(sqlite3 "${DEST_DAILY}" 'PRAGMA integrity_check;' 2>&1 || echo 'FAILED')"
if [ "${INTEGRITY}" != "ok" ]; then
    fail "integrity_check on backup failed: ${INTEGRITY}"
fi

# Stats for verification
SIZE_BYTES="$(stat -f '%z' "${DEST_DAILY}")"
PRODUCTS_COUNT="$(sqlite3 "${DEST_DAILY}" 'SELECT COUNT(*) FROM products;' 2>/dev/null || echo 'n/a')"
TXN_COUNT="$(sqlite3 "${DEST_DAILY}" 'SELECT COUNT(*) FROM transactions;' 2>/dev/null || echo 'n/a')"
log "ok daily=${DEST_DAILY} size=${SIZE_BYTES}B products=${PRODUCTS_COUNT} transactions=${TXN_COUNT}"

# 2. Monthly snapshot on the 1st
if [ "${DAY_OF_MONTH}" = "01" ]; then
    cp "${DEST_DAILY}" "${DEST_MONTHLY}"
    log "monthly snapshot -> ${DEST_MONTHLY}"
fi

# 3. Yearly snapshot on Jan 1
if [ "${DAY_OF_MONTH}" = "01" ] && [ "${MONTH_OF_YEAR}" = "01" ]; then
    cp "${DEST_DAILY}" "${DEST_YEARLY}"
    log "yearly snapshot -> ${DEST_YEARLY}"
fi

# 4. Rotation — keep last N
rotate() {
    local dir="$1"
    local keep="$2"
    local label="$3"
    local files
    files="$(ls -1t "${dir}"/inventory-*.db 2>/dev/null || true)"
    [ -z "${files}" ] && return 0
    local count
    count="$(printf '%s\n' "${files}" | wc -l | tr -d ' ')"
    if [ "${count}" -gt "${keep}" ]; then
        printf '%s\n' "${files}" | tail -n +"$((keep + 1))" | while read -r f; do
            rm -f "${f}"
            log "rotated ${label}: removed $(basename "${f}")"
        done
    fi
}

rotate "${BACKUP_DIR}"  30 daily
rotate "${MONTHLY_DIR}" 12 monthly
rotate "${YEARLY_DIR}"   3 yearly

log "done"
exit 0
