-- 190 — GH #599 (#595 · 4/7): give BSN5657's `กร` / `ถง` / `บล` Express's own
-- meaning (กุรุส / ถัง / บล็อก) in the unit map, and in the SAME change give every
-- affected product a conversion under the NEW word at the ratio its OLD reading
-- resolves to today. Apply: restart the app (database.py::init_db() runs it).
-- Rollback: 190_express_unit_meanings.rollback.sql.
--
-- ⛔ RUN ONLY through the migration runner (Python executescript, which rolls
-- back on the first error) or `sqlite3 -bail`. The plain sqlite3 CLI keeps
-- going after a failed statement, so every RAISE(ABORT) guard below would fail
-- OPEN: the abort cancels its own statement, the rest of the file runs, and
-- COMMIT keeps it.
--
-- WHY. A code's meaning comes from the Express book it came from (ADR 0018).
-- BSN5657's own unit list says `กร` = กุรุส (a gross; TFACTOR 144 on 1,252 of
-- its 1,264 stock-card lines), `ถง` = ถัง and `บล` = บล็อก. Sendy's hand-built
-- map read them as ตัว / ถุง / แผง — the trap behind #581. Migration 186
-- deliberately left these three alone (its EXCLUDED_CODES) so that the meaning
-- change and its conversions could ship together, here.
--
-- WHY THE CONVERSIONS SHIP IN THE SAME MIGRATION. From the moment the map
-- flips, the importer stores `กุรุส` for a raw `กร` line. `bsn_sync._get_base_qty`
-- then looks up the conversion for `กุรุส`, and `sales_filters.base_qty_sql`
-- (COGS) falls back to ratio 1.0 when it finds none — so a gross would be
-- costed as one piece. Worse, re-importing a line already stored under the old
-- word rewrites it (`bsn_line._unit_same` normalises the STORED side too), and
-- pass 2 then re-posts its ledger through the NEW word's conversion. Both
-- paths are quantity-neutral only if the new word resolves to exactly what the
-- old one resolves to. That is what this migration writes.
--
-- WHICH PRODUCTS. The union of two arms, both derived at run time:
--   (a) a product holding a conversion keyed on the RAW CODE — visible in the
--       DB (12 products on prod, all on `กร`);
--   (b) a product Express BILLS under the code — invisible in Sendy, because
--       the importer translated the code away on the way in. The (code, stock
--       code) pairs below come from the BSN5657 stock card via
--       scripts/derive_599_express_unit_codes.py and are resolved through
--       product_code_mapping here. 61 pairs, read 2026-09-21 from the
--       2026-08-25 STCRD snapshot: กร 43 codes / 1,264 lines, ถง 11 / 26,
--       บล 7 / 8. None of the 22 that resolve to a product is is_ignored.
--
-- WHICH RATIO. Exactly what `_get_base_qty` resolves the OLD reading to today,
-- with the raw-code row preferred when one exists (#599's own comment: "derive
-- each new กุรุส row's ratio from the product's live กร row at migration time"):
--       code_ratio  = unit_conversions(product, '<code>').ratio
--       old_ratio   = 1.0 when the old word IS the product's unit_type
--                     (_get_base_qty's short circuit), else
--                     unit_conversions(product, '<old word>').ratio
--       target      = COALESCE(code_ratio, old_ratio)
-- A product whose target is NULL gets NOTHING: the old word did not resolve
-- either, so its lines are unsynced today and stay unsynced — the same answer,
-- and /unit-conversions still asks Put to name the ratio.
--
-- ⚠ NOT A RATIO FIX. Ten of the twelve `กร` products carry ratio 1.0 while
-- Express's factor on their lines is 144 — they are the "~10 products whose own
-- unit ตัว actually means a gross" that #595 puts OUT of scope and #581/#603
-- rebase one at a time. This migration copies 1.0 forward verbatim, because
-- copying anything else would move stock. Same for the four ถง/บล products
-- whose Express factor (2, 3, 35, 50) disagrees with Sendy's 1.0; they are
-- listed in the PR body.
--
-- ⚠ OLD STORED ROWS ARE NOT RELABELLED HERE. `ตัว`/`ถุง`/`แผง` rows whose Express
-- code was กร/ถง/บล keep their word until #600 relabels them from the stock
-- card. The 63 raw `กร` purchase rows (batch 37) keep reading `กร`, whose
-- conversion this migration leaves in place.
--
-- PRECONDITIONS (migration-177 shape: a temp table of violators and a BEFORE
-- DELETE trigger that RAISEs; the runner then rolls back and does NOT stamp
-- applied_migrations, so a failed precondition leaves the DB untouched). Each
-- list is empty on the 2026-09-19 prod snapshot. RECOVERY per guard:
--   code_vs_old   the raw-code conversion and the old word resolve to
--                 DIFFERENT ratios on one product, so a relabel would move
--                 stock whichever one is copied. Read the product's bills,
--                 decide which ratio is real, fix or delete the wrong row.
--   new_conflict  a conversion already exists under the NEW word at a ratio
--                 other than the target. Decide which is right; this migration
--                 must not overwrite a ratio a human set.
--   new_is_base   the new word IS the product's unit_type while the target is
--                 not 1.0. A line in that unit short-circuits to ratio 1
--                 (_get_base_qty), so the conversion would never be read and
--                 the next ledger rebuild would post a different quantity.
--                 Fix the unit_type or the ratio.
--
-- ⚠ Sequencing: do not ship in the same deploy as #600 or #586's data change.

PRAGMA busy_timeout = 10000;

BEGIN;

-- >>> mig190 meanings
DROP TABLE IF EXISTS temp._mig190_meaning;
CREATE TEMP TABLE _mig190_meaning (
    code     TEXT PRIMARY KEY,
    old_word TEXT NOT NULL,   -- what Sendy's map produced before this migration
    new_word TEXT NOT NULL    -- what BSN5657's own unit list means by the code
);
INSERT INTO _mig190_meaning (code, old_word, new_word) VALUES
    ('กร', 'ตัว', 'กุรุส'),
    ('ถง', 'ถุง', 'ถัง'),
    ('บล', 'แผง', 'บล็อก');
-- <<< mig190 meanings

-- >>> mig190 stkcod
-- The BSN5657 stock codes Express bills under each code. Generated by
-- scripts/derive_599_express_unit_codes.py; the comment on each row is that
-- script's line count and TFACTOR histogram, kept as the audit trail.
DROP TABLE IF EXISTS temp._mig190_stkcod;
CREATE TEMP TABLE _mig190_stkcod (code TEXT NOT NULL, bsn_code TEXT NOT NULL);
INSERT INTO _mig190_stkcod (code, bsn_code) VALUES

    ('กร', '012ด5000'),  -- 11 lines, TFACTOR {144.0: 11}
    ('กร', '512ห0030'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '528ก2010'),  -- 5 lines, TFACTOR {144.0: 5}
    ('กร', '528ก2011'),  -- 5 lines, TFACTOR {144.0: 5}
    ('กร', '528ก2012'),  -- 10 lines, TFACTOR {144.0: 10}
    ('กร', '528ก2013'),  -- 10 lines, TFACTOR {144.0: 10}
    ('กร', '528ก2014'),  -- 5 lines, TFACTOR {144.0: 5}
    ('กร', '528ก2015'),  -- 12 lines, TFACTOR {144.0: 12}
    ('กร', '556ข6061'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '556ข6062'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '605ก2010'),  -- 152 lines, TFACTOR {144.0: 152}
    ('กร', '605ก2011'),  -- 80 lines, TFACTOR {144.0: 80}
    ('กร', '605ก2012'),  -- 69 lines, TFACTOR {144.0: 69}
    ('กร', '605ก2013'),  -- 226 lines, TFACTOR {144.0: 226}
    ('กร', '605ก2014'),  -- 124 lines, TFACTOR {144.0: 124}
    ('กร', '605ก2015'),  -- 50 lines, TFACTOR {144.0: 50}
    ('กร', '610ต2160'),  -- 5 lines, TFACTOR {144.0: 5}
    ('กร', '610ต2170'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '610ต2180'),  -- 8 lines, TFACTOR {144.0: 8}
    ('กร', '610ต2185'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '610ต2190'),  -- 1 lines, TFACTOR {144.0: 1}
    ('กร', '610ต2191'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '610ต2210'),  -- 1 lines, TFACTOR {144.0: 1}
    ('กร', '610ต2211'),  -- 3 lines, TFACTOR {144.0: 3}
    ('กร', '610ต2212'),  -- 124 lines, TFACTOR {144.0: 124}
    ('กร', '610ต2217'),  -- 3 lines, TFACTOR {144.0: 3}
    ('กร', '610ต2228'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '625บ7401'),  -- 23 lines, TFACTOR {144.0: 23}
    ('กร', '625บ7404'),  -- 3 lines, TFACTOR {144.0: 3}
    ('กร', '626ด1800'),  -- 55 lines, TFACTOR {1.0: 12, 144.0: 43}
    ('กร', '626ด1909'),  -- 60 lines, TFACTOR {144.0: 60}
    ('กร', '631บ8401'),  -- 10 lines, TFACTOR {144.0: 10}
    ('กร', '631บ8404'),  -- 10 lines, TFACTOR {144.0: 10}
    ('กร', '900ข2109'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '900ข2112'),  -- 1 lines, TFACTOR {144.0: 1}
    ('กร', '900ข2113'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '900ข2114'),  -- 2 lines, TFACTOR {144.0: 2}
    ('กร', '900ข5160'),  -- 85 lines, TFACTOR {144.0: 85}
    ('กร', '900ข5170'),  -- 33 lines, TFACTOR {144.0: 33}
    ('กร', '900ข5180'),  -- 5 lines, TFACTOR {144.0: 5}
    ('กร', '999ข2001'),  -- 7 lines, TFACTOR {144.0: 7}
    ('กร', '999ข5002'),  -- 4 lines, TFACTOR {144.0: 4}
    ('กร', '999ส5000'),  -- 44 lines, TFACTOR {144.0: 44}
    ('ถง', '026ต3020'),  -- 1 lines, TFACTOR {50.0: 1}
    ('ถง', '026ต3040'),  -- 1 lines, TFACTOR {50.0: 1}
    ('ถง', '041ม5555'),  -- 1 lines, TFACTOR {2.0: 1}
    ('ถง', '528ด8650'),  -- 1 lines, TFACTOR {3.0: 1}
    ('ถง', '540ค0222'),  -- 1 lines, TFACTOR {1.0: 1}
    ('ถง', '540ค0666'),  -- 1 lines, TFACTOR {1.0: 1}
    ('ถง', '567จ1218'),  -- 2 lines, TFACTOR {1.0: 2}
    ('ถง', '567จ1250'),  -- 6 lines, TFACTOR {1.0: 6}
    ('ถง', '600ส7320'),  -- 4 lines, TFACTOR {25.0: 3, 50.0: 1}
    ('ถง', '605จ1138'),  -- 4 lines, TFACTOR {1.0: 4}
    ('ถง', '605จ1237'),  -- 4 lines, TFACTOR {1.0: 4}
    ('บล', '026ต2210-2'),  -- 1 lines, TFACTOR {1.0: 1}
    ('บล', '026ต2210-3'),  -- 1 lines, TFACTOR {1.0: 1}
    ('บล', '026ต2510-1'),  -- 2 lines, TFACTOR {1.0: 1, 35.0: 1}
    ('บล', '026ต2510-2'),  -- 1 lines, TFACTOR {1.0: 1}
    ('บล', '026ต2510-3'),  -- 1 lines, TFACTOR {1.0: 1}
    ('บล', '026ต2710-2'),  -- 1 lines, TFACTOR {1.0: 1}
    ('บล', '026ต2710-3');  -- 1 lines, TFACTOR {1.0: 1}
-- <<< mig190 stkcod

-- ── The affected products, and the ratio each new-word row must carry ──────
DROP TABLE IF EXISTS temp._mig190_affected;
CREATE TEMP TABLE _mig190_affected AS
SELECT m.code, m.old_word, m.new_word, c.product_id
  FROM unit_conversions c
  JOIN _mig190_meaning m ON m.code = c.bsn_unit
UNION
SELECT m.code, m.old_word, m.new_word, pcm.product_id
  FROM _mig190_stkcod s
  JOIN _mig190_meaning m ON m.code = s.code
  JOIN product_code_mapping pcm ON pcm.bsn_code = s.bsn_code
 WHERE pcm.product_id IS NOT NULL;

DROP TABLE IF EXISTS temp._mig190_plan;
CREATE TEMP TABLE _mig190_plan AS
SELECT a.code, a.old_word, a.new_word, a.product_id,
       (SELECT u.ratio FROM unit_conversions u
         WHERE u.product_id = a.product_id AND u.bsn_unit = a.code)   AS code_ratio,
       CASE WHEN a.old_word = TRIM(COALESCE(p.unit_type, '')) THEN 1.0
            ELSE (SELECT w.ratio FROM unit_conversions w
                   WHERE w.product_id = a.product_id AND w.bsn_unit = a.old_word)
       END                                                            AS old_ratio,
       (SELECT n.ratio FROM unit_conversions n
         WHERE n.product_id = a.product_id AND n.bsn_unit = a.new_word) AS new_ratio,
       TRIM(COALESCE(p.unit_type, ''))                                AS unit_type
  FROM _mig190_affected a
  JOIN products p ON p.id = a.product_id;

-- >>> mig190 preconditions
DROP TABLE IF EXISTS temp._mig190_pre_code_vs_old;
CREATE TEMP TABLE _mig190_pre_code_vs_old AS
SELECT product_id, code FROM _mig190_plan
 WHERE code_ratio IS NOT NULL AND old_ratio IS NOT NULL AND code_ratio <> old_ratio;

DROP TABLE IF EXISTS temp._mig190_pre_new_conflict;
CREATE TEMP TABLE _mig190_pre_new_conflict AS
SELECT product_id, new_word FROM _mig190_plan
 WHERE new_ratio IS NOT NULL
   AND COALESCE(code_ratio, old_ratio) IS NOT NULL
   AND new_ratio <> COALESCE(code_ratio, old_ratio);

DROP TABLE IF EXISTS temp._mig190_pre_new_is_base;
CREATE TEMP TABLE _mig190_pre_new_is_base AS
SELECT product_id, new_word FROM _mig190_plan
 WHERE new_word = unit_type
   AND COALESCE(code_ratio, old_ratio) IS NOT NULL
   AND COALESCE(code_ratio, old_ratio) <> 1.0;
-- <<< mig190 preconditions

CREATE TEMP TRIGGER _mig190_guard_code_vs_old BEFORE DELETE ON _mig190_pre_code_vs_old
BEGIN SELECT RAISE(ABORT, 'mig 190 precondition FAILED: the raw Express code and the old word resolve to different ratios on one product (code_vs_old). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig190_guard_new_conflict BEFORE DELETE ON _mig190_pre_new_conflict
BEGIN SELECT RAISE(ABORT, 'mig 190 precondition FAILED: a conversion already exists under the new word at a different ratio (new_conflict). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig190_guard_new_is_base BEFORE DELETE ON _mig190_pre_new_is_base
BEGIN SELECT RAISE(ABORT, 'mig 190 precondition FAILED: the new word is the product unit_type while the target ratio is not 1 (new_is_base). RECOVERY in the migration header.'); END;

DELETE FROM _mig190_pre_code_vs_old;
DELETE FROM _mig190_pre_new_conflict;
DELETE FROM _mig190_pre_new_is_base;
DROP TABLE _mig190_pre_code_vs_old;
DROP TABLE _mig190_pre_new_conflict;
DROP TABLE _mig190_pre_new_is_base;

-- ── Snapshots: forensic record + the rollback's source. IF NOT EXISTS, never
--    drop-first: a hand re-run must not erase what the rollback needs.
CREATE TABLE IF NOT EXISTS migration_190_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    row_id      INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);

CREATE TABLE IF NOT EXISTS migration_190_uc_inserted (
    id          INTEGER NOT NULL,
    product_id  INTEGER,
    bsn_unit    TEXT,
    ratio       REAL
);

-- ── 1. the new-word conversions ───────────────────────────────────────────
--    Re-runnable: on a second pass every planned row already exists, so
--    `new_ratio IS NULL` selects nothing and neither statement writes.
INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
SELECT product_id, new_word, COALESCE(code_ratio, old_ratio)
  FROM _mig190_plan
 WHERE new_ratio IS NULL AND COALESCE(code_ratio, old_ratio) IS NOT NULL;

INSERT INTO migration_190_uc_inserted (id, product_id, bsn_unit, ratio)
SELECT u.id, u.product_id, u.bsn_unit, u.ratio
  FROM unit_conversions u
  JOIN _mig190_plan pl ON pl.product_id = u.product_id AND pl.new_word = u.bsn_unit
 WHERE pl.new_ratio IS NULL AND COALESCE(pl.code_ratio, pl.old_ratio) IS NOT NULL;

-- ── 2. the map itself ─────────────────────────────────────────────────────
INSERT INTO migration_190_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'unit_map', um.id, 'word', um.word, m.new_word
  FROM unit_map um
  JOIN _mig190_meaning m ON m.code = um.spelling
 WHERE um.book = 'BSN5657' AND um.word <> m.new_word;

UPDATE unit_map
   SET word = (SELECT new_word FROM _mig190_meaning WHERE code = unit_map.spelling)
 WHERE book = 'BSN5657'
   AND spelling IN (SELECT code FROM _mig190_meaning);

-- ── Postconditions ────────────────────────────────────────────────────────
-- (a) the map now says what Express means, (b) every product holding a
-- conversion under a raw code also holds one under its word (the ticket's AC),
-- and (c) every planned row landed at its target ratio.
DROP TABLE IF EXISTS temp._mig190_postcheck;
CREATE TEMP TABLE _mig190_postcheck AS
SELECT 'map' AS why, m.code AS detail
  FROM _mig190_meaning m
  JOIN unit_map um ON um.book = 'BSN5657' AND um.spelling = m.code
 WHERE um.word <> m.new_word
UNION ALL
SELECT 'code row without its word row', c.bsn_unit || ' pid ' || c.product_id
  FROM unit_conversions c
  JOIN _mig190_meaning m ON m.code = c.bsn_unit
 WHERE NOT EXISTS (SELECT 1 FROM unit_conversions w
                    WHERE w.product_id = c.product_id AND w.bsn_unit = m.new_word)
UNION ALL
SELECT 'planned row missing or off-target', pl.new_word || ' pid ' || pl.product_id
  FROM _mig190_plan pl
 WHERE COALESCE(pl.code_ratio, pl.old_ratio) IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM unit_conversions n
                    WHERE n.product_id = pl.product_id AND n.bsn_unit = pl.new_word
                      AND n.ratio = COALESCE(pl.code_ratio, pl.old_ratio));

CREATE TEMP TRIGGER _mig190_postcondition_guard BEFORE DELETE ON _mig190_postcheck
BEGIN SELECT RAISE(ABORT, 'mig 190 postcondition FAILED: the map or a new-word conversion is not what this migration promised.'); END;
DELETE FROM _mig190_postcheck;
DROP TRIGGER _mig190_postcondition_guard;
DROP TABLE _mig190_postcheck;
DROP TABLE _mig190_plan;
DROP TABLE _mig190_affected;
DROP TABLE _mig190_stkcod;
DROP TABLE _mig190_meaning;

COMMIT;
