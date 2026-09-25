-- Rollback 195 — put back every supplier unit label 195 changed
-- (migration_195_snapshot), remove the unit_map rows it added
-- (migration_195_unit_map_added), then drop both tables.
--
-- ⛔ RUN ONLY through Python (conn.executescript, then roll back on an error) or
-- `sqlite3 -bail`. The plain sqlite3 CLI keeps going after a failed statement, so
-- a half-applied rollback would still COMMIT. After it:
--   DELETE FROM applied_migrations WHERE filename = '195_supplier_unit_spellings.sql';
--
-- A row is restored only while it still holds exactly what 195 wrote. Anything
-- edited since is left as it is and REPORTED in temp.mig195_rollback_skipped
-- (table_name, row_id, reason, detail), which lives on the connection that ran
-- this file: `sqlite3 -bail` prints it at the end, Python reads it with
-- conn.execute('SELECT * FROM temp.mig195_rollback_skipped'). Reasons:
--   'changed after 195, left as is'   the unit is no longer what 195 wrote (for a
--                                     unit_map row: its word changed, so somebody
--                                     has since ruled on that spelling)
--   'row gone, nothing to restore'    the row was deleted since
-- `detail` carries the value the snapshot holds, so a skipped row stays
-- recoverable by hand from the report.
--
-- ⚠ The unit_map rows are removed only when they still say what 195 seeded. A
-- spelling somebody re-pointed afterwards is that person's decision and outlives
-- this rollback.

DROP TABLE IF EXISTS temp.mig195_rollback_skipped;
CREATE TEMP TABLE mig195_rollback_skipped (
    table_name TEXT,
    row_id     INTEGER,
    reason     TEXT,
    detail     TEXT
);

-- ── supplier_catalogue_items ────────────────────────────────────────────────
INSERT INTO mig195_rollback_skipped (table_name, row_id, reason, detail)
SELECT s.table_name, s.row_id, 'row gone, nothing to restore',
       'snapshot old_value ' || COALESCE(s.old_value, 'NULL')
  FROM migration_195_snapshot s
 WHERE s.table_name = 'supplier_catalogue_items'
   AND NOT EXISTS (SELECT 1 FROM supplier_catalogue_items t WHERE t.id = s.row_id);

INSERT INTO mig195_rollback_skipped (table_name, row_id, reason, detail)
SELECT s.table_name, s.row_id, 'changed after 195, left as is',
       'holds ' || COALESCE((SELECT t.unit FROM supplier_catalogue_items t
                              WHERE t.id = s.row_id), 'NULL')
         || ', 195 wrote ' || COALESCE(s.new_value, 'NULL')
  FROM migration_195_snapshot s
 WHERE s.table_name = 'supplier_catalogue_items'
   AND EXISTS (SELECT 1 FROM supplier_catalogue_items t WHERE t.id = s.row_id)
   AND (SELECT t.unit FROM supplier_catalogue_items t WHERE t.id = s.row_id)
       IS NOT s.new_value;

UPDATE supplier_catalogue_items
   SET unit = (SELECT s.old_value FROM migration_195_snapshot s
                WHERE s.table_name = 'supplier_catalogue_items'
                  AND s.row_id = supplier_catalogue_items.id AND s.column_name = 'unit')
 WHERE id IN (SELECT s.row_id FROM migration_195_snapshot s
               WHERE s.table_name = 'supplier_catalogue_items'
                 AND s.column_name = 'unit'
                 AND s.row_id NOT IN (SELECT row_id FROM mig195_rollback_skipped
                                       WHERE table_name = 'supplier_catalogue_items'));

-- ── supplier_catalogue_price_history ────────────────────────────────────────
INSERT INTO mig195_rollback_skipped (table_name, row_id, reason, detail)
SELECT s.table_name, s.row_id, 'row gone, nothing to restore',
       'snapshot old_value ' || COALESCE(s.old_value, 'NULL')
  FROM migration_195_snapshot s
 WHERE s.table_name = 'supplier_catalogue_price_history'
   AND NOT EXISTS (SELECT 1 FROM supplier_catalogue_price_history t WHERE t.id = s.row_id);

INSERT INTO mig195_rollback_skipped (table_name, row_id, reason, detail)
SELECT s.table_name, s.row_id, 'changed after 195, left as is',
       'holds ' || COALESCE((SELECT t.unit FROM supplier_catalogue_price_history t
                              WHERE t.id = s.row_id), 'NULL')
         || ', 195 wrote ' || COALESCE(s.new_value, 'NULL')
  FROM migration_195_snapshot s
 WHERE s.table_name = 'supplier_catalogue_price_history'
   AND EXISTS (SELECT 1 FROM supplier_catalogue_price_history t WHERE t.id = s.row_id)
   AND (SELECT t.unit FROM supplier_catalogue_price_history t WHERE t.id = s.row_id)
       IS NOT s.new_value;

UPDATE supplier_catalogue_price_history
   SET unit = (SELECT s.old_value FROM migration_195_snapshot s
                WHERE s.table_name = 'supplier_catalogue_price_history'
                  AND s.row_id = supplier_catalogue_price_history.id
                  AND s.column_name = 'unit')
 WHERE id IN (SELECT s.row_id FROM migration_195_snapshot s
               WHERE s.table_name = 'supplier_catalogue_price_history'
                 AND s.column_name = 'unit'
                 AND s.row_id NOT IN (SELECT row_id FROM mig195_rollback_skipped
                                       WHERE table_name = 'supplier_catalogue_price_history'));

-- ── the map rows it added ───────────────────────────────────────────────────
INSERT INTO mig195_rollback_skipped (table_name, row_id, reason, detail)
SELECT 'unit_map', u.id, 'changed after 195, left as is',
       u.book || ' ' || u.spelling || ' now says ' || u.word || ', 195 seeded ' || a.word
  FROM migration_195_unit_map_added a
  JOIN unit_map u ON u.book = a.book AND u.spelling = a.spelling
 WHERE u.word IS NOT a.word;

DELETE FROM unit_map
 WHERE EXISTS (SELECT 1 FROM migration_195_unit_map_added a
                WHERE a.book = unit_map.book AND a.spelling = unit_map.spelling
                  AND a.word IS unit_map.word);

DROP TABLE IF EXISTS migration_195_snapshot;
DROP TABLE IF EXISTS migration_195_unit_map_added;
