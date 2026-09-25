-- 195 — GH #641 item 1: the supplier price list's own unit spellings join the map,
-- and the rows already stored under them are translated in the SAME change.
-- Label-only: no ratio, quantity, price, stock, ledger or cost row changes. Apply:
-- restart the app (database.py::init_db() picks it up). Rollback:
-- 195_supplier_unit_spellings.rollback.sql.
--
-- ⛔ RUN ONLY through the migration runner (Python executescript, which rolls
-- back on the first error) or `sqlite3 -bail`. The plain sqlite3 CLI keeps going
-- after a failed statement, so every RAISE(ABORT) guard below would fail OPEN.
--
-- THE DECISION (Put, 2026-09-22, decisions/log.md — he chose A). In
-- ศรีไทยเจริญโลหะกิจ's price list, tab ค rows 325–332 (ค้อนทุบหินจีนแดง 2P–12P)
-- write the unit `เต้า` while the same hammer at 14P/16P (rows 333–334) writes
-- `ลูก`, and the price follows weight identically across all of them (฿35/ปอนด์),
-- so it is the same per-hammer unit. With it, the abbreviation pairs the list uses
-- interchangeably:
--     เต้า   -> ลูก
--     กป. ก.ป      -> กระป๋อง
--     กล. ก.ล ก.ล. -> กล่อง
--     ปิ๊บ   -> ปิ๊ป      (Sendy's spelling: mig 193 seeded BSN5657 ปป -> ปิ๊ป)
--     ปอนด์  stays ปอนด์  (a unit of its own, 13 rows, deliberately NO map row)
--
-- BOOK '*', NOT BSN5657. These are Sendy spellings a supplier typed, not Express
-- codes, so they belong to every source. The supplier importer already reads the
-- map with `bsn_units.BOOK_ANY` (scripts/import_supplier_catalogue.py:222), which
-- is why this migration needs NO code change and why the #609 rule holds by
-- construction: the stored value becomes exactly what the importer will produce
-- for the same raw input, because both read this one map.
--
-- ⚠ WHY THE COLUMN SCOPE IS ONLY THE TWO SUPPLIER ONES. `ขด` in a supplier
-- column means a COIL of rope, not the Express code ขด = ขีด (64 prod rows).
-- Migration 193 established this split and translates the supplier columns with
-- source '*' only; a '*' row for ขด would silently turn 64 coils into 100g. This
-- migration adds no such row, and the precondition below proves none exists.
--
-- ⚠ `บักเต้า` IS A PRODUCT NAME, not a unit (Put's own note on the decision).
-- Nothing here does substring matching: every translation is an exact `unit = ?`
-- match on the unit column, so a product name can never be touched.
--
-- MEASURED ON PROD, 2026-09-25 (read-only). 77 rows in each of
-- supplier_catalogue_items and supplier_catalogue_price_history, 154 in total:
-- กป. 37+37 · ก.ป 12+12 · กล. 14+14 · เต้า 8+8 · ก.ล 2+2 · ก.ล. 2+2 · ปิ๊บ 2+2.
-- All seven spellings are absent from unit_map today. Every target word is
-- already a Sendy word in it (กระป๋อง, กล่อง, ปิ๊ป, ลูก). The catalogue today
-- spells one unit two ways — ปิ๊ป on 1 row and ปิ๊บ on 2 — which is the exact
-- thing #595 exists to end.
--
-- PRECONDITIONS (each ABORTS; the runner then does not stamp
-- applied_migrations, so a refusal leaves the DB untouched):
--   seed_conflict  one of the seven already exists in unit_map under a DIFFERENT
--                  word. Then somebody else has ruled on it and this must not
--                  overwrite them.
--   kot_would_move a '*' row for ขด exists or is being added, which would
--                  translate 64 coils of rope into ขีด.
--   pound_mapped   a row for ปอนด์ exists or is being added. Put ruled it stays.
--   not_the_map    the translation this migration is about to write differs from
--                  what the map itself says after the seed. Belt and braces on
--                  the #609 rule: the map is built from unit_map, so this can
--                  only fire if a seed row failed to land.
--
-- RECOVERY if one fires: read the named temp table on the same connection, e.g.
--   SELECT * FROM temp._mig195_pre_seed_conflict;
-- fix the data or get Put's ruling, then re-run. Nothing was written.

CREATE TABLE IF NOT EXISTS migration_195_snapshot (
    table_name  TEXT    NOT NULL,
    row_id      INTEGER NOT NULL,
    column_name TEXT    NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    PRIMARY KEY (table_name, row_id, column_name)
);

CREATE TABLE IF NOT EXISTS migration_195_unit_map_added (
    book     TEXT NOT NULL,
    spelling TEXT NOT NULL,
    word     TEXT NOT NULL,
    PRIMARY KEY (book, spelling)
);

-- >>> mig195 seeds
DROP TABLE IF EXISTS temp._mig195_seed;
CREATE TEMP TABLE _mig195_seed (
    book     TEXT NOT NULL,
    spelling TEXT NOT NULL,
    word     TEXT NOT NULL,
    PRIMARY KEY (book, spelling)
);
INSERT INTO _mig195_seed (book, spelling, word) VALUES
    ('*', 'เต้า',  'ลูก'),
    ('*', 'กป.',   'กระป๋อง'),
    ('*', 'ก.ป',   'กระป๋อง'),
    ('*', 'กล.',   'กล่อง'),
    ('*', 'ก.ล',   'กล่อง'),
    ('*', 'ก.ล.',  'กล่อง'),
    ('*', 'ปิ๊บ',  'ปิ๊ป');
-- <<< mig195 seeds

-- >>> mig195 seed preconditions
DROP TABLE IF EXISTS temp._mig195_pre_seed_conflict;
CREATE TEMP TABLE _mig195_pre_seed_conflict AS
SELECT u.id AS map_id, u.book, u.spelling, u.word AS stored_word, s.word AS wanted_word
  FROM unit_map u
  JOIN _mig195_seed s ON s.book = u.book AND s.spelling = u.spelling
 WHERE u.word IS NOT s.word;

DROP TABLE IF EXISTS temp._mig195_pre_kot_would_move;
CREATE TEMP TABLE _mig195_pre_kot_would_move AS
            SELECT 'unit_map' AS src, book, spelling, word FROM unit_map
             WHERE book = '*' AND spelling = 'ขด'
  UNION ALL SELECT 'seed', book, spelling, word FROM _mig195_seed WHERE spelling = 'ขด';

DROP TABLE IF EXISTS temp._mig195_pre_pound_mapped;
CREATE TEMP TABLE _mig195_pre_pound_mapped AS
            SELECT 'unit_map' AS src, book, spelling, word FROM unit_map
             WHERE spelling = 'ปอนด์'
  UNION ALL SELECT 'seed', book, spelling, word FROM _mig195_seed WHERE spelling = 'ปอนด์';
-- <<< mig195 seed preconditions

CREATE TEMP TRIGGER _mig195_guard_seed_conflict BEFORE DELETE ON _mig195_pre_seed_conflict
BEGIN SELECT RAISE(ABORT, 'mig 195 precondition FAILED: a spelling this migration seeds already maps to a DIFFERENT word (seed_conflict). RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig195_guard_kot BEFORE DELETE ON _mig195_pre_kot_would_move
BEGIN SELECT RAISE(ABORT, 'mig 195 precondition FAILED: a book-independent row for ขด exists or is being added (kot_would_move); that would turn 64 coils of rope into ขีด. RECOVERY in the migration header.'); END;
CREATE TEMP TRIGGER _mig195_guard_pound BEFORE DELETE ON _mig195_pre_pound_mapped
BEGIN SELECT RAISE(ABORT, 'mig 195 precondition FAILED: ปอนด์ is mapped or being mapped (pound_mapped); Put ruled it stays a unit of its own. RECOVERY in the migration header.'); END;

DELETE FROM _mig195_pre_seed_conflict;
DELETE FROM _mig195_pre_kot_would_move;
DELETE FROM _mig195_pre_pound_mapped;

DROP TRIGGER IF EXISTS _mig195_guard_seed_conflict;
DROP TRIGGER IF EXISTS _mig195_guard_kot;
DROP TRIGGER IF EXISTS _mig195_guard_pound;

-- >>> mig195 learn
INSERT INTO migration_195_unit_map_added (book, spelling, word)
SELECT s.book, s.spelling, s.word
  FROM _mig195_seed s
 WHERE NOT EXISTS (SELECT 1 FROM unit_map u
                    WHERE u.book = s.book AND u.spelling = s.spelling)
   AND NOT EXISTS (SELECT 1 FROM migration_195_unit_map_added a
                    WHERE a.book = s.book AND a.spelling = s.spelling);

INSERT OR IGNORE INTO unit_map (book, spelling, word)
SELECT book, spelling, word FROM _mig195_seed;
-- <<< mig195 learn

-- >>> mig195 map
-- Built from unit_map AFTER the seeds land, never from the seed list, so what is
-- written is by construction what bsn_units.normalize_unit will produce for the
-- same raw input (the #609 rule). A row whose word IS its spelling is dropped:
-- translating it would be a no-op that still records a snapshot row.
DROP TABLE IF EXISTS temp._mig195_map;
CREATE TEMP TABLE _mig195_map (spelling TEXT PRIMARY KEY, word TEXT NOT NULL);
INSERT INTO _mig195_map (spelling, word)
SELECT u.spelling, u.word
  FROM unit_map u
  JOIN _mig195_seed s ON s.book = u.book AND s.spelling = u.spelling
 WHERE u.book = '*' AND u.word <> u.spelling;
-- <<< mig195 map

DROP TABLE IF EXISTS temp._mig195_pre_not_the_map;
CREATE TEMP TABLE _mig195_pre_not_the_map AS
SELECT s.spelling, s.word AS wanted, m.word AS map_says
  FROM _mig195_seed s
  LEFT JOIN _mig195_map m ON m.spelling = s.spelling
 WHERE m.word IS NOT s.word;

CREATE TEMP TRIGGER _mig195_guard_not_the_map BEFORE DELETE ON _mig195_pre_not_the_map
BEGIN SELECT RAISE(ABORT, 'mig 195 precondition FAILED: the translation differs from what unit_map says after the seed (not_the_map); a seed row did not land. RECOVERY in the migration header.'); END;
DELETE FROM _mig195_pre_not_the_map;
DROP TRIGGER IF EXISTS _mig195_guard_not_the_map;

-- >>> mig195 translate
-- Exact `unit = spelling` matches only, so a product NAME containing one of these
-- words (บักเต้า) can never be touched.
INSERT INTO migration_195_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_catalogue_items', t.id, 'unit', t.unit, m.word
  FROM supplier_catalogue_items t
  JOIN _mig195_map m ON m.spelling = t.unit
 WHERE NOT EXISTS (SELECT 1 FROM migration_195_snapshot s
                    WHERE s.table_name = 'supplier_catalogue_items'
                      AND s.row_id = t.id AND s.column_name = 'unit');

INSERT INTO migration_195_snapshot (table_name, row_id, column_name, old_value, new_value)
SELECT 'supplier_catalogue_price_history', t.id, 'unit', t.unit, m.word
  FROM supplier_catalogue_price_history t
  JOIN _mig195_map m ON m.spelling = t.unit
 WHERE NOT EXISTS (SELECT 1 FROM migration_195_snapshot s
                    WHERE s.table_name = 'supplier_catalogue_price_history'
                      AND s.row_id = t.id AND s.column_name = 'unit');

UPDATE supplier_catalogue_items
   SET unit = (SELECT word FROM _mig195_map m WHERE m.spelling = supplier_catalogue_items.unit)
 WHERE unit IN (SELECT spelling FROM _mig195_map);

UPDATE supplier_catalogue_price_history
   SET unit = (SELECT word FROM _mig195_map m
                WHERE m.spelling = supplier_catalogue_price_history.unit)
 WHERE unit IN (SELECT spelling FROM _mig195_map);
-- <<< mig195 translate
