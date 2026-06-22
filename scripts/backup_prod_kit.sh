#!/usr/bin/env bash
#
# backup_prod_kit.sh — one-command, off-volume backup of Sendy PROD (Railway).
#
# Produces a self-contained "backup kit" under ~/sendy-prod-backups/<timestamp>/ :
#   - inventory-prod-<ts>.db   consistent online-backup snapshot (sqlite .backup,
#                              captures WAL pages a raw "Download DB" would miss)
#   - railway-env-vars.txt     Railway env vars incl. secrets (chmod 600)
#   - RESTORE.md               what's here + disaster-recovery steps
#
# READ-ONLY on prod (snapshot + read env vars). Does NOT touch your local DB or
# restart Sendy (unlike pull_prod_to_local.sh, which REPLACES local).
#
# Usage:   scripts/backup_prod_kit.sh
# Env:     KEEP=<n>   how many timestamped kits to retain (default 14)
#
set -euo pipefail

VAULT_ROOT="${VAULT_ROOT:-$HOME/sendy-prod-backups}"
PY="${ERP_PY:-$HOME/.virtualenvs/erp/bin/python}"
KEEP="${KEEP:-14}"
TS="$(date +%Y%m%d_%H%M%S)"
VAULT="$VAULT_ROOT/$TS"
DB_OUT="$VAULT/inventory-prod-$TS.db"
ENV_OUT="$VAULT/railway-env-vars.txt"

die() { echo "ERROR: $*" >&2; exit 1; }

# ── 0. preflight ─────────────────────────────────────────────────────────────
command -v railway >/dev/null 2>&1 || die "railway CLI not found (brew install railway)"
railway whoami >/dev/null 2>&1     || die "not logged in. Run:  railway login"
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not found"
[ -x "$PY" ] || die "erp venv python not found at $PY (set ERP_PY=...)"

mkdir -p "$VAULT"
echo "==> Backup kit -> $VAULT"

# ── 1. consistent prod DB snapshot (sqlite .backup, WAL-safe) ────────────────
echo "==> Pulling consistent prod DB snapshot via railway ssh ..."
railway ssh "python3 -c \"import sqlite3; s=sqlite3.connect('/data/inventory.db'); d=sqlite3.connect('/tmp/snap.db'); s.backup(d); d.close(); s.close()\" && gzip -c /tmp/snap.db | base64 && rm -f /tmp/snap.db" 2>/dev/null \
  | "$PY" -c "import sys,base64,gzip; open('$DB_OUT','wb').write(gzip.decompress(base64.b64decode(sys.stdin.read())))" \
  || { rm -rf "$VAULT"; die "snapshot fetch/decode failed (railway ssh). Vault removed."; }

[ -s "$DB_OUT" ] || { rm -rf "$VAULT"; die "snapshot is empty. Vault removed."; }

# ── 2. verify integrity (refuse to keep a corrupt backup) ────────────────────
ICHK="$(sqlite3 "$DB_OUT" 'PRAGMA integrity_check;' 2>&1 | head -1)"
[ "$ICHK" = "ok" ] || { rm -rf "$VAULT"; die "integrity_check failed ($ICHK). Vault removed."; }

PRODUCTS="$(sqlite3 "$DB_OUT" 'SELECT COUNT(*) FROM products;')"
TXNS="$(sqlite3 "$DB_OUT" 'SELECT COUNT(*) FROM transactions;')"
SALES="$(sqlite3 "$DB_OUT" 'SELECT COUNT(*) FROM sales_transactions;')"
CUSTOMERS="$(sqlite3 "$DB_OUT" 'SELECT COUNT(*) FROM customers;')"
LAST_MIG="$(sqlite3 "$DB_OUT" 'SELECT MAX(filename) FROM applied_migrations;')"
LAST_TXN="$(sqlite3 "$DB_OUT" 'SELECT MAX(created_at) FROM transactions;')"
DB_SIZE="$(du -h "$DB_OUT" | cut -f1)"
echo "    OK: integrity=ok · products=$PRODUCTS txns=$TXNS sales=$SALES customers=$CUSTOMERS · mig=$LAST_MIG · last_txn=$LAST_TXN · $DB_SIZE"

# ── 3. Railway env vars (secrets — locked down) ──────────────────────────────
echo "==> Saving Railway env vars (chmod 600) ..."
railway variables --kv > "$ENV_OUT" 2>/dev/null || railway variables > "$ENV_OUT" 2>/dev/null \
  || echo "(could not read env vars — save SECRET_KEY/ADMIN_PASSWORD/DATA_DIR manually)" > "$ENV_OUT"
chmod 600 "$ENV_OUT"

# ── 4. RESTORE.md ────────────────────────────────────────────────────────────
cat > "$VAULT/RESTORE.md" <<EOF
# Sendy PROD backup kit — $TS

Consistent point-in-time backup of Sendy production (Railway \`gentle-inspiration\` / web / production).

| File | What it is |
|------|-----------|
| \`$(basename "$DB_OUT")\` | Whole prod SQLite DB via \`sqlite3.backup()\` (WAL-consistent, integrity_check=ok). products=$PRODUCTS · txns=$TXNS · sales=$SALES · customers=$CUSTOMERS · mig=$LAST_MIG · last_txn=$LAST_TXN. |
| \`railway-env-vars.txt\` | Railway env vars incl. SECRETS (SECRET_KEY, ADMIN_PASSWORD). chmod 600. Not inside the DB. |

NOT here (by design): code (GitHub \`Zyringe/sendy-erp\`), product photos (\`Design/photos/\` is local-only, never deployed), uploaded import files (ephemeral on prod).

## Disaster recovery (Railway volume lost / rebuild)
1. Recreate the Railway service from GitHub \`Zyringe/sendy-erp\` (auto-deploys on main).
2. Set env vars from \`railway-env-vars.txt\` — you only need SECRET_KEY, ADMIN_PASSWORD, DATA_DIR=/data, SESSION_COOKIE_SECURE (skip the auto-injected RAILWAY_* ones).
3. Put this .db onto the new /data volume as \`inventory.db\`; delete any stale -wal/-shm.
4. Hit /healthz (200) and confirm applied_migrations MAX = $LAST_MIG.

> Routine sync note: do NOT raw-replace the live prod .db (wipes call logs / audit / deposits). Full-file restore is disaster-recovery only; routine updates go via /admin/upload-db (master-only) or re-import.

Made by \`scripts/backup_prod_kit.sh\`.
EOF

# ── 5. retention: keep newest $KEEP kits (bash 3.2 safe — no mapfile) ─────────
{ ls -1d "$VAULT_ROOT"/*/ 2>/dev/null || true; } | sort -r | tail -n +"$((KEEP+1))" \
  | while read -r old; do
      echo "==> pruning old kit: $old"
      rm -rf "$old"
    done

rm -f "$DB_OUT"-wal "$DB_OUT"-shm   # drop sidecars created by the read queries above

echo
echo "✅ Backup kit complete: $VAULT"
ls -lh "$VAULT"
echo
echo "Off-site tip: sync ~/sendy-prod-backups/ to Google Drive/iCloud so a kit"
echo "survives this machine dying (it contains secrets — keep it private)."
