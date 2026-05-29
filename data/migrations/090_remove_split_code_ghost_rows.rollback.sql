-- ============================================================================
-- Rollback 090 — restore split-code ghost rows
--
-- Re-inserts the three ghost sales_transactions rows and their linked OUT
-- transactions. Stock re-adjusts automatically via the after_transaction_insert
-- trigger (mig 080). Run manually then delete the applied_migrations row.
--
-- NOTE: the ghost rows are identified by their ORIGINAL values. The re-inserted
-- transactions rows will receive NEW autoincrement ids (not the original 41258,
-- 41259, 66666). The ST rows re-use their original ids via INSERT OR IGNORE.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- ── Step 1: re-insert ghost sales_transactions rows ──────────────────────────
-- Original ids: 73, 295, 313. ON CONFLICT IGNORE is safe because these rows
-- only exist in a rolled-back DB (already deleted by mig 090 forward run).

INSERT OR IGNORE INTO sales_transactions
    (id, batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
     product_name_raw, customer_code, qty, unit, unit_price,
     vat_type, discount, total, net, synced_to_stock)
SELECT
    73, batch_id, date_iso, doc_no, doc_base, product_id, '041ม2761',
    product_name_raw, customer_code, qty, unit, unit_price,
    vat_type, discount, 1122.0, 1122.0, 1
FROM sales_transactions
WHERE doc_no = 'IV6900394-7' AND bsn_code = '041ม2760'
LIMIT 1;

INSERT OR IGNORE INTO sales_transactions
    (id, batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
     product_name_raw, customer_code, qty, unit, unit_price,
     vat_type, discount, total, net, synced_to_stock)
SELECT
    295, 1, date_iso, 'IV6900391-2', 'IV6900391', 815, '556ห7000',
    product_name_raw, customer_code, qty, unit, unit_price,
    vat_type, discount, 25.0, 25.0, 1
FROM sales_transactions
WHERE doc_no = 'IV6900391-2'
LIMIT 1;

INSERT OR IGNORE INTO sales_transactions
    (id, batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
     product_name_raw, customer_code, qty, unit, unit_price,
     vat_type, discount, total, net, synced_to_stock)
SELECT
    313, -2, date_iso, 'IV6900392-1', 'IV6900392', 436, '999อ1501',
    product_name_raw, customer_code, 5, unit, unit_price,
    vat_type, discount, 101.0, 101.0, 1
FROM sales_transactions
WHERE doc_no = 'IV6900392-1'
LIMIT 1;

-- ── Step 2: re-insert ghost transactions rows ────────────────────────────────
-- New autoincrement ids — after_transaction_insert trigger adjusts stock.

INSERT INTO transactions
    (product_id, txn_type, quantity_change, unit_mode, reference_no, note)
VALUES
    (128, 'OUT', -24, 'unit', 'IV6900394-7', 'BSN ขาย');

INSERT INTO transactions
    (product_id, txn_type, quantity_change, unit_mode, reference_no, note)
VALUES
    (815, 'OUT', -1, 'unit', 'IV6900391-2', 'BSN ขาย');

INSERT INTO transactions
    (product_id, txn_type, quantity_change, unit_mode, reference_no, note)
VALUES
    (436, 'OUT', -5, 'unit', 'IV6900392-1', 'BSN ขาย');

COMMIT;

PRAGMA foreign_keys = ON;

DELETE FROM applied_migrations WHERE filename = '090_remove_split_code_ghost_rows.sql';
