-- 193 — GH #610 (#595 · 7/7): the unit map learns the approved vocabulary, and
-- every stored unit the importers now translate is translated in the SAME
-- change, conversions moving with their rows. Label-only: no quantity, ratio,
-- synced_to_stock, ledger or cost row changes. Apply: restart the app (the
-- runner in database.py::init_db() picks it up). Rollback:
-- 193_unit_vocabulary.rollback.sql.
--
-- ⛔ RUN ONLY through the migration runner (Python executescript, which rolls
-- back on the first error) or `sqlite3 -bail`. The plain sqlite3 CLI keeps
-- going after a failed statement, so every RAISE(ABORT) guard below would fail
-- OPEN: the abort cancels its own statement, the rest of the file runs, and
-- COMMIT keeps it.
--
-- THE VOCABULARY (#595's approved word list; each Express code checked against
-- the ISTAB (TABTYP '20') of the book it is seeded under, 2026-09-22):
--   BSN5657  ช5 ชุด5 · ช3 ชุด3 · คค ครั้ง · ใบ ใบ · ขว ขวด · คร เครื่อง · ดม ด้าม
--            มด เม็ด · เม เมตร · ตล ตลับ · ปป ปิ๊ป · ทน แท่น · หบ หีบ · บา บาน
--            เก เกล็ด · คล ครึ่งโล (separate from กิโลกรัม)
--   xp5      ขว ขวด · คร เครื่อง · ตล ตลับ · ทน แท่น · ใบ ใบ
--   '*'      กิโล / กก. / 1กิโล กิโลกรัม · กล.เล็ก กล่องเล็ก (a unit separate from
--            กล่อง) · แพค แพ็ค — Sendy spellings, not Express codes, so they
--            apply to every source, supplier price lists included.
-- After it every code in both books' unit lists translates (49 + 34).
-- REMOVED: the five `!` rows (!กล !คู !ลก !หด !หล, BSN5657), per #595's approved
-- list: `!` is a warning mark Express prints in text reports, not part of a
-- unit, and the parsers strip it. Their exact rows go to
-- migration_193_unit_map_removed for the rollback. They match no stored row
-- on prod; if one ever does, the bang_in_use precondition refuses (dropping
-- the map row would make the next import rewrite that line, the #609 shape).
-- BOTH BOOKS SEEDED TOGETHER: every spelling added here means the same word in
-- BSN5657 and xp5, so bsn_line._unit_same and bsn_sync's conversion-role
-- check, which normalise with the default book even inside the VAT-book import,
-- give the same answer as xp5 would (pinned by the tests). หอ/ดว are #601's.
--
-- THE RULE (erp-engineering-discipline.md, the #609 blocker): a stored unit is
-- only ever translated to exactly what bsn_units.normalize_unit produces for it
-- in the book its column is read against. Everything below translates through
-- ONE map, _mig193_map, built from unit_map itself AFTER the seeds land, with
-- translate()'s own precedence (a book row wins, a '*' row is the fallback):
--   source 'BSN5657'  every column the main DB's importers and forms write:
--                     sales/purchase lines, products.unit_type,
--                     promotions.bundle_unit, product_code_mapping.bsn_unit,
--                     pending_product_suggestions.{bsn_unit,suggested_unit_type},
--                     credit_note_imports.unit, express_sales.unit,
--                     product_price_tiers.qty_label (unit part only),
--                     unit_conversions.bsn_unit, and the two verbatim copies
--                     186 had to skip: express_sales_order_lines and
--                     express_credit_note_lines (their writers translate as of
--                     this PR, so the next upload writes the same word).
--   source '*'        supplier_catalogue_items.unit and
--                     supplier_catalogue_price_history.unit: Sendy spellings
--                     only, never an Express code (64 prod rows of ขด mean a coil
--                     of rope, not ขีด).
-- tests/test_migration_193_unit_vocabulary.py pins _mig193_map EQUAL, both
-- directions, to what bsn_units' public API produces.
--
-- BILL LINES ARE TRANSLATED ONLY WHEN WHAT THEY RESOLVE TO IS UNCHANGED. A
-- line's ratio (bsn_sync._get_base_qty: 1 when the unit is the product's own,
-- else its conversion, else nothing) is captured before anything moves. The
-- COGS ratio (sales_filters.base_qty_sql) is the same value with 1 for
-- nothing, so an unchanged ratio leaves COGS unchanged too. A line whose word
-- would resolve differently keeps its spelling and is listed in
-- migration_193_skipped. The prod case: pid 436's two
-- UNSYNCED ช3 lines have no ช3 conversion, but the product has a ชุด3 one at 3,
-- so as ชุด3 they would sync on the next import (stock −6) and cost at 3. They
-- stay ช3 until Put names a ratio on /unit-conversions; a re-import leaves them
-- alone (bsn_line._unit_same normalises the stored side). A postcondition then
-- re-reads every line and ABORTS if any resolves differently.
--
-- CONVERSION RULES (unit_conversions, UNIQUE(product_id, bsn_unit)), as 186:
--   spelling row + word row at the same ratio        -> delete the spelling row
--   two spellings for one word, no word row, same
--     ratio (กิโล + กก.)                              -> keep the lower id, delete the other
--   spelling row, no word row                         -> rename it to the word
--   Every deleted row is snapshotted in migration_193_uc_deleted.
-- ⛔ EXCEPT the กร / ถง / บล keys, which stay exactly as they are
--   (_mig193_uc_map leaves them out). After #600 no bill line reads them and
--   each agrees with its กุรุส / ถัง / บล็อก twin, so they are harmless; #603's
--   hasp plan pins 1187/1188 at {'กร': 1.0, 'ตัว': 1.0} and would refuse if they
--   merged. Their clean-up is a follow-up AFTER that rebase lands (lead's
--   ruling, 2026-09-22).
--
-- PRECONDITIONS (the migration-177 shape: a temp table of violators and a
-- BEFORE DELETE trigger that RAISEs; the runner rolls back and does not stamp
-- applied_migrations, so the DB is untouched). Every list is empty on the
-- 2026-09-22 prod snapshot. scripts/preflight_193_unit_vocabulary.py prints the
-- violating rows of a real DB. RECOVERY per guard:
--   bang_in_use     a stored unit (bill line, conversion, product, tier, mapping,
--                   suggestion, Express copy) still reads one of the five `!`
--                   codes. Translate it to the word after the `!` (the code
--                   itself, e.g. !หล -> โหล) through the declared-change path,
--                   moving its conversion with it, then re-run.
--   seed_conflict   unit_map already names a spelling this migration seeds,
--                   with a DIFFERENT word (Put named it on /unit-conversions).
--                   Decide which word is right; if it is the approved one,
--                   UPDATE unit_map to it; if not, the vocabulary needs Put's
--                   ruling before 193 can ship.
--   twin_ratio      a spelling row and its word row disagree on the ratio.
--                   Read the product's bills, fix the wrong ratio, delete the
--                   spelling row.
--   codes_ratio     two spellings for one word disagree (กิโล vs กก.). Same.
--   pcm_collision   product_code_mapping holds (bsn_code, spelling) next to a
--                   row that becomes the same (bsn_code, word). Decide which
--                   product the unit maps to, delete the other row.
--   tier_collision  a tier label becomes a label the product already has.
--                   Keep one price, delete the other tier.
--   unit_type_ratio a product's unit_type and one of its conversions end up as
--                   the same word with a ratio other than 1: a line in that
--                   unit would short-circuit to 1 (_get_base_qty), so the next
--                   rebuild would post a different quantity. Fix the ratio or
--                   the unit_type.
-- POSTCONDITIONS (abort, same effect): line_resolution (above) and
-- still_a_spelling (a covered column still holds a spelling the map translates,
-- outside the listed skips — an UPDATE that did not land).
--
-- ⚠ Sequencing: must not ship in the same deploy as #603's rebase (1187/1188),
-- #586's data change, or a #600 run: each pins the units it expects to find.

PRAGMA busy_timeout = 10000;

BEGIN;

-- >>> mig193 seeds
DROP TABLE IF EXISTS temp._mig193_seed;
CREATE TEMP TABLE _mig193_seed (
    book     TEXT NOT NULL,
    spelling TEXT NOT NULL,
    word     TEXT NOT NULL,
    PRIMARY KEY (book, spelling)
);
INSERT INTO _mig193_seed (book, spelling, word) VALUES
    ('BSN5657', 'ช5',   'ชุด5'),
    ('BSN5657', 'ช3',   'ชุด3'),
    ('BSN5657', 'คค',   'ครั้ง'),
    ('BSN5657', 'ใบ',   'ใบ'),
    ('BSN5657', 'ขว',   'ขวด'),
    ('BSN5657', 'คร',   'เครื่อง'),
    ('BSN5657', 'ดม',   'ด้าม'),
    ('BSN5657', 'มด',   'เม็ด'),
    ('BSN5657', 'เม',   'เมตร'),
    ('BSN5657', 'ตล',   'ตลับ'),
    ('BSN5657', 'ปป',   'ปิ๊ป'),
    ('BSN5657', 'ทน',   'แท่น'),
    ('BSN5657', 'หบ',   'หีบ'),
    ('BSN5657', 'บา',   'บาน'),
    ('BSN5657', 'เก',   'เกล็ด'),
    ('BSN5657', 'คล',   'ครึ่งโล'),
    ('xp5',     'ขว',   'ขวด'),
    ('xp5',     'คร',   'เครื่อง'),
    ('xp5',     'ตล',   'ตลับ'),
    ('xp5',     'ทน',   'แท่น'),
    ('xp5',     'ใบ',   'ใบ'),
    ('*',       'กิโล',  'กิโลกรัม'),
    ('*',       'กก.',  'กิโลกรัม'),
    ('*',       '1กิโล', 'กิโลกรัม'),
    ('*',       'กล.เล็ก', 'กล่องเล็ก'),
    ('*',       'แพค',  'แพ็ค');
-- <<< mig193 seeds

-- ── Snapshot tables: forensic record + the rollback's source. IF NOT EXISTS,
--    never drop-first: a hand re-run must not erase what the rollback needs.
CREATE TABLE IF NOT EXISTS migration_193_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    row_id      INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);
CREATE INDEX IF NOT EXISTS idx_migration_193_snapshot_lookup
    ON migration_193_snapshot(table_name, row_id, column_name);

CREATE TABLE IF NOT EXISTS migration_193_uc_deleted (
    id          INTEGER NOT NULL,
    product_id  INTEGER,
    bsn_unit    TEXT,
    ratio       REAL,
    created_at  TEXT
);

-- The seeds this run inserted (not the ones already present): the rollback
-- deletes exactly these.
CREATE TABLE IF NOT EXISTS migration_193_unit_map_added (
    book        TEXT NOT NULL,
    spelling    TEXT NOT NULL,
    word        TEXT NOT NULL
);

-- The `!` rows this run removed, verbatim (the rollback re-inserts them).
CREATE TABLE IF NOT EXISTS migration_193_unit_map_removed (
    id          INTEGER NOT NULL,
    book        TEXT NOT NULL,
    spelling    TEXT NOT NULL,
    word        TEXT NOT NULL,
    created_at  TEXT
);

-- Bill lines left untranslated because their word would resolve differently.
CREATE TABLE IF NOT EXISTS migration_193_skipped (
    table_name  TEXT NOT NULL,
    row_id      INTEGER NOT NULL,
    product_id  INTEGER,
    unit        TEXT,
    word        TEXT,
    detail      TEXT
);

-- >>> mig193 seed precondition
DROP TABLE IF EXISTS temp._mig193_bang;
CREATE TEMP TABLE _mig193_bang (spelling TEXT PRIMARY KEY);
INSERT INTO _mig193_bang (spelling) VALUES ('!กล'), ('!คู'), ('!ลก'), ('!หด'), ('!หล');

DROP TABLE IF EXISTS temp._mig193_pre_bang_in_use;
CREATE TEMP TABLE _mig193_pre_bang_in_use AS
          SELECT 'sales_transactions' AS table_name, id AS row_id, unit AS value
            FROM sales_transactions WHERE unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'purchase_transactions', id, unit
            FROM purchase_transactions WHERE unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'unit_conversions', id, bsn_unit
            FROM unit_conversions WHERE bsn_unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'products', id, unit_type
            FROM products WHERE unit_type IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'promotions', id, bundle_unit
            FROM promotions WHERE bundle_unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'product_code_mapping', id, bsn_unit
            FROM product_code_mapping WHERE bsn_unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'pending_product_suggestions', id, bsn_unit
            FROM pending_product_suggestions WHERE bsn_unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'pending_product_suggestions', id, suggested_unit_type
            FROM pending_product_suggestions WHERE suggested_unit_type IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'credit_note_imports', id, unit
            FROM credit_note_imports WHERE unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'express_sales', id, unit
            FROM express_sales WHERE unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'express_sales_order_lines', id, unit
            FROM express_sales_order_lines WHERE unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'express_credit_note_lines', id, unit
            FROM express_credit_note_lines WHERE unit IN (SELECT spelling FROM _mig193_bang)
UNION ALL SELECT 'product_price_tiers', id, qty_label
            FROM product_price_tiers
           WHERE trim(ltrim(qty_label, '0123456789 '), ' ') IN (SELECT spelling FROM _mig193_bang);

DROP TABLE IF EXISTS temp._mig193_pre_seed_conflict;
CREATE TEMP TABLE _mig193_pre_seed_conflict AS
SELECT u.id AS map_id
  FROM unit_map u
  JOIN _mig193_seed s ON s.book = u.book AND s.spelling = u.spelling
 WHERE u.word IS NOT s.word;
-- <<< mig193 seed precondition

CREATE TEMP TRIGGER _mig193_guard_bang_in_use BEFORE DELETE ON _mig193_pre_bang_in_use
BEGIN SELECT RAISE(ABORT, 'mig 193 precondition FAILED: a stored unit still holds a ! code this migration removes from the map (bang_in_use). RECOVERY in the migration header.'); END;
DELETE FROM _mig193_pre_bang_in_use;
DROP TABLE _mig193_pre_bang_in_use;

CREATE TEMP TRIGGER _mig193_guard_seed_conflict BEFORE DELETE ON _mig193_pre_seed_conflict
BEGIN SELECT RAISE(ABORT, 'mig 193 precondition FAILED: the unit map already names a spelling 193 seeds, with a different word (seed_conflict). RECOVERY in the migration header.'); END;
DELETE FROM _mig193_pre_seed_conflict;
DROP TABLE _mig193_pre_seed_conflict;

-- ── 1. the map learns the vocabulary ───────────────────────────────────────
-- >>> mig193 learn
INSERT INTO migration_193_unit_map_added (book, spelling, word)
SELECT s.book, s.spelling, s.word
  FROM _mig193_seed s
 WHERE NOT EXISTS (SELECT 1 FROM unit_map u WHERE u.book = s.book AND u.spelling = s.spelling);

INSERT INTO unit_map (book, spelling, word)
SELECT s.book, s.spelling, s.word
  FROM _mig193_seed s
 WHERE NOT EXISTS (SELECT 1 FROM unit_map u WHERE u.book = s.book AND u.spelling = s.spelling);

-- ... and forgets the five `!` rows (kept verbatim for the rollback)
INSERT INTO migration_193_unit_map_removed (id, book, spelling, word, created_at)
SELECT u.id, u.book, u.spelling, u.word, u.created_at
  FROM unit_map u
 WHERE u.spelling IN (SELECT spelling FROM _mig193_bang)
   AND u.id NOT IN (SELECT id FROM migration_193_unit_map_removed);
DELETE FROM unit_map WHERE spelling IN (SELECT spelling FROM _mig193_bang);
-- <<< mig193 learn

-- >>> mig193 map
-- The runtime map, resolved the way bsn_units.translate() resolves it, for the
-- two sources this migration writes: 'BSN5657' (a book row wins, a '*' row is
-- the fallback) and '*' alone. Only spellings whose word differs.
DROP TABLE IF EXISTS temp._mig193_map;
CREATE TEMP TABLE _mig193_map (
    source   TEXT NOT NULL,
    spelling TEXT NOT NULL,
    word     TEXT NOT NULL,
    PRIMARY KEY (source, spelling)
);
INSERT INTO _mig193_map (source, spelling, word)
SELECT 'BSN5657', s.spelling,
       COALESCE((SELECT b.word FROM unit_map b WHERE b.book = 'BSN5657' AND b.spelling = s.spelling),
                (SELECT a.word FROM unit_map a WHERE a.book = '*' AND a.spelling = s.spelling))
  FROM (SELECT spelling FROM unit_map WHERE book = 'BSN5657'
        UNION
        SELECT spelling FROM unit_map WHERE book = '*') s;
INSERT INTO _mig193_map (source, spelling, word)
SELECT '*', spelling, word FROM unit_map WHERE book = '*';
DELETE FROM _mig193_map WHERE word = spelling;
-- <<< mig193 map

-- >>> mig193 preconditions
-- The keys this migration moves in unit_conversions: the BSN5657 map minus
-- the three codes left for after #603 (see CONVERSION RULES).
DROP TABLE IF EXISTS temp._mig193_uc_map;
CREATE TEMP TABLE _mig193_uc_map (spelling TEXT PRIMARY KEY, word TEXT NOT NULL);
INSERT INTO _mig193_uc_map (spelling, word)
SELECT spelling, word FROM _mig193_map
 WHERE source = 'BSN5657' AND spelling NOT IN ('กร', 'ถง', 'บล');

-- Tier labels, split the way bsn_units.split_tier_label splits them: a leading
-- run of digits plus the spaces after it is the count (kept byte for byte), the
-- rest, trimmed, is the unit. No leading digit means no count.
DROP TABLE IF EXISTS temp._mig193_tier;
CREATE TEMP TABLE _mig193_tier AS
WITH d AS (
    SELECT id, product_id, qty_label,
           length(qty_label) - length(ltrim(qty_label, '0123456789')) AS n
      FROM product_price_tiers
), p AS (
    SELECT id, product_id, qty_label,
           CASE WHEN n > 0
                THEN substr(qty_label, 1, n + length(substr(qty_label, n + 1))
                                            - length(ltrim(substr(qty_label, n + 1), ' ')))
                ELSE '' END AS prefix
      FROM d
)
SELECT p.id, p.product_id, p.qty_label AS old_label, p.prefix || m.word AS new_label
  FROM p
  JOIN _mig193_map m ON m.source = 'BSN5657'
                    AND m.spelling = trim(substr(p.qty_label, length(p.prefix) + 1), ' ');

DROP TABLE IF EXISTS temp._mig193_pcm;
CREATE TEMP TABLE _mig193_pcm AS
SELECT p.id, p.bsn_code, p.bsn_unit AS old_unit, m.word AS new_unit
  FROM product_code_mapping p
  JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = p.bsn_unit;

DROP TABLE IF EXISTS temp._mig193_pre_twin_ratio;
CREATE TEMP TABLE _mig193_pre_twin_ratio AS
SELECT c.id AS uc_id
  FROM unit_conversions c
  JOIN _mig193_uc_map m ON m.spelling = c.bsn_unit
  JOIN unit_conversions w ON w.product_id = c.product_id AND w.bsn_unit = m.word
 WHERE w.ratio IS NOT c.ratio;

DROP TABLE IF EXISTS temp._mig193_pre_codes_ratio;
CREATE TEMP TABLE _mig193_pre_codes_ratio AS
SELECT a.id AS uc_id
  FROM unit_conversions a
  JOIN _mig193_uc_map ma ON ma.spelling = a.bsn_unit
  JOIN unit_conversions b ON b.product_id = a.product_id AND b.id <> a.id
  JOIN _mig193_uc_map mb ON mb.spelling = b.bsn_unit AND mb.word = ma.word
 WHERE b.ratio IS NOT a.ratio;

DROP TABLE IF EXISTS temp._mig193_pre_pcm_collision;
CREATE TEMP TABLE _mig193_pre_pcm_collision AS
SELECT x.id AS pcm_id
  FROM _mig193_pcm x
 WHERE EXISTS (SELECT 1 FROM product_code_mapping q
                 LEFT JOIN _mig193_pcm y ON y.id = q.id
                WHERE q.bsn_code = x.bsn_code AND q.id <> x.id
                  AND COALESCE(y.new_unit, q.bsn_unit) = x.new_unit);

DROP TABLE IF EXISTS temp._mig193_pre_tier_collision;
CREATE TEMP TABLE _mig193_pre_tier_collision AS
SELECT x.id AS tier_id
  FROM _mig193_tier x
 WHERE EXISTS (SELECT 1 FROM product_price_tiers t
                 LEFT JOIN _mig193_tier y ON y.id = t.id
                WHERE t.product_id = x.product_id AND t.id <> x.id
                  AND COALESCE(y.new_label, t.qty_label) = x.new_label);

DROP TABLE IF EXISTS temp._mig193_pre_unit_type_ratio;
CREATE TEMP TABLE _mig193_pre_unit_type_ratio AS
SELECT c.id AS uc_id
  FROM unit_conversions c
  JOIN products p ON p.id = c.product_id
  LEFT JOIN _mig193_uc_map mc ON mc.spelling = c.bsn_unit
  LEFT JOIN _mig193_map mp ON mp.source = 'BSN5657' AND mp.spelling = p.unit_type
 WHERE (mc.spelling IS NOT NULL OR mp.spelling IS NOT NULL)
   AND COALESCE(mc.word, c.bsn_unit) = COALESCE(mp.word, p.unit_type)
   AND c.ratio IS NOT 1.0;
-- <<< mig193 preconditions

CREATE TEMP TRIGGER _mig193_guard_twin_ratio BEFORE DELETE ON _mig193_pre_twin_ratio
BEGIN SELECT RAISE(ABORT, 'mig 193 precondition FAILED: code and word conversion at different ratios on one product (twin_ratio). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig193_guard_codes_ratio BEFORE DELETE ON _mig193_pre_codes_ratio
BEGIN SELECT RAISE(ABORT, 'mig 193 precondition FAILED: two spellings for one word at different ratios on one product (codes_ratio). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig193_guard_pcm_collision BEFORE DELETE ON _mig193_pre_pcm_collision
BEGIN SELECT RAISE(ABORT, 'mig 193 precondition FAILED: product_code_mapping would hold one (bsn_code, word) twice (pcm_collision). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig193_guard_tier_collision BEFORE DELETE ON _mig193_pre_tier_collision
BEGIN SELECT RAISE(ABORT, 'mig 193 precondition FAILED: product_price_tiers would hold one (product_id, label) twice (tier_collision). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig193_guard_unit_type_ratio BEFORE DELETE ON _mig193_pre_unit_type_ratio
BEGIN SELECT RAISE(ABORT, 'mig 193 precondition FAILED: unit_type and a conversion become the same word at a ratio other than 1 (unit_type_ratio). RECOVERY in the migration header.'); END;

DELETE FROM _mig193_pre_twin_ratio;
DELETE FROM _mig193_pre_codes_ratio;
DELETE FROM _mig193_pre_pcm_collision;
DELETE FROM _mig193_pre_tier_collision;
DELETE FROM _mig193_pre_unit_type_ratio;
DROP TABLE _mig193_pre_twin_ratio;
DROP TABLE _mig193_pre_codes_ratio;
DROP TABLE _mig193_pre_pcm_collision;
DROP TABLE _mig193_pre_tier_collision;
DROP TABLE _mig193_pre_unit_type_ratio;

-- ── 2. what every bill line resolves to NOW, before anything moves ─────────
-- res = bsn_sync._get_base_qty's ratio (NULL = the line cannot sync). Its
-- short circuit compares str.strip()ped units; SQL's trim() takes an explicit
-- set, so the common whitespace is listed (space, tab, CR/LF, VT, FF, NBSP).
-- Rarer Unicode spaces are not: the preflight re-derives every line through
-- the Python function itself, which is the check that sees them.
DROP TABLE IF EXISTS temp._mig193_res_pre;
CREATE TEMP TABLE _mig193_res_pre AS
SELECT 'sales_transactions' AS table_name, t.id AS row_id, t.product_id, t.unit,
       CASE WHEN t.unit IS NOT NULL AND trim(t.unit, char(32, 9, 10, 11, 12, 13, 160)) = trim(COALESCE(p.unit_type, ''), char(32, 9, 10, 11, 12, 13, 160)) THEN 1.0
            ELSE (SELECT c.ratio FROM unit_conversions c
                   WHERE c.product_id = t.product_id AND c.bsn_unit = t.unit) END AS res
  FROM sales_transactions t LEFT JOIN products p ON p.id = t.product_id
UNION ALL
SELECT 'purchase_transactions', t.id, t.product_id, t.unit,
       CASE WHEN t.unit IS NOT NULL AND trim(t.unit, char(32, 9, 10, 11, 12, 13, 160)) = trim(COALESCE(p.unit_type, ''), char(32, 9, 10, 11, 12, 13, 160)) THEN 1.0
            ELSE (SELECT c.ratio FROM unit_conversions c
                   WHERE c.product_id = t.product_id AND c.bsn_unit = t.unit) END
  FROM purchase_transactions t LEFT JOIN products p ON p.id = t.product_id;
CREATE INDEX temp._mig193_res_pre_key ON _mig193_res_pre(table_name, row_id);

-- ── 3. products.unit_type ──────────────────────────────────────────────────
INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'products', p.id, 'unit_type', p.unit_type, m.word
  FROM products p JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = p.unit_type;

UPDATE products
   SET unit_type = (SELECT word FROM _mig193_map WHERE source = 'BSN5657' AND spelling = products.unit_type)
 WHERE unit_type IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

-- ── 4. unit_conversions ────────────────────────────────────────────────────
-- (a) a spelling row whose word row already exists (same ratio, checked above)
INSERT INTO migration_193_uc_deleted (id, product_id, bsn_unit, ratio, created_at)
SELECT c.id, c.product_id, c.bsn_unit, c.ratio, c.created_at
  FROM unit_conversions c
  JOIN _mig193_uc_map m ON m.spelling = c.bsn_unit
 WHERE EXISTS (SELECT 1 FROM unit_conversions w
                WHERE w.product_id = c.product_id AND w.bsn_unit = m.word);

-- (b) two spellings for one word and no word row: keep the lower id
INSERT INTO migration_193_uc_deleted (id, product_id, bsn_unit, ratio, created_at)
SELECT c.id, c.product_id, c.bsn_unit, c.ratio, c.created_at
  FROM unit_conversions c
  JOIN _mig193_uc_map m ON m.spelling = c.bsn_unit
 WHERE c.id NOT IN (SELECT id FROM migration_193_uc_deleted)
   AND EXISTS (SELECT 1 FROM unit_conversions o
                 JOIN _mig193_uc_map mo ON mo.spelling = o.bsn_unit
                WHERE o.product_id = c.product_id AND mo.word = m.word AND o.id < c.id);

DELETE FROM unit_conversions WHERE id IN (SELECT id FROM migration_193_uc_deleted);

-- (c) every spelling row left is the only one for its word: rename it
INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'unit_conversions', c.id, 'bsn_unit', c.bsn_unit, m.word
  FROM unit_conversions c JOIN _mig193_uc_map m ON m.spelling = c.bsn_unit;

UPDATE unit_conversions
   SET bsn_unit = (SELECT word FROM _mig193_uc_map WHERE spelling = unit_conversions.bsn_unit)
 WHERE bsn_unit IN (SELECT spelling FROM _mig193_uc_map);

-- ── 5. bill lines, only where the word resolves exactly as the spelling did
DROP TABLE IF EXISTS temp._mig193_line;
CREATE TEMP TABLE _mig193_line AS
SELECT r.table_name, r.row_id, r.product_id, r.unit AS old_unit, m.word AS new_unit, r.res,
       CASE WHEN trim(m.word, char(32, 9, 10, 11, 12, 13, 160)) = trim(COALESCE(p.unit_type, ''), char(32, 9, 10, 11, 12, 13, 160)) THEN 1.0
            ELSE (SELECT c.ratio FROM unit_conversions c
                   WHERE c.product_id = r.product_id AND c.bsn_unit = m.word) END AS res_new
  FROM _mig193_res_pre r
  JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = r.unit
  LEFT JOIN products p ON p.id = r.product_id;

-- >>> mig193 skip
INSERT INTO migration_193_skipped (table_name, row_id, product_id, unit, word, detail)
SELECT l.table_name, l.row_id, l.product_id, l.old_unit, l.new_unit,
       'resolves to ' || COALESCE(l.res, 'nothing') || ' as ' || l.old_unit
       || ' but to ' || COALESCE(l.res_new, 'nothing') || ' as ' || l.new_unit
  FROM _mig193_line l
 WHERE l.res IS NOT l.res_new
   AND NOT EXISTS (SELECT 1 FROM migration_193_skipped k
                    WHERE k.table_name = l.table_name AND k.row_id = l.row_id);
DELETE FROM _mig193_line WHERE res IS NOT res_new;
-- <<< mig193 skip

-- Through the mig-173 declared-change path. change_reason is set NULL (the
-- guard requires one only for 'manual'): left alone, the audit row would repeat
-- the reason of whoever last edited the line.
INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT table_name, row_id, 'unit', old_unit, new_unit FROM _mig193_line;

UPDATE sales_transactions
   SET unit          = (SELECT l.new_unit FROM _mig193_line l
                         WHERE l.table_name = 'sales_transactions' AND l.row_id = sales_transactions.id),
       change_source = 'import',
       change_actor  = 'mig193-unit-vocabulary',
       change_token  = 'mig193-unit-' || id,
       change_reason = NULL
 WHERE id IN (SELECT row_id FROM _mig193_line WHERE table_name = 'sales_transactions');

UPDATE purchase_transactions
   SET unit          = (SELECT l.new_unit FROM _mig193_line l
                         WHERE l.table_name = 'purchase_transactions' AND l.row_id = purchase_transactions.id),
       change_source = 'import',
       change_actor  = 'mig193-unit-vocabulary',
       change_token  = 'mig193-unit-' || id,
       change_reason = NULL
 WHERE id IN (SELECT row_id FROM _mig193_line WHERE table_name = 'purchase_transactions');

-- ── 6. the other columns read against BSN5657 ──────────────────────────────
INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'promotions', p.id, 'bundle_unit', p.bundle_unit, m.word
  FROM promotions p JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = p.bundle_unit;
UPDATE promotions
   SET bundle_unit = (SELECT word FROM _mig193_map WHERE source = 'BSN5657' AND spelling = promotions.bundle_unit)
 WHERE bundle_unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'product_code_mapping', id, 'bsn_unit', old_unit, new_unit FROM _mig193_pcm;
UPDATE product_code_mapping
   SET bsn_unit = (SELECT new_unit FROM _mig193_pcm x WHERE x.id = product_code_mapping.id)
 WHERE id IN (SELECT id FROM _mig193_pcm);

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'pending_product_suggestions', p.id, 'bsn_unit', p.bsn_unit, m.word
  FROM pending_product_suggestions p JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = p.bsn_unit;
UPDATE pending_product_suggestions
   SET bsn_unit = (SELECT word FROM _mig193_map
                    WHERE source = 'BSN5657' AND spelling = pending_product_suggestions.bsn_unit)
 WHERE bsn_unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'pending_product_suggestions', p.id, 'suggested_unit_type', p.suggested_unit_type, m.word
  FROM pending_product_suggestions p
  JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = p.suggested_unit_type;
UPDATE pending_product_suggestions
   SET suggested_unit_type = (SELECT word FROM _mig193_map
                               WHERE source = 'BSN5657'
                                 AND spelling = pending_product_suggestions.suggested_unit_type)
 WHERE suggested_unit_type IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'credit_note_imports', c.id, 'unit', c.unit, m.word
  FROM credit_note_imports c JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = c.unit;
UPDATE credit_note_imports
   SET unit = (SELECT word FROM _mig193_map WHERE source = 'BSN5657' AND spelling = credit_note_imports.unit)
 WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'express_sales', e.id, 'unit', e.unit, m.word
  FROM express_sales e JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = e.unit;
UPDATE express_sales
   SET unit = (SELECT word FROM _mig193_map WHERE source = 'BSN5657' AND spelling = express_sales.unit)
 WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'product_price_tiers', id, 'qty_label', old_label, new_label FROM _mig193_tier;
UPDATE product_price_tiers
   SET qty_label = (SELECT new_label FROM _mig193_tier x WHERE x.id = product_price_tiers.id)
 WHERE id IN (SELECT id FROM _mig193_tier);

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'express_sales_order_lines', e.id, 'unit', e.unit, m.word
  FROM express_sales_order_lines e JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = e.unit;
UPDATE express_sales_order_lines
   SET unit = (SELECT word FROM _mig193_map
                WHERE source = 'BSN5657' AND spelling = express_sales_order_lines.unit)
 WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'express_credit_note_lines', e.id, 'unit', e.unit, m.word
  FROM express_credit_note_lines e JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = e.unit;
UPDATE express_credit_note_lines
   SET unit = (SELECT word FROM _mig193_map
                WHERE source = 'BSN5657' AND spelling = express_credit_note_lines.unit)
 WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');

-- ── 7. supplier price lists: Sendy spellings only ('*') ────────────────────
INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_catalogue_items', s.id, 'unit', s.unit, m.word
  FROM supplier_catalogue_items s JOIN _mig193_map m ON m.source = '*' AND m.spelling = s.unit;
UPDATE supplier_catalogue_items
   SET unit = (SELECT word FROM _mig193_map WHERE source = '*' AND spelling = supplier_catalogue_items.unit)
 WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = '*');

INSERT INTO migration_193_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_catalogue_price_history', s.id, 'unit', s.unit, m.word
  FROM supplier_catalogue_price_history s JOIN _mig193_map m ON m.source = '*' AND m.spelling = s.unit;
UPDATE supplier_catalogue_price_history
   SET unit = (SELECT word FROM _mig193_map
                WHERE source = '*' AND spelling = supplier_catalogue_price_history.unit)
 WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = '*');

-- ── Postcondition 1: every bill line resolves exactly as it did ────────────
DROP TABLE IF EXISTS temp._mig193_post_resolution;
CREATE TEMP TABLE _mig193_post_resolution AS
SELECT r.row_id
  FROM _mig193_res_pre r
  JOIN sales_transactions t ON t.id = r.row_id
  LEFT JOIN products p ON p.id = t.product_id
 WHERE r.table_name = 'sales_transactions'
   AND r.res IS NOT CASE WHEN t.unit IS NOT NULL AND trim(t.unit, char(32, 9, 10, 11, 12, 13, 160)) = trim(COALESCE(p.unit_type, ''), char(32, 9, 10, 11, 12, 13, 160)) THEN 1.0
                         ELSE (SELECT c.ratio FROM unit_conversions c
                                WHERE c.product_id = t.product_id AND c.bsn_unit = t.unit) END
UNION ALL
SELECT r.row_id
  FROM _mig193_res_pre r
  JOIN purchase_transactions t ON t.id = r.row_id
  LEFT JOIN products p ON p.id = t.product_id
 WHERE r.table_name = 'purchase_transactions'
   AND r.res IS NOT CASE WHEN t.unit IS NOT NULL AND trim(t.unit, char(32, 9, 10, 11, 12, 13, 160)) = trim(COALESCE(p.unit_type, ''), char(32, 9, 10, 11, 12, 13, 160)) THEN 1.0
                         ELSE (SELECT c.ratio FROM unit_conversions c
                                WHERE c.product_id = t.product_id AND c.bsn_unit = t.unit) END;

CREATE TEMP TRIGGER _mig193_guard_post_resolution BEFORE DELETE ON _mig193_post_resolution
BEGIN SELECT RAISE(ABORT, 'mig 193 postcondition FAILED: a bill line would resolve to a different quantity or cost (line_resolution).'); END;
DELETE FROM _mig193_post_resolution;
DROP TRIGGER _mig193_guard_post_resolution;
DROP TABLE _mig193_post_resolution;

-- ── Postcondition 2: no covered column still holds a spelling the map
--    translates, outside the listed skips ───────────────────────────────────
DROP TABLE IF EXISTS temp._mig193_postcheck;
CREATE TEMP TABLE _mig193_postcheck AS
          SELECT id FROM sales_transactions WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
             AND id NOT IN (SELECT row_id FROM migration_193_skipped WHERE table_name = 'sales_transactions')
UNION ALL SELECT id FROM purchase_transactions WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
             AND id NOT IN (SELECT row_id FROM migration_193_skipped WHERE table_name = 'purchase_transactions')
UNION ALL SELECT id FROM products                    WHERE unit_type           IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM promotions                  WHERE bundle_unit         IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM product_code_mapping        WHERE bsn_unit            IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM pending_product_suggestions WHERE bsn_unit            IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM pending_product_suggestions WHERE suggested_unit_type IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM credit_note_imports         WHERE unit                IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM express_sales               WHERE unit                IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM unit_conversions            WHERE bsn_unit            IN (SELECT spelling FROM _mig193_uc_map)
UNION ALL SELECT id FROM express_sales_order_lines   WHERE unit                IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM express_credit_note_lines   WHERE unit                IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657')
UNION ALL SELECT id FROM supplier_catalogue_items    WHERE unit                IN (SELECT spelling FROM _mig193_map WHERE source = '*')
UNION ALL SELECT id FROM supplier_catalogue_price_history WHERE unit           IN (SELECT spelling FROM _mig193_map WHERE source = '*')
UNION ALL SELECT t.id FROM product_price_tiers t JOIN _mig193_tier x ON x.id = t.id WHERE t.qty_label IS NOT x.new_label;

CREATE TEMP TRIGGER _mig193_guard_postcheck BEFORE DELETE ON _mig193_postcheck
BEGIN SELECT RAISE(ABORT, 'mig 193 postcondition FAILED: a covered column still holds a spelling the unit map translates (still_a_spelling).'); END;
DELETE FROM _mig193_postcheck;
DROP TRIGGER _mig193_guard_postcheck;
DROP TABLE _mig193_postcheck;

DROP TABLE _mig193_line;
DROP TABLE _mig193_res_pre;
DROP TABLE _mig193_tier;
DROP TABLE _mig193_pcm;
DROP TABLE _mig193_uc_map;
DROP TABLE _mig193_map;
DROP TABLE _mig193_seed;
DROP TABLE _mig193_bang;

COMMIT;
