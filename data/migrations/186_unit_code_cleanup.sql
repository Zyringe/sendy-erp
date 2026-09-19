-- 186 — GH #597 (#595 · 2/7): translate stored Express unit CODES to the word
-- THE IMPORTER ITSELF produces for them, and merge/rename the duplicate
-- unit_conversions rows that leaves behind. Label-only: no quantity, ratio,
-- synced_to_stock or ledger row changes. Apply: restart the app (the runner in
-- database.py::init_db() picks it up). Rollback: 186_unit_code_cleanup.rollback.sql.
--
-- ⛔ RUN ONLY through the migration runner (Python executescript, which rolls
-- back on the first error) or `sqlite3 -bail`. The plain sqlite3 CLI keeps
-- going after a failed statement, so every RAISE(ABORT) guard below would fail
-- OPEN: the abort cancels its own statement, the rest of the file runs, and
-- COMMIT keeps it.
--
-- THE RULE (Opus review of #609, scope comment on #597): a stored unit is only
-- ever translated to exactly bsn_units.normalize_unit(code), and only where
-- that differs from the code. Anything else and the next import sees the stored
-- word differ from the raw code Express sends again, delete+inserts the line,
-- finds no conversion for the raw code, and moves stock (reproduced: pid 436
-- +70 with ช5 -> ชุด5). The map below is therefore the importer's own map
-- (data/reference/bsn_unit_full.json today, the unit_map table after #596)
-- minus กร/ถง/บล (Express means something else by them: #599/#600) and minus
-- the five `!` entries (not units). tests/test_migration_186_unit_code_cleanup
-- pins it EQUAL, both directions, to what bsn_units' public API produces.
-- The approved vocabulary the importer does not know yet (ช5, กิโล, กก., คค,
-- ขว ...) is #610's job, together with teaching the importer.
--
-- COLUMNS, and why each writer keeps the translation (the full file:line list
-- is in PR #609's body):
--   sales_transactions.unit, purchase_transactions.unit — the importer
--     normalises every line (models/imports.py) and compares with the stored
--     side normalised (models/bsn_line.py::_unit_same). Written through the
--     mig-173 declared-change path.
--   products.unit_type, promotions.bundle_unit, product_code_mapping.bsn_unit,
--   pending_product_suggestions.{bsn_unit,suggested_unit_type},
--   credit_note_imports.unit, express_sales.unit, product_price_tiers.qty_label
--     (leading count and its spacing kept), unit_conversions.bsn_unit — each
--     writer either normalises or never rewrites an existing row.
-- NOT covered, because their writer rewrites raw codes on the next upload:
--   express_sales_order_lines (replaced wholesale on every DBF upload),
--   express_credit_note_lines (the DBF-direct path deletes and re-inserts each
--   document with its raw unit), and the supplier tables, whose units are the
--   supplier's own words (64 prod rows of ขด = coil of rope, not Express's ขีด).
--
-- CONVERSION RULES (unit_conversions, UNIQUE(product_id, bsn_unit)):
--   code row + word row at the same ratio          -> delete the code row
--   two codes for one word (แพ + แพค), no word row,
--     same ratio                                   -> keep the lower id, delete the other
--   code row, no word row                          -> rename it to the word
--   Every deleted row is snapshotted in migration_186_uc_deleted.
--
-- PRECONDITIONS (the migration-177 shape: a temp table of violators and a
-- BEFORE DELETE trigger that RAISEs; the runner then rolls back and does not
-- stamp applied_migrations, so the DB is untouched). Each list is empty on the
-- 2026-09-19 prod snapshot. scripts/preflight_186_unit_code_cleanup.py prints
-- the violating rows of a real DB; RECOVERY per guard:
--   twin_ratio      a code row and its word row disagree on the ratio. Check
--                   the product's bills (as done for pid 1393 in the PR), fix
--                   the wrong ratio, delete the code row.
--   codes_ratio     two codes for one word disagree (แพ vs แพค). Same.
--   pcm_collision   product_code_mapping holds (bsn_code, code) next to a row
--                   that translates to the same (bsn_code, word). Decide which
--                   product the unit maps to, delete the other row.
--   tier_collision  a tier label translates onto a label the product already
--                   has. Keep one price, delete the other tier.
--   unit_type_ratio a product's unit_type and one of its conversions end up as
--                   the same word with a ratio other than 1. After 186 a line
--                   in that unit short-circuits to ratio 1
--                   (bsn_sync._get_base_qty), so the next ledger rebuild would
--                   post a different quantity. Fix the ratio or the unit_type.
--
-- ⚠ Sequencing: must not ship in the same deploy as #586's data change or
-- #600. It relabels rows of pid 689/767/1658, and the #586 script pins their
-- pre-186 units (it refuses to run if they changed underneath it).

PRAGMA busy_timeout = 10000;

BEGIN;

-- >>> mig186 map
-- Pinned EQUAL to bsn_units by the test above; do not add a pair here that
-- normalize_unit() does not produce.
DROP TABLE IF EXISTS temp._mig186_map;
CREATE TEMP TABLE _mig186_map (code TEXT PRIMARY KEY, word TEXT NOT NULL);
INSERT INTO _mig186_map (code, word) VALUES
    ('ดก', 'ดอก'),
    ('ปน', 'ปื้น'),
    ('กส', 'กระสอบ'),
    ('กล', 'กล่อง'),
    ('อน', 'อัน'),
    ('ผน', 'แผ่น'),
    ('หล', 'โหล'),
    ('ชด', 'ชุด'),
    ('ผง', 'แผง'),
    ('มน', 'ม้วน'),
    ('ลง', 'ลัง'),
    ('ซง', 'ซอง'),
    ('กก', 'กิโลกรัม'),
    ('ตว', 'ตัว'),
    ('แพ', 'แพ็ค'),
    ('ลก', 'ลูก'),
    ('ขด', 'ขีด'),
    ('ถุ', 'ถุง'),
    ('กน', 'ก้อน'),
    ('หด', 'หลอด'),
    ('หค', 'โหลคู่'),
    ('สน', 'เส้น'),
    ('กป', 'กระป๋อง'),
    ('คู', 'คู่'),
    ('หอ', 'ห่อ'),
    ('คน', 'คัน'),
    ('ทง', 'แท่ง'),
    ('ผื', 'ผืน'),
    ('แก', 'แกลลอน'),
    ('แพค', 'แพ็ค'),
    ('ชน', 'ชิ้น');
-- <<< mig186 map

-- ── Snapshot tables: forensic record + the rollback's source. IF NOT EXISTS,
--    never drop-first: a hand re-run must not erase what the rollback needs.
CREATE TABLE IF NOT EXISTS migration_186_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    row_id      INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);
CREATE INDEX IF NOT EXISTS idx_migration_186_snapshot_lookup
    ON migration_186_snapshot(table_name, row_id, column_name);

CREATE TABLE IF NOT EXISTS migration_186_uc_deleted (
    id          INTEGER NOT NULL,
    product_id  INTEGER,
    bsn_unit    TEXT,
    ratio       REAL,
    created_at  TEXT
);

-- >>> mig186 preconditions
-- Translated tier labels and mapping units, computed once and reused below.
DROP TABLE IF EXISTS temp._mig186_tier;
CREATE TEMP TABLE _mig186_tier AS
SELECT t.id, t.product_id, t.qty_label AS old_label,
       substr(t.qty_label, 1, length(t.qty_label) - length(ltrim(t.qty_label, '0123456789 ')))
       || m.word AS new_label
  FROM product_price_tiers t
  JOIN _mig186_map m ON m.code = ltrim(t.qty_label, '0123456789 ');

DROP TABLE IF EXISTS temp._mig186_pcm;
CREATE TEMP TABLE _mig186_pcm AS
SELECT p.id, p.bsn_code, p.bsn_unit AS old_unit, m.word AS new_unit
  FROM product_code_mapping p
  JOIN _mig186_map m ON m.code = p.bsn_unit;

DROP TABLE IF EXISTS temp._mig186_pre_twin_ratio;
CREATE TEMP TABLE _mig186_pre_twin_ratio AS
SELECT c.id AS uc_id
  FROM unit_conversions c
  JOIN _mig186_map m ON m.code = c.bsn_unit
  JOIN unit_conversions w ON w.product_id = c.product_id AND w.bsn_unit = m.word
 WHERE w.ratio IS NOT c.ratio;

DROP TABLE IF EXISTS temp._mig186_pre_codes_ratio;
CREATE TEMP TABLE _mig186_pre_codes_ratio AS
SELECT a.id AS uc_id
  FROM unit_conversions a
  JOIN _mig186_map ma ON ma.code = a.bsn_unit
  JOIN unit_conversions b ON b.product_id = a.product_id AND b.id <> a.id
  JOIN _mig186_map mb ON mb.code = b.bsn_unit AND mb.word = ma.word
 WHERE b.ratio IS NOT a.ratio;

DROP TABLE IF EXISTS temp._mig186_pre_pcm_collision;
CREATE TEMP TABLE _mig186_pre_pcm_collision AS
SELECT x.id AS pcm_id
  FROM _mig186_pcm x
 WHERE EXISTS (SELECT 1 FROM product_code_mapping q
                 LEFT JOIN _mig186_pcm y ON y.id = q.id
                WHERE q.bsn_code = x.bsn_code AND q.id <> x.id
                  AND COALESCE(y.new_unit, q.bsn_unit) = x.new_unit);

DROP TABLE IF EXISTS temp._mig186_pre_tier_collision;
CREATE TEMP TABLE _mig186_pre_tier_collision AS
SELECT x.id AS tier_id
  FROM _mig186_tier x
 WHERE EXISTS (SELECT 1 FROM product_price_tiers t
                 LEFT JOIN _mig186_tier y ON y.id = t.id
                WHERE t.product_id = x.product_id AND t.id <> x.id
                  AND COALESCE(y.new_label, t.qty_label) = x.new_label);

DROP TABLE IF EXISTS temp._mig186_pre_unit_type_ratio;
CREATE TEMP TABLE _mig186_pre_unit_type_ratio AS
SELECT c.id AS uc_id
  FROM unit_conversions c
  JOIN products p ON p.id = c.product_id
  LEFT JOIN _mig186_map mc ON mc.code = c.bsn_unit
  LEFT JOIN _mig186_map mp ON mp.code = p.unit_type
 WHERE (mc.code IS NOT NULL OR mp.code IS NOT NULL)
   AND COALESCE(mc.word, c.bsn_unit) = COALESCE(mp.word, p.unit_type)
   AND c.ratio IS NOT 1.0;
-- <<< mig186 preconditions

CREATE TEMP TRIGGER _mig186_guard_twin_ratio BEFORE DELETE ON _mig186_pre_twin_ratio
BEGIN SELECT RAISE(ABORT, 'mig 186 precondition FAILED: code and word conversion at different ratios on one product (twin_ratio). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig186_guard_codes_ratio BEFORE DELETE ON _mig186_pre_codes_ratio
BEGIN SELECT RAISE(ABORT, 'mig 186 precondition FAILED: two codes for one word at different ratios on one product (codes_ratio). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig186_guard_pcm_collision BEFORE DELETE ON _mig186_pre_pcm_collision
BEGIN SELECT RAISE(ABORT, 'mig 186 precondition FAILED: product_code_mapping would hold one (bsn_code, word) twice (pcm_collision). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig186_guard_tier_collision BEFORE DELETE ON _mig186_pre_tier_collision
BEGIN SELECT RAISE(ABORT, 'mig 186 precondition FAILED: product_price_tiers would hold one (product_id, label) twice (tier_collision). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig186_guard_unit_type_ratio BEFORE DELETE ON _mig186_pre_unit_type_ratio
BEGIN SELECT RAISE(ABORT, 'mig 186 precondition FAILED: unit_type and a conversion become the same word at a ratio other than 1 (unit_type_ratio). RECOVERY in the migration header.'); END;

DELETE FROM _mig186_pre_twin_ratio;
DELETE FROM _mig186_pre_codes_ratio;
DELETE FROM _mig186_pre_pcm_collision;
DELETE FROM _mig186_pre_tier_collision;
DELETE FROM _mig186_pre_unit_type_ratio;
DROP TABLE _mig186_pre_twin_ratio;
DROP TABLE _mig186_pre_codes_ratio;
DROP TABLE _mig186_pre_pcm_collision;
DROP TABLE _mig186_pre_tier_collision;
DROP TABLE _mig186_pre_unit_type_ratio;

-- ── 1-2. bill lines, through the mig-173 declared-change path. change_reason
--    is set NULL (the guard requires one only for 'manual'): left alone, the
--    audit row would repeat the reason of whoever last edited the line.
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'sales_transactions', s.id, 'unit', s.unit, m.word
  FROM sales_transactions s JOIN _mig186_map m ON m.code = s.unit;

UPDATE sales_transactions
   SET unit          = (SELECT word FROM _mig186_map WHERE code = sales_transactions.unit),
       change_source = 'import',
       change_actor  = 'mig186-unit-cleanup',
       change_token  = 'mig186-unit-' || id,
       change_reason = NULL
 WHERE unit IN (SELECT code FROM _mig186_map);

INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'purchase_transactions', p.id, 'unit', p.unit, m.word
  FROM purchase_transactions p JOIN _mig186_map m ON m.code = p.unit;

UPDATE purchase_transactions
   SET unit          = (SELECT word FROM _mig186_map WHERE code = purchase_transactions.unit),
       change_source = 'import',
       change_actor  = 'mig186-unit-cleanup',
       change_token  = 'mig186-unit-' || id,
       change_reason = NULL
 WHERE unit IN (SELECT code FROM _mig186_map);

-- ── 3. products.unit_type ──────────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'products', p.id, 'unit_type', p.unit_type, m.word
  FROM products p JOIN _mig186_map m ON m.code = p.unit_type;

UPDATE products
   SET unit_type = (SELECT word FROM _mig186_map WHERE code = products.unit_type)
 WHERE unit_type IN (SELECT code FROM _mig186_map);

-- ── 4. promotions.bundle_unit ──────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'promotions', p.id, 'bundle_unit', p.bundle_unit, m.word
  FROM promotions p JOIN _mig186_map m ON m.code = p.bundle_unit;

UPDATE promotions
   SET bundle_unit = (SELECT word FROM _mig186_map WHERE code = promotions.bundle_unit)
 WHERE bundle_unit IN (SELECT code FROM _mig186_map);

-- ── 5. product_code_mapping.bsn_unit (collisions refused above) ───────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'product_code_mapping', id, 'bsn_unit', old_unit, new_unit FROM _mig186_pcm;

UPDATE product_code_mapping
   SET bsn_unit = (SELECT new_unit FROM _mig186_pcm x WHERE x.id = product_code_mapping.id)
 WHERE id IN (SELECT id FROM _mig186_pcm);

-- ── 6. pending_product_suggestions.bsn_unit + .suggested_unit_type ────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'pending_product_suggestions', p.id, 'bsn_unit', p.bsn_unit, m.word
  FROM pending_product_suggestions p JOIN _mig186_map m ON m.code = p.bsn_unit;

UPDATE pending_product_suggestions
   SET bsn_unit = (SELECT word FROM _mig186_map WHERE code = pending_product_suggestions.bsn_unit)
 WHERE bsn_unit IN (SELECT code FROM _mig186_map);

INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'pending_product_suggestions', p.id, 'suggested_unit_type', p.suggested_unit_type, m.word
  FROM pending_product_suggestions p JOIN _mig186_map m ON m.code = p.suggested_unit_type;

UPDATE pending_product_suggestions
   SET suggested_unit_type = (SELECT word FROM _mig186_map
                               WHERE code = pending_product_suggestions.suggested_unit_type)
 WHERE suggested_unit_type IN (SELECT code FROM _mig186_map);

-- ── 7. credit_note_imports.unit ────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'credit_note_imports', c.id, 'unit', c.unit, m.word
  FROM credit_note_imports c JOIN _mig186_map m ON m.code = c.unit;

UPDATE credit_note_imports
   SET unit = (SELECT word FROM _mig186_map WHERE code = credit_note_imports.unit)
 WHERE unit IN (SELECT code FROM _mig186_map);

-- ── 8. express_sales.unit ──────────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'express_sales', e.id, 'unit', e.unit, m.word
  FROM express_sales e JOIN _mig186_map m ON m.code = e.unit;

UPDATE express_sales
   SET unit = (SELECT word FROM _mig186_map WHERE code = express_sales.unit)
 WHERE unit IN (SELECT code FROM _mig186_map);

-- ── 9. product_price_tiers.qty_label (collisions refused above) ───────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'product_price_tiers', id, 'qty_label', old_label, new_label FROM _mig186_tier;

UPDATE product_price_tiers
   SET qty_label = (SELECT new_label FROM _mig186_tier x WHERE x.id = product_price_tiers.id)
 WHERE id IN (SELECT id FROM _mig186_tier);

-- ── 10. unit_conversions ───────────────────────────────────────────────────
-- (a) a code row whose word row already exists (same ratio, checked above)
INSERT INTO migration_186_uc_deleted (id, product_id, bsn_unit, ratio, created_at)
SELECT c.id, c.product_id, c.bsn_unit, c.ratio, c.created_at
  FROM unit_conversions c
  JOIN _mig186_map m ON m.code = c.bsn_unit
 WHERE EXISTS (SELECT 1 FROM unit_conversions w
                WHERE w.product_id = c.product_id AND w.bsn_unit = m.word);

-- (b) two codes for one word and no word row: keep the lower id
INSERT INTO migration_186_uc_deleted (id, product_id, bsn_unit, ratio, created_at)
SELECT c.id, c.product_id, c.bsn_unit, c.ratio, c.created_at
  FROM unit_conversions c
  JOIN _mig186_map m ON m.code = c.bsn_unit
 WHERE c.id NOT IN (SELECT id FROM migration_186_uc_deleted)
   AND EXISTS (SELECT 1 FROM unit_conversions o
                 JOIN _mig186_map mo ON mo.code = o.bsn_unit
                WHERE o.product_id = c.product_id AND mo.word = m.word AND o.id < c.id);

DELETE FROM unit_conversions WHERE id IN (SELECT id FROM migration_186_uc_deleted);

-- (c) every code row left is the only one for its word: rename it
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'unit_conversions', c.id, 'bsn_unit', c.bsn_unit, m.word
  FROM unit_conversions c JOIN _mig186_map m ON m.code = c.bsn_unit;

UPDATE unit_conversions
   SET bsn_unit = (SELECT word FROM _mig186_map WHERE code = unit_conversions.bsn_unit)
 WHERE bsn_unit IN (SELECT code FROM _mig186_map);

-- ── Postcondition: no covered column still holds a mapped code ───────────
DROP TABLE IF EXISTS temp._mig186_postcheck;
CREATE TEMP TABLE _mig186_postcheck AS
          SELECT id FROM sales_transactions          WHERE unit                IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM purchase_transactions       WHERE unit                IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM products                    WHERE unit_type           IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM promotions                  WHERE bundle_unit         IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM product_code_mapping        WHERE bsn_unit            IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM pending_product_suggestions WHERE bsn_unit            IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM pending_product_suggestions WHERE suggested_unit_type IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM credit_note_imports         WHERE unit                IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM express_sales               WHERE unit                IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM unit_conversions            WHERE bsn_unit            IN (SELECT code FROM _mig186_map)
UNION ALL SELECT id FROM product_price_tiers         WHERE ltrim(qty_label, '0123456789 ') IN (SELECT code FROM _mig186_map);

CREATE TEMP TRIGGER _mig186_postcondition_guard BEFORE DELETE ON _mig186_postcheck
BEGIN SELECT RAISE(ABORT, 'mig 186 postcondition FAILED: a covered column still holds a code this migration translates.'); END;
DELETE FROM _mig186_postcheck;
DROP TRIGGER _mig186_postcondition_guard;
DROP TABLE _mig186_postcheck;
DROP TABLE _mig186_tier;
DROP TABLE _mig186_pcm;
DROP TABLE _mig186_map;

COMMIT;
