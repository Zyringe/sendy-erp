-- 133: Backfill blank customer_code on marketplace SR (return / credit-note) rows.
--
-- The team books each marketplace order as ONE Express invoice under a platform
-- customer code — Zหน้าร้าน (Shopee) / Bหน้าร้าน (Shopee บุญเรืองเรือง, closed) /
-- Lหน้าร้าน (Lazada) — but keys marketplace RETURNS (SR docs, ใบลดหนี้) with the
-- customer NAME filled (หน้าร้านS/B/L) and the customer CODE left blank. Sendy
-- imported both verbatim, so /customers (models.get_customers GROUPs BY
-- customer_code) shows each shop TWICE: once under its real code, once under a
-- blank code holding only the SR rows.
--
-- Verified 2026-07-08: 100% systematic — every marketplace SR doc has a blank
-- code, every IV carries it (46 หน้าร้านS + 12 หน้าร้านB + 30 หน้าร้านL = 88 rows).
-- This is a pure RELABEL: net/qty/total are untouched (no money movement),
-- guarded to SR docs only so a blank-code IV (a different bug) is never swept
-- in, and the marketplace matcher reads doc_base LIKE 'IV%' only so it is
-- unaffected. Idempotent: re-running finds no blank rows and is a no-op.
UPDATE sales_transactions SET customer_code = 'Zหน้าร้าน'
 WHERE customer = 'หน้าร้านS'
   AND (customer_code IS NULL OR customer_code = '')
   AND doc_base LIKE 'SR%';

UPDATE sales_transactions SET customer_code = 'Bหน้าร้าน'
 WHERE customer = 'หน้าร้านB'
   AND (customer_code IS NULL OR customer_code = '')
   AND doc_base LIKE 'SR%';

UPDATE sales_transactions SET customer_code = 'Lหน้าร้าน'
 WHERE customer = 'หน้าร้านL'
   AND (customer_code IS NULL OR customer_code = '')
   AND doc_base LIKE 'SR%';
