-- 161_ar_outstanding_due_date.sql (filename kept for the number already claimed)
-- Doc-level AR attributes that ARTRN has always carried and the DBF adapter
-- was dropping: the due date, the credit terms that produced it, and the
-- billing note (ใบวางบิล) the invoice sits on.
--
-- Why it matters, measured on the 2026-08-17 export against the 170 invoices
-- /ar would chase: every one has a DUEDAT, and 18 of them (฿116,850, 29% of
-- collectable AR) were NOT YET DUE. cashflow.ar_aging buckets by document
-- date, so those were aged as if already owed and would have put the team on
-- the phone to customers whose credit terms had not expired.
--
-- On express_ar_outstanding rather than express_invoice_refs on purpose: that
-- table is built inside the 60-day ledger window and covered only 132 of the
-- 170 (measured), while the snapshot is windowless by design and is therefore
-- the only place every open document appears. It is also where the consumers
-- (cashflow.ar_aging, ar_followup) already read.
--
-- Nullable ON PURPOSE: rows written before this migration have no values, and
-- Express leaves PAYTRM blank on cash documents. NULL means "not known from
-- the file", deliberately distinct from 0 days.
--
-- Apply: restart the app (database.py::init_db() auto-applies).
-- Rollback: data/migrations/161_invoice_refs_due_date.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

ALTER TABLE express_ar_outstanding ADD COLUMN due_date_iso TEXT;
ALTER TABLE express_ar_outstanding ADD COLUMN pay_terms    INTEGER;
ALTER TABLE express_ar_outstanding ADD COLUMN bill_no      TEXT;

-- Aging and the dunning list both scan the latest snapshot by due date.
CREATE INDEX IF NOT EXISTS idx_ar_outstanding_due
    ON express_ar_outstanding(entity, snapshot_date_iso, due_date_iso);

COMMIT;
