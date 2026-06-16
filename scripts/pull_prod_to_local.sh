#!/usr/bin/env bash
#
# pull_prod_to_local.sh — replace the LOCAL Sendy DB with a consistent
# snapshot of PROD (Railway), so local becomes a faithful staging replica
# for testing (includes team-typed data like customer_call_log).
#
# This is the prod->local direction ONLY. See references/sops/local-prod-db-sync.md.
#   prod -> local : full .db replace (local is disposable)   <- THIS SCRIPT
#   local -> prod : NEVER replace the file. master-only upload + re-import.
#
# Fully automated: pulls a consistent snapshot via `railway ssh` (sqlite
# .backup in-container + gzip+base64 stream) — no manual "Download DB" click.
# Backs up the current local DB first, swaps the file, removes the stale
# -wal/-shm sidecars (skipping that is the classic corruption footgun),
# restarts Sendy, and sanity-checks the team-data made it down.
#
# Usage:   scripts/pull_prod_to_local.sh [--yes]
#   --yes   skip the "this overwrites local" confirmation prompt
#
set -euo pipefail

SENDY_ERP_DIR="${SENDY_ERP_DIR:-$HOME/Sendai-Boonsawat/sendy_erp}"
APP_DIR="$SENDY_ERP_DIR/inventory_app"
DB="$APP_DIR/instance/inventory.db"
PY="$HOME/.virtualenvs/erp/bin/python"
PORT=5001
TS="$(date +%Y%m%d_%H%M%S)"
TMP_PULL="/tmp/prod_pull_${TS}.db"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

die() { echo "ERROR: $*" >&2; exit 1; }

# ── 0. preflight ─────────────────────────────────────────────────────────────
command -v railway >/dev/null 2>&1 || die "railway CLI not found. brew install railway"
railway whoami >/dev/null 2>&1 || die "not logged in. Run:  railway login   (then re-run this)"
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not found"
[ -x "$PY" ] || die "erp venv python not found at $PY"
[ -d "$APP_DIR" ] || die "Sendy app dir not found at $APP_DIR"

# ── 1. pull a CONSISTENT prod snapshot (no manual download) ──────────────────
echo "==> Pulling a consistent prod snapshot via railway ssh ..."
railway ssh "python3 -c \"import sqlite3; s=sqlite3.connect('/data/inventory.db'); d=sqlite3.connect('/tmp/snap.db'); s.backup(d); d.close(); s.close()\" && gzip -c /tmp/snap.db | base64 && rm -f /tmp/snap.db" 2>/dev/null \
  | "$PY" -c "import sys,base64,gzip; open('$TMP_PULL','wb').write(gzip.decompress(base64.b64decode(sys.stdin.read())))" \
  || die "snapshot fetch/decode failed (railway ssh + decode). Nothing changed locally."

[ -s "$TMP_PULL" ] || die "pulled file is empty: $TMP_PULL"
ICHK="$(sqlite3 "$TMP_PULL" 'PRAGMA integrity_check;' 2>&1 | head -1)"
[ "$ICHK" = "ok" ] || die "pulled snapshot failed integrity_check ($ICHK). Nothing changed locally."

PROD_CALLS="$(sqlite3 "$TMP_PULL" 'SELECT COUNT(*) FROM customer_call_log;' 2>/dev/null || echo '?')"
PROD_ORDERS="$(sqlite3 "$TMP_PULL" 'SELECT COUNT(*) FROM marketplace_orders;' 2>/dev/null || echo '?')"
PROD_MIG="$(sqlite3 "$TMP_PULL" 'SELECT MAX(filename) FROM applied_migrations;' 2>/dev/null || echo '?')"
echo "    prod snapshot OK: call_logs=$PROD_CALLS  marketplace_orders=$PROD_ORDERS  last_mig=$PROD_MIG"

# ── 2. confirm (this OVERWRITES local) ───────────────────────────────────────
if [ "$ASSUME_YES" -ne 1 ]; then
  echo
  echo "This will REPLACE your local DB ($DB) with the prod snapshot above."
  echo "Local data not on prod will be lost. (Push pending master/catalog edits FIRST.)"
  printf "Continue? [y/N] "
  read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) rm -f "$TMP_PULL"; die "aborted by user. Nothing changed." ;; esac
fi

# ── 3. stop Sendy ────────────────────────────────────────────────────────────
echo "==> Stopping Sendy on :$PORT ..."
pids="$(lsof -ti:"$PORT" 2>/dev/null || true)"
if [ -n "$pids" ]; then
  echo "$pids" | xargs kill 2>/dev/null || true
  sleep 1
  lsof -ti:"$PORT" >/dev/null 2>&1 && { echo "$pids" | xargs kill -9 2>/dev/null || true; }
fi

# ── 4. back up current local, then swap ──────────────────────────────────────
if [ -f "$DB" ]; then
  BK="$APP_DIR/instance/_prepull-${TS}.db"
  echo "==> Backing up current local DB -> $BK"
  sqlite3 "$DB" ".backup '$BK'" || die "local backup failed; aborting BEFORE swap. Local untouched."
fi
echo "==> Swapping in prod snapshot + clearing stale WAL/SHM ..."
mv -f "$TMP_PULL" "$DB"
rm -f "${DB}-wal" "${DB}-shm"

# ── 5. restart Sendy + verify ────────────────────────────────────────────────
echo "==> Starting Sendy ..."
( cd "$APP_DIR" && nohup "$PY" app.py > /tmp/sendy.log 2>&1 & )
code=""
for _ in $(seq 1 25); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true)"
  [ "$code" = "200" ] && break
  sleep 1
done
[ "$code" = "200" ] || die "Sendy did not return /healthz 200 (got '$code'). Check /tmp/sendy.log"

LOCAL_CALLS="$(sqlite3 "$DB" 'SELECT COUNT(*) FROM customer_call_log;' 2>/dev/null || echo '?')"
echo
echo "✅ Done. Local is now a prod replica."
echo "   /healthz=200 · local call_logs=$LOCAL_CALLS (prod had $PROD_CALLS)"
[ "$LOCAL_CALLS" = "$PROD_CALLS" ] || echo "   ⚠ call_log count mismatch — check /tmp/sendy.log"
echo
echo "Next: test your import on this replica, then push to prod the SAFE way —"
echo "  catalog/master -> /admin/upload-db (master-only)"
echo "  marketplace/ขาย/ซื้อ/Express -> re-import the SAME file on prod (idempotent)"
echo "  NEVER replace the prod .db file (wipes call logs, audit log, deposits)."
