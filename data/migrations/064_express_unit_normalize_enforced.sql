-- 064_express_unit_normalize_enforced.sql
-- Make historical Express-unit normalization an ENFORCED, automatic part of
-- the deploy (the migration runner runs this), instead of a manual script.
--
-- Why (Codex adversarial review high, 2026-05-20):
--   migration 063 recomputes express_sales.brand_kind by comparing
--   express_sales.unit to product_code_mapping.bsn_unit. Historical
--   express_sales.unit was imported RAW ('กล') while bsn_unit is canonical
--   ('กล่อง'), so 063's auto-run recompute on a never-normalized DB writes
--   wrong brand_kind for split codes, and commission then trusts it. The
--   normalization fix (scripts/backfill_express_unit_normalize.py) was a
--   manual post-deploy step → if skipped/delayed/failed there was no
--   guardrail. This migration folds that normalization into the same
--   automatic path and recomputes brand_kind AFTER, so deploy alone leaves
--   a correct state.
--
--   SQLite can't call the Python normalizer (bsn_units is JSON-backed, no
--   DB). So we snapshot the acronym map (data/reference/bsn_unit_full.json,
--   44 entries, generated 2026-05-20) into a table and normalize via JOIN.
--   This is a one-time historical-repair snapshot; runtime normalization
--   for NEW rows still goes through bsn_units.normalize_unit() at import
--   (import_express.py) — the JSON stays the single source of truth there.
--   bsn_unit_alias is reseeded idempotently (DELETE+INSERT) so re-running
--   this migration on a fresh mirror stays correct.
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner records
--   every migration it executes automatically.

BEGIN;

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

-- Normalize historical express_sales.unit in place (raw acronym → canonical).
-- Mirrors bsn_units.normalize_unit(): map.get(unit, unit) — only rows whose
-- unit is a known acronym change; unknown/already-canonical untouched.
UPDATE express_sales
   SET unit = (SELECT a.full FROM bsn_unit_alias a WHERE a.acronym = express_sales.unit)
 WHERE unit IN (SELECT acronym FROM bsn_unit_alias)
   AND unit <> (SELECT a.full FROM bsn_unit_alias a WHERE a.acronym = express_sales.unit);

-- Now that units are canonical, recompute brand_kind with the resolver-
-- faithful rule (identical to migration 063: resolve product_id from
-- product_code_mapping ALONE, then look up brand; unresolved rows untouched).
UPDATE express_sales
   SET brand_kind = (
       SELECT CASE WHEN br.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
         FROM brands br
        WHERE br.id = (
              SELECT p.brand_id FROM products p
               WHERE p.id = (
                     SELECT m.product_id
                       FROM product_code_mapping m
                      WHERE m.bsn_code = express_sales.product_code
                        AND m.bsn_unit IN (COALESCE(express_sales.unit, ''), '')
                        AND m.product_id IS NOT NULL
                      ORDER BY (m.bsn_unit = '')
                      LIMIT 1
               )
        )
   )
 WHERE EXISTS (
       SELECT 1
         FROM product_code_mapping m
        WHERE m.bsn_code = express_sales.product_code
          AND m.bsn_unit IN (COALESCE(express_sales.unit, ''), '')
          AND m.product_id IS NOT NULL
   );

COMMIT;
