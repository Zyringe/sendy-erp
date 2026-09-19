-- 185_unit_map_table.sql
-- The unit map (แผนที่หน่วย) becomes one DB table (#596, spec #595, ADR 0018).
--
-- Replaces migration 064's `bsn_unit_alias` (seeded once for a historical
-- repair, no runtime reader since) with `unit_map`, the table
-- inventory_app/bsn_units.py now reads and writes at runtime instead of
-- data/reference/bsn_unit_full.json (that file and every write to it are
-- retired in this same PR).
--
-- Keyed by (book, spelling): a code's MEANING comes from the Express book it
-- came from (BSN5657 or xp5); '*' is a book-independent variant (a Sendy
-- spelling like `กก.`/`กิโล` that means the same thing regardless of book).
--
-- NO MEANING CHANGES in this migration — it seeds exactly the 44 entries
-- data/reference/bsn_unit_full.json held today, all under BSN5657, so every
-- caller that doesn't pass a book yet (bsn_units.DEFAULT_BOOK) keeps reading
-- today's translations verbatim (กร still -> ตัว). Tickets #599/#601 correct
-- the wrong meanings (กร/ถง/บล) and add the xp5-specific rows (หอ -> หลอด).
--
-- Drop-first so this file is re-runnable (erp-engineering-discipline.md).

BEGIN;

DROP TABLE IF EXISTS unit_map;
DROP TABLE IF EXISTS bsn_unit_alias;

CREATE TABLE unit_map (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book        TEXT NOT NULL,   -- 'BSN5657' | 'xp5' | '*' (applies to every book)
    spelling    TEXT NOT NULL,   -- the Express code or spelling variant, exactly as written
    word        TEXT NOT NULL,   -- the one Sendy spelling for this หน่วย
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX ux_unit_map_book_spelling ON unit_map(book, spelling);

INSERT INTO unit_map (book, spelling, word) VALUES
  ('BSN5657', 'ดก', 'ดอก'),
  ('BSN5657', 'ปน', 'ปื้น'),
  ('BSN5657', 'กส', 'กระสอบ'),
  ('BSN5657', 'บล', 'แผง'),
  ('BSN5657', 'กล', 'กล่อง'),
  ('BSN5657', 'อน', 'อัน'),
  ('BSN5657', 'ผน', 'แผ่น'),
  ('BSN5657', 'หล', 'โหล'),
  ('BSN5657', 'ชด', 'ชุด'),
  ('BSN5657', 'ผง', 'แผง'),
  ('BSN5657', 'มน', 'ม้วน'),
  ('BSN5657', 'ลง', 'ลัง'),
  ('BSN5657', 'ซง', 'ซอง'),
  ('BSN5657', 'โหล', 'โหล'),
  ('BSN5657', 'กก', 'กิโลกรัม'),
  ('BSN5657', 'ซอง', 'ซอง'),
  ('BSN5657', 'ตว', 'ตัว'),
  ('BSN5657', 'แพ', 'แพ็ค'),
  ('BSN5657', 'ลก', 'ลูก'),
  ('BSN5657', 'อัน', 'อัน'),
  ('BSN5657', 'ขด', 'ขีด'),
  ('BSN5657', 'ถุ', 'ถุง'),
  ('BSN5657', 'ชุด', 'ชุด'),
  ('BSN5657', 'ลัง', 'ลัง'),
  ('BSN5657', 'กน', 'ก้อน'),
  ('BSN5657', 'หด', 'หลอด'),
  ('BSN5657', 'หค', 'โหลคู่'),
  ('BSN5657', 'กร', 'ตัว'),
  ('BSN5657', 'สน', 'เส้น'),
  ('BSN5657', 'กป', 'กระป๋อง'),
  ('BSN5657', 'คู', 'คู่'),
  ('BSN5657', 'หอ', 'ห่อ'),
  ('BSN5657', 'คน', 'คัน'),
  ('BSN5657', 'ทง', 'แท่ง'),
  ('BSN5657', 'ผื', 'ผืน'),
  ('BSN5657', 'แก', 'แกลลอน'),
  ('BSN5657', 'ถง', 'ถุง'),
  ('BSN5657', 'แพค', 'แพ็ค'),
  ('BSN5657', '!กล', 'กล่อง'),
  ('BSN5657', '!คู', 'คู่'),
  ('BSN5657', 'ชน', 'ชิ้น'),
  ('BSN5657', '!ลก', 'ลูก'),
  ('BSN5657', '!หด', 'หลอด'),
  ('BSN5657', '!หล', 'โหล');

COMMIT;
