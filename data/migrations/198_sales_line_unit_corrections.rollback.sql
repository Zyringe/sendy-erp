-- Rollback 198. Drops every correction row with the table. Cancel the active
-- corrections first (line_unit_correction.cancel): dropping the table leaves
-- their corrected units and offset ADJUST rows in place with nothing that
-- remembers them, and the next daily zip would then replace each corrected
-- line with Express's unit while its offset stays in the ledger.
DROP TRIGGER IF EXISTS audit_sales_line_unit_corrections_insert;
DROP TRIGGER IF EXISTS audit_sales_line_unit_corrections_update;
DROP TRIGGER IF EXISTS audit_sales_line_unit_corrections_delete;
DROP INDEX IF EXISTS idx_sales_line_unit_corrections_active;
DROP TABLE IF EXISTS sales_line_unit_corrections;
