-- 012_purchase_orders.rollback.sql
-- Reverses 012_purchase_orders.sql.
--
-- Drops triggers + tables in FK-safe order: po_receipts (depends on
-- lines), purchase_order_lines (depends on PO), purchase_orders
-- (depends on companies + suppliers), then po_sequences (independent).
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/012_purchase_orders.rollback.sql

BEGIN;

DROP TRIGGER IF EXISTS audit_po_receipts_delete;
DROP TRIGGER IF EXISTS audit_po_receipts_update;
DROP TRIGGER IF EXISTS audit_po_receipts_insert;
DROP TRIGGER IF EXISTS audit_purchase_order_lines_delete;
DROP TRIGGER IF EXISTS audit_purchase_order_lines_update;
DROP TRIGGER IF EXISTS audit_purchase_order_lines_insert;
DROP TRIGGER IF EXISTS audit_purchase_orders_delete;
DROP TRIGGER IF EXISTS audit_purchase_orders_update;
DROP TRIGGER IF EXISTS audit_purchase_orders_insert;

DROP INDEX IF EXISTS idx_por_date;
DROP INDEX IF EXISTS idx_por_line;
DROP TABLE IF EXISTS po_receipts;

DROP INDEX IF EXISTS idx_pol_product;
DROP INDEX IF EXISTS idx_pol_po;
DROP TABLE IF EXISTS purchase_order_lines;

DROP INDEX IF EXISTS idx_po_order_date;
DROP INDEX IF EXISTS idx_po_status;
DROP INDEX IF EXISTS idx_po_supplier;
DROP INDEX IF EXISTS idx_po_company;
DROP TABLE IF EXISTS purchase_orders;

DROP TABLE IF EXISTS po_sequences;

COMMIT;
