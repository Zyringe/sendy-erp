# ERP DB — Manual Restore Procedure

Restore from a backup file written by `scripts/backup_db.sh`. Never auto-restore.

## Pre-flight

1. **Stop the Flask app.** Restoring while it's running risks a corrupted target file or stale cache.
   ```
   pkill -f 'inventory_app/app.py'      # or stop your dev server / launchd
   ```
2. **Identify the backup to restore.** Daily, monthly, and yearly snapshots live under:
   - `/Users/putty/Sendai-Boonsawat/sendy_erp/data/backups/inventory-YYYY-MM-DD.db`
   - `/Users/putty/Sendai-Boonsawat/sendy_erp/data/backups/monthly/inventory-YYYY-MM.db`
   - `/Users/putty/Sendai-Boonsawat/sendy_erp/data/backups/yearly/inventory-YYYY.db`

   List candidates with row counts:
   ```
   for f in /Users/putty/Sendai-Boonsawat/sendy_erp/data/backups/inventory-*.db; do
       echo "$f"
       sqlite3 "$f" "SELECT 'products', COUNT(*) FROM products UNION ALL SELECT 'transactions', COUNT(*) FROM transactions;"
   done
   ```
3. **Verify backup integrity.**
   ```
   sqlite3 /path/to/backup.db 'PRAGMA integrity_check;'
   # expect: ok
   ```

## Restore

1. **Move the current live DB aside** (do not delete — keep as recovery point).
   ```
   LIVE=/Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db
   mv "$LIVE" "${LIVE}.before-restore-$(date +%Y%m%d-%H%M%S)"
   ```
2. **Copy the backup into place.** Plain `cp` is fine here because the live file is no longer in use.
   ```
   cp /Users/putty/Sendai-Boonsawat/sendy_erp/data/backups/inventory-YYYY-MM-DD.db "$LIVE"
   ```
3. **Verify.**
   ```
   sqlite3 "$LIVE" 'PRAGMA integrity_check;'
   sqlite3 "$LIVE" "SELECT COUNT(*) FROM products;"
   sqlite3 "$LIVE" "SELECT COUNT(*) FROM transactions;"
   sqlite3 "$LIVE" "SELECT MAX(created_at) FROM transactions;"
   ```
4. **Restart the Flask app** and smoke-test:
   - `/dashboard` loads
   - `/products` shows expected count
   - `/transactions` shows expected last entry timestamp

## If something is wrong after restore

The pre-restore live DB is still on disk as `inventory.db.before-restore-<timestamp>`. To revert:
```
mv /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
   /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db.failed-restore
mv /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db.before-restore-* \
   /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db
```

## Notes

- WAL files: SQLite may leave `inventory.db-wal` and `inventory.db-shm` next to the live DB. After restore, if those exist alongside the moved-aside file, leave them; SQLite will recreate fresh ones for the restored DB. If you see odd behavior, with the app stopped, you can safely delete stale `.db-wal` / `.db-shm` files belonging to the old DB.
- TCC: copying inside `~/Documents` works from an interactive terminal that already has access. If running from a fresh shell or daemon, ensure Full Disk Access is granted to the process.
- Migrations: a restored DB may be at an older schema version. Run `init_db()` (start the Flask app) afterward to apply any idempotent schema migrations baked into `database.py`.
