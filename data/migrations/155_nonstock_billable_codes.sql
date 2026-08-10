-- ============================================================================
-- 155 — ZZZ + 888ค8888 become billable non-stock lines.
--
-- Classification itself lives in models/stock_filters.py (a constant, because
-- vat_book_builder inserts its mappings AFTER migrations run — a column set
-- here would be silently 0 on every fresh build). This migration only fixes
-- the two pieces of DATA that the constant cannot reach.
--
-- The unit_conversions deletion is defence in depth, not tidying: those rows
-- exist only to serve a sync that must never run again, and they are what let
-- a ค่าขนส่ง line become phantom inventory on pid 1211 before.
-- Drop-first shape so a hand re-run on a rehearsal copy is safe.
-- ============================================================================
UPDATE product_code_mapping
   SET is_ignored = 0
 WHERE bsn_code IN ('ZZZ', '888ค8888');

DELETE FROM unit_conversions WHERE product_id IN (1211, 1623);
