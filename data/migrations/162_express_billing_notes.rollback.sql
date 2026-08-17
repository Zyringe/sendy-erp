-- Rollback for 162_express_billing_notes.sql — a new table, so it just goes.
-- express_ar_outstanding.bill_no (mig 161) survives; it is a plain text column
-- and never had a foreign key to this table.
BEGIN;
DROP INDEX IF EXISTS idx_billing_notes_customer;
DROP TABLE IF EXISTS express_billing_notes;
COMMIT;
