# Audit handoff — staff import access + auto-backup-before-import (2026-06-03)

Repo: `Zyringe/sendy-erp` (Flask + SQLite ERP "Sendy"). Both changes are **merged to `main` and deployed to Railway** (gunicorn `-w 2`, prod DB on a 500MB persistent volume at `/data`).

Two squash commits on `main`:
- `7e567d4` — #106 staff import access
- `2595e33` — #108 auto-backup before every import + admin restore

Audit goal: verify correctness + safety of (a) the permission change and (b) the new backup/restore subsystem, with emphasis on prod-data-safety risks.

---

## Change 1 — Staff role can run imports (#106 `7e567d4`)

**Files:** `inventory_app/app.py`, `tests/test_unified_import_routes.py`, `tests/test_marketplace_orders.py`

**What changed:**
- Added `unified_import`, `unified_import_confirm`, `marketplace.import_orders` to `_STAFF_POST_OK` (the POST-permission whitelist in `app.py`, enforced by `before_request`). Manager inherits via `_MANAGER_POST_OK = _STAFF_POST_OK | {...}`.
- Removed the inline `if session.get('role') not in ('admin','manager')` gate at the top of `unified_import` and `unified_import_confirm`. The whitelist is now the single gate (same pattern as the pre-existing `/import-weekly`).

**Design decision (important):** "Decision B" — staff **commit imports directly**; manager/admin review **after the fact**. NOT a pre-commit approval queue. The queue was rejected because the staged upload lives in the Flask **session cookie** (`session['import_stage']`) and `login` calls `session.clear()`, so a *different* manager login cannot confirm a staff member's staged upload without a new server-side queue feature.

**Audit focus:**
- Did adding those 3 endpoints to `_STAFF_POST_OK` unintentionally expose anything else? (Confirm each endpoint only does what's intended; no admin-only side effects reachable.)
- `before_request` flow: staff/manager POST to non-whitelisted endpoints must still redirect; admin can POST anything. GET pages: staff are only hard-blocked from `hr.*` and `cashbook.*` — confirm `/import-data` GET being open to staff is acceptable.
- CSRF still enforced on the import POSTs (flask-wtf global `CSRFProtect`).

---

## Change 2 — Auto-snapshot DB before every import + admin restore (#108 `2595e33`)

**Files:** `inventory_app/db_backup.py` (new), `inventory_app/app.py` (routes + helper), `inventory_app/blueprints/marketplace.py`, `inventory_app/templates/admin_backups.html` (new), `inventory_app/templates/base.html`, `inventory_app/templates/import_box.html`, `tests/test_db_backup.py` (new), `tests/test_backup_routes.py` (new)

**What changed:**
- `db_backup.py` (pure, no Flask): `create_backup(reason)` writes a gzipped SQLite online `.backup()` to `<dir-of-DATABASE_PATH>/backups/auto-<reason>-<YYYYMMDD_HHMMSS>.db.gz`. `list_backups`, `prune_backups`, `restore_backup`, `safe_create_backup` (never raises), `disk_usage_mb`.
- `_snapshot_before_import(reason)` in `app.py` is called at the commit points of `unified_import_confirm` ('unified'), `import_weekly_confirm` ('weekly'); `blueprints/marketplace.py::import_orders` calls `safe_create_backup('marketplace')` before the upsert. **Best-effort**: a backup failure is flashed as a warning; the import still proceeds.
- New routes (admin-only): `GET /admin/backups` (list + volume-usage bar), `GET /admin/backups/download/<name>`, `POST /admin/backups/restore`. Download/restore additionally require the existing `session['db_routes_enabled']` toggle. Restore is admin-only by NOT being in any POST whitelist (admin bypasses; manager/staff redirected by `before_request`).
- Sidebar link added in `base.html` (only renders when `active_module == 'admin_module' and is_admin`); a convenience admin link added on `import_box.html`.

**Retention / 500MB-volume tuning (DB is 140MB, gzips to ~17MB):**
- The uncompressed `.backup` temp (~140MB) is written to the **system temp dir** (`tempfile.mkstemp` default = off-volume `/tmp`); only the ~17MB gzip lands on `/data`.
- `prune_backups(keep_days=7, max_keep=5)`: deletes snapshots older than 7 days OR beyond the newest 5; always keeps the newest; never touches the live DB or non-`auto-*` files. Runs before AND after each create.
- `create_backup` refuses (raises `RuntimeError`, caught → warning) if free space `< MIN_FREE_BYTES` (60MB).
- `restore_backup` pre-checks the volume can hold a full-size decompress temp (`getsize(db) + 40MB`) before starting.

**Restore mechanics:** snapshots current state first (`reason=pre-restore`) → decompress chosen `.gz` to a temp in the live-DB dir → `PRAGMA schema_version` probe → `os.replace(temp, DATABASE_PATH)` (atomic, same fs) → delete `<db>-wal` / `<db>-shm`. Name is validated by `_NAME_RE` (no `/` or `.`, anchored) + a `dirname == backup_dir` guard.

**Caveat (documented in UI):** restore reverts the **whole DB** to that snapshot, not just the bad import.

### Audit focus (highest priority — prod data safety):
1. **Restore under gunicorn `-w 2`:** `os.replace` swaps the file while a second worker may hold an open connection to the old inode. `get_connection()` opens a fresh connection per request, so new requests get the new file, but assess in-flight-request risk and whether the `-wal`/`-shm` deletion is correct/sufficient (could a sibling worker recreate a WAL mid-restore?). UI advises restarting after restore — is that enough, or should restore force a restart?
2. **WAL correctness of the snapshot:** confirm the `.backup()` online API captures committed-but-unckecked WAL pages (vs a bare file copy). There's a test (`test_create_backup_captures_committed_wal_data`) — verify it actually proves this.
3. **Disk-space math on the 500MB volume:** peak usage during a backup (off-volume temp) and during a restore (on-volume 140MB decompress temp + 17MB pre-restore gz + existing backups + 224MB baseline). Could a restore at high baseline overflow 500MB despite the guard? Is `MIN_FREE_BYTES=60MB` enough headroom for the 17MB gz write?
4. **`tempfile` temp location assumption:** code assumes the system temp dir is off the `/data` volume on Railway. Verify `TMPDIR`/`/tmp` is the container's ephemeral disk, not the volume, and that it has room for a 140MB temp.
5. **Path traversal / name validation** on `download` and `restore` (`<name>`): confirm `_NAME_RE` + the `dirname` guard fully prevent reading/replacing arbitrary files. The download route also cross-checks `name in list_backups()`.
6. **Best-effort backup swallowing errors:** `safe_create_backup` catches all exceptions → import proceeds without a restore point. Is "warn and proceed" the right call, or should some failures block? Confirm a failure can't leave a half-written `.gz`.
7. **Prune safety:** never deletes the live DB or foreign files; "always keep newest" floor; behavior when `backup_dir` doesn't exist.
8. **Marketplace re-import is an UPSERT on `(platform, order_sn)`** (`models.import_marketplace_orders`) — re-importing OVERWRITES order `status` + all header fields and rebuilds line items. This is pre-existing behavior (not changed here) but now staff can trigger it; flag as a data-overwrite path (no manual-edit protection, no manual-edit UI exists).

---

## Test status
- New: `tests/test_db_backup.py` (12), `tests/test_backup_routes.py` (5). Cover gzip+WAL round-trip, prune (days / count cap / newest-floor / never-touch-live), restore round-trip + sidecar cleanup + name validation + disk-low refusal, admin gate, restore-blocked-without-db-routes-toggle.
- `tests/test_unified_import_routes.py` + `tests/test_marketplace_orders.py` updated for staff access.
- **Full suite: 936 passed, 22 skipped** (skips are pre-existing environmental/superseded). Server restarts clean, `/healthz` 200.
- Audit suggestion: the suite was run by the author — re-run independently; check that the backup-on-import wiring didn't silently change any import test's expected DB state.

## Known limitations / NOT built (by decision)
- No "review afterward" surface for Decision B: no import-history / who-imported page (`import_log` has no `imported_by` column).
- Railway volume is 500MB (Trial cap, can't resize via CLI); retention is tuned for it. If upgraded (Hobby → 5GB), `DEFAULT_MAX_KEEP` should be raised.
- This is code only — no prod DB *data* migration was performed.
