-- 186 — GH #597 (#595 · 2/7): translate leftover Express unit CODES to their
-- หน่วย word, for codes whose MEANING does not change, and merge/rename
-- duplicate unit_conversions rows. Apply: restart the app (runner picks this
-- up via database.py::init_db()). Rollback: 186_unit_code_cleanup.rollback.sql
-- (restores migration_186_snapshot + migration_186_uc_deleted, then drops
-- both so sqlite_master returns to its pre-186 shape).
--
-- SCOPE (ticket #597; the full unit-word project is #595 · 7 tickets):
-- one Express-code vocabulary → word, applied to every column that stores
-- Express-derived units: sales_transactions.unit, purchase_transactions.unit
-- (both go through the mig-173 declared-change path — they are guarded),
-- products.unit_type, promotions.bundle_unit, product_code_mapping.bsn_unit,
-- pending_product_suggestions.{bsn_unit,suggested_unit_type},
-- credit_note_imports.unit, express_credit_note_lines.unit,
-- express_sales.unit, express_sales_order_lines.unit,
-- product_price_tiers.qty_label (leading count + its spacing kept, only the
-- unit part translated), and unit_conversions.bsn_unit (merge same-ratio
-- twins, rename code-only rows, ABORT on a disagreeing twin).
--
-- DELIBERATELY EXCLUDED from the map (left exactly as-is, on every column):
--   กร, ถง, บล  — Express's OWN meaning for these three differs from what
--                 Sendy's map has said since 2026-05-18 (กร=กุรุส not ตัว,
--                 ถง=ถัง not ถุง, บล=บล็อก not แผง). Relabelling them is
--                 ticket #600 (needs the Express stock card per document/
--                 line, not a blind find-replace); ticket #599 makes new
--                 bills correct going forward. Their rows and conversions
--                 are counted below and reported in the PR, not touched.
--   หอ (xp5 only) — หอ IS included in THIS migration's map (→ ห่อ), because
--                 every table this migration touches is BSN5657-book data
--                 (verified: express_sales_order_lines is populated by
--                 blueprints/bsn.py's daily DBF import against the SAME
--                 book that feeds sales_transactions/purchase_transactions
--                 — see express_registers.py's docstring and
--                 vat_book_builder.py's own header, which builds the xp5
--                 VAT book into a SEPARATE file, vat_book.db, that this
--                 migration never opens). "หอ" as raw BSN5657 data already
--                 means ห่อ; it is xp5's DIFFERENT meaning (หลอด) that must
--                 never leak in here, and it structurally cannot — xp5's
--                 book lives in a different sqlite file entirely (ticket
--                 #601's job, the VAT-book rebuild).
--   the five `!` entries (!กล !คู !ลก !หด !หล) — not units, 0 rows on prod.
--
-- ⚠ supplier_catalogue_items.unit / supplier_catalogue_price_history.unit /
-- supplier_product_mapping.supplier_unit use a DELIBERATELY SMALLER map
-- (_mig186_supplier_map below) — the universal Sendy-spelling variants only
-- (กก/กก./กิโล/1กิโล→กิโลกรัม, แพ/แพค→แพ็ค, กล.เล็ก→กล่องเล็ก), NOT the full
-- Express short-code vocabulary. FINDING (not in the ticket, found rehearsing
-- on prod 2026-09-19): supplier_catalogue_items.unit holds 64 rows of literal
-- "ขด" meaning COIL (เชือกเขียวขี้ม้า, a rope, sold by the coil) — this is a
-- supplier's own vocabulary, unrelated to Express's "ขด" code (which means
-- ขีด, a notch/count unit, on the BSN5657 stock card). Applying the full
-- Express map to supplier data would have silently renamed 64 correct
-- "coil" rows to the wrong word "ขีด". supplier_product_mapping.erp_unit
-- (Sendy's own vocabulary side of that table) DOES use the full map.
--
-- CONVERSION RULES (unit_conversions, UNIQUE(product_id, bsn_unit)):
--   code row + matching-ratio word row already exists  -> DELETE the code row
--   code row, no word row for that product              -> RENAME to the word
--   code row + word row at a DIFFERENT ratio             -> ABORT (below),
--     before touching anything -- migration-177 precondition shape: a temp
--     table of violators + a BEFORE DELETE trigger that RAISEs. RECOVERY:
--       SELECT c.product_id, c.bsn_unit AS code, c.ratio AS code_ratio,
--              m.word, w.ratio AS word_ratio
--         FROM unit_conversions c
--         JOIN _mig186_full_map m ON m.code = c.bsn_unit
--         JOIN unit_conversions w ON w.product_id = c.product_id
--                                 AND w.bsn_unit = m.word
--        WHERE w.ratio IS NOT c.ratio;
--     then decide per product which ratio is right (check its bills, same
--     as the pid 1393 check below) and fix the wrong one before re-running.
--   Measured on the prod rehearsal snapshot (2026-09-19): 0 conflicts.
--
-- ORDERING (why a ledger row is never left orphaned from its conversion):
-- every ledger-ish column (sales/purchase_transactions, the four express_*
-- tables, credit_note_imports, pending_product_suggestions) is translated
-- BEFORE the unit_conversions merge/rename runs below, in this SAME
-- transaction. So by the time a code-row conversion is deleted as a twin,
-- no remaining ledger row can still hold that raw code (excluding
-- กร/ถง/บล, whose conversions this migration also never touches) --
-- structurally, not by a runtime check.
--
-- pid 1393 (#595/#597 explicit check): a code-only 'ตว' conversion
-- (ratio 1.0) on a product whose unit_type is แผง. Checked against its
-- bills before trusting the rename (prod, 2026-09-19): its 3 purchase
-- lines (HP6700065, HP6700092 = IN 2880 each; GR6700018 = a ซื้อ-คืน
-- return, OUT 1440) were all costed at unit_price 12.5 -- which matches
-- products.cost_price (12.5) for pid 1393 EXACTLY, and sale lines in the
-- product's own unit (แผง) go out at 64.49-80.00, a normal ~5.6x hardware
-- margin. ratio 1.0 is corroborated by an independent oracle (cost_price)
-- and is safe to keep; this migration renames 'ตว' -> 'ตัว' on that row
-- like any other rename, changing no ratio.
--
-- SAFETY NET: after every translation, a postcondition sweep (same shape as
-- the precondition) re-derives "does any covered column still hold a
-- translated code" and ABORTs the whole migration if so -- catching a typo
-- in one of the WHERE clauses above rather than shipping a partial cleanup.
--
-- RE-RUNNABLE: the runner never re-applies a stamped migration; this file
-- is not designed to be re-run by hand against a DB that already ran it
-- (its own UPDATEs would then be no-ops matching nothing, which is safe,
-- but CREATE TABLE migration_186_snapshot would fail -- that is intentional,
-- matching mig 184's "second run" note: start a hand re-run from a DB that
-- does not already have the migration_186_* tables).

PRAGMA busy_timeout = 10000;

BEGIN;

-- ── Embedded translation maps ──────────────────────────────────────────────
-- _mig186_full_map: every Express BSN5657 short code whose meaning does NOT
-- change, from data/reference/bsn_unit_full.json (identity pairs dropped;
-- กร/ถง/บล dropped; the five "!" entries dropped) PLUS #595's approved
-- additions not already in that JSON.
CREATE TEMP TABLE _mig186_full_map (code TEXT PRIMARY KEY, word TEXT NOT NULL);
INSERT INTO _mig186_full_map (code, word) VALUES
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
    ('ชน', 'ชิ้น'),
    -- #595 approved additions (Sendy spelling variants + Express codes not
    -- already in bsn_unit_full.json)
    ('กก.', 'กิโลกรัม'),
    ('กิโล', 'กิโลกรัม'),
    ('1กิโล', 'กิโลกรัม'),
    ('กล.เล็ก', 'กล่องเล็ก'),
    ('คค', 'ครั้ง'),
    ('ช5', 'ชุด5'),
    ('ช3', 'ชุด3'),
    ('ขว', 'ขวด'),
    ('คร', 'เครื่อง'),
    ('ดม', 'ด้าม'),
    ('มด', 'เม็ด'),
    ('เม', 'เมตร'),
    ('ตล', 'ตลับ'),
    ('ปป', 'ปิ๊ป'),
    ('ทน', 'แท่น'),
    ('หบ', 'หีบ'),
    ('บา', 'บาน'),
    ('เก', 'เกล็ด'),
    ('คล', 'ครึ่งโล'),
    ('ดว', 'ดวง');
    -- 'ใบ' is NOT listed: the Express code equals the word (identity).
    -- กร / ถง / บล are NOT listed: excluded per the header above.

-- _mig186_supplier_map: the SAFE subset for supplier-typed data (see the
-- ⚠ finding in the header — never give supplier_catalogue_items /
-- supplier_catalogue_price_history / supplier_product_mapping.supplier_unit
-- the single/double-letter Express code space).
CREATE TEMP TABLE _mig186_supplier_map (code TEXT PRIMARY KEY, word TEXT NOT NULL);
INSERT INTO _mig186_supplier_map (code, word) VALUES
    ('กก', 'กิโลกรัม'),
    ('กก.', 'กิโลกรัม'),
    ('กิโล', 'กิโลกรัม'),
    ('1กิโล', 'กิโลกรัม'),
    ('แพ', 'แพ็ค'),
    ('แพค', 'แพ็ค'),
    ('กล.เล็ก', 'กล่องเล็ก');

-- ── Snapshot tables (forensic record + exact rollback source) ─────────────
CREATE TABLE migration_186_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    row_id      INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);
CREATE INDEX idx_migration_186_snapshot_lookup
    ON migration_186_snapshot(table_name, row_id, column_name);

CREATE TABLE migration_186_uc_deleted (
    id          INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    bsn_unit    TEXT NOT NULL,
    ratio       REAL NOT NULL,
    created_at  TEXT NOT NULL
);

-- ── D1 precondition: a disagreeing code/word twin -> ABORT before anything ─
DROP TABLE IF EXISTS temp._mig186_precheck;
CREATE TEMP TABLE _mig186_precheck AS
SELECT c.id
  FROM unit_conversions c
  JOIN _mig186_full_map m ON m.code = c.bsn_unit
  JOIN unit_conversions w ON w.product_id = c.product_id AND w.bsn_unit = m.word
 WHERE w.ratio IS NOT c.ratio;

CREATE TEMP TRIGGER _mig186_precondition_guard
BEFORE DELETE ON _mig186_precheck
BEGIN
  SELECT RAISE(ABORT,
    'mig 186 precondition FAILED: a product holds a code conversion and its word conversion at different ratios. See the RECOVERY query in this migration''s header comment.');
END;
DELETE FROM _mig186_precheck;
DROP TRIGGER _mig186_precondition_guard;
DROP TABLE _mig186_precheck;

-- ── 1. sales_transactions.unit (mig-173 declared-change path) ─────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'sales_transactions', id, 'unit', unit,
       (SELECT word FROM _mig186_full_map WHERE code = sales_transactions.unit)
  FROM sales_transactions
 WHERE unit IN (SELECT code FROM _mig186_full_map);

UPDATE sales_transactions
   SET unit          = (SELECT word FROM _mig186_full_map WHERE code = sales_transactions.unit),
       change_source = 'import',
       change_actor  = 'mig186-unit-cleanup',
       change_token  = 'mig186-unit-' || id
 WHERE unit IN (SELECT code FROM _mig186_full_map);

-- ── 2. purchase_transactions.unit (mig-173 declared-change path) ──────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'purchase_transactions', id, 'unit', unit,
       (SELECT word FROM _mig186_full_map WHERE code = purchase_transactions.unit)
  FROM purchase_transactions
 WHERE unit IN (SELECT code FROM _mig186_full_map);

UPDATE purchase_transactions
   SET unit          = (SELECT word FROM _mig186_full_map WHERE code = purchase_transactions.unit),
       change_source = 'import',
       change_actor  = 'mig186-unit-cleanup',
       change_token  = 'mig186-unit-' || id
 WHERE unit IN (SELECT code FROM _mig186_full_map);

-- ── 3. products.unit_type ──────────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'products', id, 'unit_type', unit_type,
       (SELECT word FROM _mig186_full_map WHERE code = products.unit_type)
  FROM products
 WHERE unit_type IN (SELECT code FROM _mig186_full_map);

UPDATE products
   SET unit_type = (SELECT word FROM _mig186_full_map WHERE code = products.unit_type)
 WHERE unit_type IN (SELECT code FROM _mig186_full_map);

-- ── 4. promotions.bundle_unit ──────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'promotions', id, 'bundle_unit', bundle_unit,
       (SELECT word FROM _mig186_full_map WHERE code = promotions.bundle_unit)
  FROM promotions
 WHERE bundle_unit IN (SELECT code FROM _mig186_full_map);

UPDATE promotions
   SET bundle_unit = (SELECT word FROM _mig186_full_map WHERE code = promotions.bundle_unit)
 WHERE bundle_unit IN (SELECT code FROM _mig186_full_map);

-- ── 5. product_code_mapping.bsn_unit — UNIQUE(bsn_code, bsn_unit): skip a
--      translation that would collide with an existing (bsn_code, word) row
--      rather than violate the constraint (0 collisions on prod today; the
--      postcondition sweep below would ABORT the whole migration if this
--      guard ever left a code untranslated, so a collision cannot ship
--      silently) ───────────────────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'product_code_mapping', p.id, 'bsn_unit', p.bsn_unit,
       (SELECT word FROM _mig186_full_map WHERE code = p.bsn_unit)
  FROM product_code_mapping p
 WHERE p.bsn_unit IN (SELECT code FROM _mig186_full_map)
   AND NOT EXISTS (
       SELECT 1 FROM product_code_mapping p2
        WHERE p2.bsn_code = p.bsn_code
          AND p2.bsn_unit = (SELECT word FROM _mig186_full_map WHERE code = p.bsn_unit)
          AND p2.id <> p.id
   );

UPDATE product_code_mapping
   SET bsn_unit = (SELECT word FROM _mig186_full_map WHERE code = product_code_mapping.bsn_unit)
 WHERE bsn_unit IN (SELECT code FROM _mig186_full_map)
   AND NOT EXISTS (
       SELECT 1 FROM product_code_mapping p2
        WHERE p2.bsn_code = product_code_mapping.bsn_code
          AND p2.bsn_unit = (SELECT word FROM _mig186_full_map WHERE code = product_code_mapping.bsn_unit)
          AND p2.id <> product_code_mapping.id
   );

-- ── 6. pending_product_suggestions.bsn_unit + .suggested_unit_type ────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'pending_product_suggestions', id, 'bsn_unit', bsn_unit,
       (SELECT word FROM _mig186_full_map WHERE code = pending_product_suggestions.bsn_unit)
  FROM pending_product_suggestions
 WHERE bsn_unit IN (SELECT code FROM _mig186_full_map);

UPDATE pending_product_suggestions
   SET bsn_unit = (SELECT word FROM _mig186_full_map WHERE code = pending_product_suggestions.bsn_unit)
 WHERE bsn_unit IN (SELECT code FROM _mig186_full_map);

INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'pending_product_suggestions', id, 'suggested_unit_type', suggested_unit_type,
       (SELECT word FROM _mig186_full_map WHERE code = pending_product_suggestions.suggested_unit_type)
  FROM pending_product_suggestions
 WHERE suggested_unit_type IN (SELECT code FROM _mig186_full_map);

UPDATE pending_product_suggestions
   SET suggested_unit_type = (SELECT word FROM _mig186_full_map WHERE code = pending_product_suggestions.suggested_unit_type)
 WHERE suggested_unit_type IN (SELECT code FROM _mig186_full_map);

-- ── 7. credit_note_imports.unit ────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'credit_note_imports', id, 'unit', unit,
       (SELECT word FROM _mig186_full_map WHERE code = credit_note_imports.unit)
  FROM credit_note_imports
 WHERE unit IN (SELECT code FROM _mig186_full_map);

UPDATE credit_note_imports
   SET unit = (SELECT word FROM _mig186_full_map WHERE code = credit_note_imports.unit)
 WHERE unit IN (SELECT code FROM _mig186_full_map);

-- ── 8. express_credit_note_lines.unit ─────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'express_credit_note_lines', id, 'unit', unit,
       (SELECT word FROM _mig186_full_map WHERE code = express_credit_note_lines.unit)
  FROM express_credit_note_lines
 WHERE unit IN (SELECT code FROM _mig186_full_map);

UPDATE express_credit_note_lines
   SET unit = (SELECT word FROM _mig186_full_map WHERE code = express_credit_note_lines.unit)
 WHERE unit IN (SELECT code FROM _mig186_full_map);

-- ── 9. express_sales.unit ──────────────────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'express_sales', id, 'unit', unit,
       (SELECT word FROM _mig186_full_map WHERE code = express_sales.unit)
  FROM express_sales
 WHERE unit IN (SELECT code FROM _mig186_full_map);

UPDATE express_sales
   SET unit = (SELECT word FROM _mig186_full_map WHERE code = express_sales.unit)
 WHERE unit IN (SELECT code FROM _mig186_full_map);

-- ── 10. express_sales_order_lines.unit ────────────────────────────────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'express_sales_order_lines', id, 'unit', unit,
       (SELECT word FROM _mig186_full_map WHERE code = express_sales_order_lines.unit)
  FROM express_sales_order_lines
 WHERE unit IN (SELECT code FROM _mig186_full_map);

UPDATE express_sales_order_lines
   SET unit = (SELECT word FROM _mig186_full_map WHERE code = express_sales_order_lines.unit)
 WHERE unit IN (SELECT code FROM _mig186_full_map);

-- ── 11. supplier_catalogue_items.unit (RESTRICTED supplier map) ──────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_catalogue_items', id, 'unit', unit,
       (SELECT word FROM _mig186_supplier_map WHERE code = supplier_catalogue_items.unit)
  FROM supplier_catalogue_items
 WHERE unit IN (SELECT code FROM _mig186_supplier_map);

UPDATE supplier_catalogue_items
   SET unit = (SELECT word FROM _mig186_supplier_map WHERE code = supplier_catalogue_items.unit)
 WHERE unit IN (SELECT code FROM _mig186_supplier_map);

-- ── 12. supplier_catalogue_price_history.unit (RESTRICTED supplier map) ──
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_catalogue_price_history', id, 'unit', unit,
       (SELECT word FROM _mig186_supplier_map WHERE code = supplier_catalogue_price_history.unit)
  FROM supplier_catalogue_price_history
 WHERE unit IN (SELECT code FROM _mig186_supplier_map);

UPDATE supplier_catalogue_price_history
   SET unit = (SELECT word FROM _mig186_supplier_map WHERE code = supplier_catalogue_price_history.unit)
 WHERE unit IN (SELECT code FROM _mig186_supplier_map);

-- ── 13. supplier_product_mapping — erp_unit (full map, Sendy's own side) +
--       supplier_unit (RESTRICTED, the supplier's own spelling) ──────────
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_product_mapping', id, 'erp_unit', erp_unit,
       (SELECT word FROM _mig186_full_map WHERE code = supplier_product_mapping.erp_unit)
  FROM supplier_product_mapping
 WHERE erp_unit IN (SELECT code FROM _mig186_full_map);

UPDATE supplier_product_mapping
   SET erp_unit = (SELECT word FROM _mig186_full_map WHERE code = supplier_product_mapping.erp_unit)
 WHERE erp_unit IN (SELECT code FROM _mig186_full_map);

INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_product_mapping', id, 'supplier_unit', supplier_unit,
       (SELECT word FROM _mig186_supplier_map WHERE code = supplier_product_mapping.supplier_unit)
  FROM supplier_product_mapping
 WHERE supplier_unit IN (SELECT code FROM _mig186_supplier_map);

UPDATE supplier_product_mapping
   SET supplier_unit = (SELECT word FROM _mig186_supplier_map WHERE code = supplier_product_mapping.supplier_unit)
 WHERE supplier_unit IN (SELECT code FROM _mig186_supplier_map);

-- ── 14. product_price_tiers.qty_label — keep the leading count + its exact
--       spacing (digits, then any run of spaces), translate only the tail ─
INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'product_price_tiers', id, 'qty_label', qty_label,
       substr(qty_label, 1, length(qty_label) - length(ltrim(qty_label, '0123456789 ')))
       || (SELECT word FROM _mig186_full_map
            WHERE code = ltrim(qty_label, '0123456789 '))
  FROM product_price_tiers
 WHERE (CASE WHEN qty_label GLOB '[0-9]*'
             THEN ltrim(qty_label, '0123456789 ')
             ELSE qty_label END) IN (SELECT code FROM _mig186_full_map);

UPDATE product_price_tiers
   SET qty_label =
       substr(qty_label, 1, length(qty_label) - length(ltrim(qty_label, '0123456789 ')))
       || (SELECT word FROM _mig186_full_map
            WHERE code = ltrim(qty_label, '0123456789 '))
 WHERE (CASE WHEN qty_label GLOB '[0-9]*'
             THEN ltrim(qty_label, '0123456789 ')
             ELSE qty_label END) IN (SELECT code FROM _mig186_full_map);

-- ── 15. unit_conversions — merge same-ratio twins (delete the code row),
--       then rename the remaining code-only rows to the word ────────────
INSERT INTO migration_186_uc_deleted (id, product_id, bsn_unit, ratio, created_at)
SELECT c.id, c.product_id, c.bsn_unit, c.ratio, c.created_at
  FROM unit_conversions c
  JOIN _mig186_full_map m ON m.code = c.bsn_unit
  JOIN unit_conversions w ON w.product_id = c.product_id AND w.bsn_unit = m.word
 WHERE w.ratio IS c.ratio;

DELETE FROM unit_conversions
 WHERE id IN (SELECT id FROM migration_186_uc_deleted);

INSERT INTO migration_186_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'unit_conversions', c.id, 'bsn_unit', c.bsn_unit, m.word
  FROM unit_conversions c
  JOIN _mig186_full_map m ON m.code = c.bsn_unit
 WHERE NOT EXISTS (
       SELECT 1 FROM unit_conversions w
        WHERE w.product_id = c.product_id AND w.bsn_unit = m.word
 );

UPDATE unit_conversions
   SET bsn_unit = (SELECT word FROM _mig186_full_map WHERE code = unit_conversions.bsn_unit)
 WHERE bsn_unit IN (SELECT code FROM _mig186_full_map)
   AND NOT EXISTS (
       SELECT 1 FROM unit_conversions w
        WHERE w.product_id = unit_conversions.product_id
          AND w.bsn_unit = (SELECT word FROM _mig186_full_map WHERE code = unit_conversions.bsn_unit)
          AND w.id <> unit_conversions.id
   );

-- ── D2 postcondition: any covered column still holding a translated code
--    (any WHERE clause above missed a row) -> ABORT the whole migration ──
DROP TABLE IF EXISTS temp._mig186_postcheck;
CREATE TEMP TABLE _mig186_postcheck AS
SELECT 'sales_transactions' AS src, id FROM sales_transactions WHERE unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'purchase_transactions', id FROM purchase_transactions WHERE unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'products', id FROM products WHERE unit_type IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'promotions', id FROM promotions WHERE bundle_unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'product_code_mapping', id FROM product_code_mapping WHERE bsn_unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'pending_product_suggestions.bsn_unit', id FROM pending_product_suggestions WHERE bsn_unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'pending_product_suggestions.suggested_unit_type', id FROM pending_product_suggestions WHERE suggested_unit_type IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'credit_note_imports', id FROM credit_note_imports WHERE unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'express_credit_note_lines', id FROM express_credit_note_lines WHERE unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'express_sales', id FROM express_sales WHERE unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'express_sales_order_lines', id FROM express_sales_order_lines WHERE unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'unit_conversions', id FROM unit_conversions WHERE bsn_unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'supplier_product_mapping.erp_unit', id FROM supplier_product_mapping WHERE erp_unit IN (SELECT code FROM _mig186_full_map)
UNION ALL SELECT 'supplier_catalogue_items', id FROM supplier_catalogue_items WHERE unit IN (SELECT code FROM _mig186_supplier_map)
UNION ALL SELECT 'supplier_catalogue_price_history', id FROM supplier_catalogue_price_history WHERE unit IN (SELECT code FROM _mig186_supplier_map)
UNION ALL SELECT 'supplier_product_mapping.supplier_unit', id FROM supplier_product_mapping WHERE supplier_unit IN (SELECT code FROM _mig186_supplier_map)
UNION ALL SELECT 'product_price_tiers', id FROM product_price_tiers WHERE
    (CASE WHEN qty_label GLOB '[0-9]*' THEN ltrim(qty_label, '0123456789 ') ELSE qty_label END)
    IN (SELECT code FROM _mig186_full_map);

CREATE TEMP TRIGGER _mig186_postcondition_guard
BEFORE DELETE ON _mig186_postcheck
BEGIN
  SELECT RAISE(ABORT,
    'mig 186 postcondition FAILED: a covered column still holds a code this migration was meant to translate. See _mig186_postcheck''s construction in this migration for which column/table.');
END;
DELETE FROM _mig186_postcheck;
DROP TRIGGER _mig186_postcondition_guard;
DROP TABLE _mig186_postcheck;

COMMIT;
