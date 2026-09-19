-- Rollback 186 — put back every label 186 changed (migration_186_snapshot) and
-- every unit_conversions row it deleted (migration_186_uc_deleted), then drop
-- both tables.
--
-- ⛔ RUN ONLY through Python (conn.executescript, then roll back on an error)
-- or `sqlite3 -bail`. The plain sqlite3 CLI keeps going after a failed
-- statement, so a half-applied rollback would still COMMIT. After it:
--   DELETE FROM applied_migrations WHERE filename = '186_unit_code_cleanup.sql';
--
-- A row is restored only while it still holds exactly what 186 wrote. Anything
-- newer is left as it is and REPORTED in temp.mig186_rollback_skipped
-- (table_name, row_id, reason, detail), which lives on the connection that ran
-- this file: `sqlite3 -bail` prints it at the end, Python reads it with
-- conn.execute('SELECT * FROM temp.mig186_rollback_skipped'). Reasons:
--   'changed after 186, left as is'        the row was edited or deleted since
--   'code row exists again, not restored'  unit_conversions already holds that
--                                          (product_id, bsn_unit) again
--   'value exists again, not restored'     same for product_code_mapping /
--                                          product_price_tiers
-- A skipped deleted conversion keeps its old ratio in `detail`.
--
-- Bill lines are restored through the mig-173 declared-change path (a plain
-- UPDATE would ABORT under *_change_needs_declaration).
--
-- A second run fails on the missing migration_186_snapshot table and changes
-- nothing.

PRAGMA busy_timeout = 10000;

BEGIN;

-- One row per snapshotted cell: its value NOW, and whether putting the old
-- value back would collide on a UNIQUE key.
DROP TABLE IF EXISTS temp._mig186_rb;
CREATE TEMP TABLE _mig186_rb AS
SELECT s.table_name, s.column_name, s.row_id, s.old_value, s.new_value,
       CASE s.table_name || '.' || s.column_name
         WHEN 'sales_transactions.unit'
           THEN (SELECT unit FROM sales_transactions WHERE id = s.row_id)
         WHEN 'purchase_transactions.unit'
           THEN (SELECT unit FROM purchase_transactions WHERE id = s.row_id)
         WHEN 'products.unit_type'
           THEN (SELECT unit_type FROM products WHERE id = s.row_id)
         WHEN 'promotions.bundle_unit'
           THEN (SELECT bundle_unit FROM promotions WHERE id = s.row_id)
         WHEN 'product_code_mapping.bsn_unit'
           THEN (SELECT bsn_unit FROM product_code_mapping WHERE id = s.row_id)
         WHEN 'pending_product_suggestions.bsn_unit'
           THEN (SELECT bsn_unit FROM pending_product_suggestions WHERE id = s.row_id)
         WHEN 'pending_product_suggestions.suggested_unit_type'
           THEN (SELECT suggested_unit_type FROM pending_product_suggestions WHERE id = s.row_id)
         WHEN 'credit_note_imports.unit'
           THEN (SELECT unit FROM credit_note_imports WHERE id = s.row_id)
         WHEN 'express_sales.unit'
           THEN (SELECT unit FROM express_sales WHERE id = s.row_id)
         WHEN 'product_price_tiers.qty_label'
           THEN (SELECT qty_label FROM product_price_tiers WHERE id = s.row_id)
         WHEN 'unit_conversions.bsn_unit'
           THEN (SELECT bsn_unit FROM unit_conversions WHERE id = s.row_id)
       END AS cur_value,
       CASE s.table_name
         WHEN 'unit_conversions' THEN EXISTS (
             SELECT 1 FROM unit_conversions u JOIN unit_conversions o
                 ON o.product_id = u.product_id AND o.id <> u.id
              WHERE u.id = s.row_id AND o.bsn_unit = s.old_value)
         WHEN 'product_code_mapping' THEN EXISTS (
             SELECT 1 FROM product_code_mapping u JOIN product_code_mapping o
                 ON o.bsn_code = u.bsn_code AND o.id <> u.id
              WHERE u.id = s.row_id AND o.bsn_unit = s.old_value)
         WHEN 'product_price_tiers' THEN EXISTS (
             SELECT 1 FROM product_price_tiers u JOIN product_price_tiers o
                 ON o.product_id = u.product_id AND o.id <> u.id
              WHERE u.id = s.row_id AND o.qty_label = s.old_value)
         ELSE 0
       END AS collides
  FROM migration_186_snapshot s;

DROP TABLE IF EXISTS temp._mig186_rb_ok;
CREATE TEMP TABLE _mig186_rb_ok AS
SELECT table_name, column_name, row_id, old_value
  FROM _mig186_rb
 WHERE cur_value IS new_value
   AND NOT collides;

DROP TABLE IF EXISTS temp.mig186_rollback_skipped;
CREATE TEMP TABLE mig186_rollback_skipped AS
SELECT table_name, row_id, 'changed after 186, left as is' AS reason,
       column_name || ': 186 wrote ' || new_value || ', now ' || COALESCE(cur_value, '(row gone)') AS detail
  FROM _mig186_rb
 WHERE cur_value IS NOT new_value
UNION ALL
SELECT table_name, row_id,
       CASE table_name WHEN 'unit_conversions' THEN 'code row exists again, not restored'
                       ELSE 'value exists again, not restored' END,
       column_name || ': ' || old_value || ' is already taken'
  FROM _mig186_rb
 WHERE cur_value IS new_value
   AND collides
UNION ALL
SELECT 'unit_conversions', d.id, 'code row exists again, not restored',
       'deleted row product_id=' || d.product_id || ' bsn_unit=' || d.bsn_unit || ' ratio=' || d.ratio
  FROM migration_186_uc_deleted d
 WHERE EXISTS (SELECT 1 FROM unit_conversions u
                WHERE u.id = d.id OR (u.product_id = d.product_id AND u.bsn_unit = d.bsn_unit));

-- ── bill lines (declared-change path) ───────────────────────────────────────
UPDATE sales_transactions
   SET unit          = (SELECT k.old_value FROM _mig186_rb_ok k
                         WHERE k.table_name = 'sales_transactions' AND k.column_name = 'unit'
                           AND k.row_id = sales_transactions.id),
       change_source = 'manual',
       change_actor  = 'mig186-rollback',
       change_token  = 'mig186-rollback-unit-' || id,
       change_reason = 'Rollback of mig 186 (GH #597 unit code cleanup): unit restored from migration_186_snapshot'
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'sales_transactions' AND column_name = 'unit');

UPDATE purchase_transactions
   SET unit          = (SELECT k.old_value FROM _mig186_rb_ok k
                         WHERE k.table_name = 'purchase_transactions' AND k.column_name = 'unit'
                           AND k.row_id = purchase_transactions.id),
       change_source = 'manual',
       change_actor  = 'mig186-rollback',
       change_token  = 'mig186-rollback-unit-' || id,
       change_reason = 'Rollback of mig 186 (GH #597 unit code cleanup): unit restored from migration_186_snapshot'
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'purchase_transactions' AND column_name = 'unit');

-- ── plain columns ───────────────────────────────────────────────────────────
UPDATE products
   SET unit_type = (SELECT k.old_value FROM _mig186_rb_ok k
                     WHERE k.table_name = 'products' AND k.column_name = 'unit_type'
                       AND k.row_id = products.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'products' AND column_name = 'unit_type');

UPDATE promotions
   SET bundle_unit = (SELECT k.old_value FROM _mig186_rb_ok k
                       WHERE k.table_name = 'promotions' AND k.column_name = 'bundle_unit'
                         AND k.row_id = promotions.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'promotions' AND column_name = 'bundle_unit');

UPDATE product_code_mapping
   SET bsn_unit = (SELECT k.old_value FROM _mig186_rb_ok k
                    WHERE k.table_name = 'product_code_mapping' AND k.column_name = 'bsn_unit'
                      AND k.row_id = product_code_mapping.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'product_code_mapping' AND column_name = 'bsn_unit');

UPDATE pending_product_suggestions
   SET bsn_unit = (SELECT k.old_value FROM _mig186_rb_ok k
                    WHERE k.table_name = 'pending_product_suggestions' AND k.column_name = 'bsn_unit'
                      AND k.row_id = pending_product_suggestions.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'pending_product_suggestions' AND column_name = 'bsn_unit');

UPDATE pending_product_suggestions
   SET suggested_unit_type = (SELECT k.old_value FROM _mig186_rb_ok k
                               WHERE k.table_name = 'pending_product_suggestions'
                                 AND k.column_name = 'suggested_unit_type'
                                 AND k.row_id = pending_product_suggestions.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'pending_product_suggestions' AND column_name = 'suggested_unit_type');

UPDATE credit_note_imports
   SET unit = (SELECT k.old_value FROM _mig186_rb_ok k
                WHERE k.table_name = 'credit_note_imports' AND k.column_name = 'unit'
                  AND k.row_id = credit_note_imports.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'credit_note_imports' AND column_name = 'unit');

UPDATE express_sales
   SET unit = (SELECT k.old_value FROM _mig186_rb_ok k
                WHERE k.table_name = 'express_sales' AND k.column_name = 'unit'
                  AND k.row_id = express_sales.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'express_sales' AND column_name = 'unit');

UPDATE product_price_tiers
   SET qty_label = (SELECT k.old_value FROM _mig186_rb_ok k
                     WHERE k.table_name = 'product_price_tiers' AND k.column_name = 'qty_label'
                       AND k.row_id = product_price_tiers.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'product_price_tiers' AND column_name = 'qty_label');

-- ── unit_conversions: undo renames, then re-insert the deleted rows ───────
UPDATE unit_conversions
   SET bsn_unit = (SELECT k.old_value FROM _mig186_rb_ok k
                    WHERE k.table_name = 'unit_conversions' AND k.column_name = 'bsn_unit'
                      AND k.row_id = unit_conversions.id)
 WHERE id IN (SELECT row_id FROM _mig186_rb_ok
               WHERE table_name = 'unit_conversions' AND column_name = 'bsn_unit');

INSERT INTO unit_conversions (id, product_id, bsn_unit, ratio, created_at)
SELECT d.id, d.product_id, d.bsn_unit, d.ratio, d.created_at
  FROM migration_186_uc_deleted d
 WHERE NOT EXISTS (SELECT 1 FROM unit_conversions u
                    WHERE u.id = d.id OR (u.product_id = d.product_id AND u.bsn_unit = d.bsn_unit));

DROP TABLE _mig186_rb_ok;
DROP TABLE _mig186_rb;
DROP TABLE migration_186_snapshot;
DROP TABLE migration_186_uc_deleted;

COMMIT;

SELECT table_name, row_id, reason, detail FROM temp.mig186_rollback_skipped;
