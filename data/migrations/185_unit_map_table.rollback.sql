-- 185_unit_map_table.rollback.sql
-- Manual rollback (the migration runner does not auto-rollback).
--
-- Drops unit_map and restores bsn_unit_alias to migration 064's original 44
-- rows (the shape scripts/backfill_express_unit_normalize.py and the old
-- bsn_units.py (JSON-backed) expect if this migration is reverted). This is
-- a pure schema/label rollback — 185 changed no ledger data, so there is
-- nothing to undo beyond the table itself.
--
-- ⚠ Dropping unit_map also drops every code named on /unit-conversions since
-- 185 shipped (they live nowhere else). Export them first if any matter:
--   SELECT book, spelling, word, created_at FROM unit_map ORDER BY id;

BEGIN;

DROP TABLE IF EXISTS unit_map;

CREATE TABLE IF NOT EXISTS bsn_unit_alias (
    acronym TEXT PRIMARY KEY,
    full    TEXT NOT NULL
);

DELETE FROM bsn_unit_alias;
INSERT INTO bsn_unit_alias (acronym, full) VALUES
  ('ดก', 'ดอก'),
  ('ปน', 'ปื้น'),
  ('กส', 'กระสอบ'),
  ('บล', 'แผง'),
  ('กล', 'กล่อง'),
  ('อน', 'อัน'),
  ('ผน', 'แผ่น'),
  ('หล', 'โหล'),
  ('ชด', 'ชุด'),
  ('ผง', 'แผง'),
  ('มน', 'ม้วน'),
  ('ลง', 'ลัง'),
  ('ซง', 'ซอง'),
  ('โหล', 'โหล'),
  ('กก', 'กิโลกรัม'),
  ('ซอง', 'ซอง'),
  ('ตว', 'ตัว'),
  ('แพ', 'แพ็ค'),
  ('ลก', 'ลูก'),
  ('อัน', 'อัน'),
  ('ขด', 'ขีด'),
  ('ถุ', 'ถุง'),
  ('ชุด', 'ชุด'),
  ('ลัง', 'ลัง'),
  ('กน', 'ก้อน'),
  ('หด', 'หลอด'),
  ('หค', 'โหลคู่'),
  ('กร', 'ตัว'),
  ('สน', 'เส้น'),
  ('กป', 'กระป๋อง'),
  ('คู', 'คู่'),
  ('หอ', 'ห่อ'),
  ('คน', 'คัน'),
  ('ทง', 'แท่ง'),
  ('ผื', 'ผืน'),
  ('แก', 'แกลลอน'),
  ('ถง', 'ถุง'),
  ('แพค', 'แพ็ค'),
  ('!กล', 'กล่อง'),
  ('!คู', 'คู่'),
  ('ชน', 'ชิ้น'),
  ('!ลก', 'ลูก'),
  ('!หด', 'หลอด'),
  ('!หล', 'โหล');

DELETE FROM applied_migrations
 WHERE filename = '185_unit_map_table.sql';

COMMIT;
