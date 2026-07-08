-- 132_express_invoice_refs.sql
-- Side table for the Express DBF sales adapter (express_dbf_source.py,
-- Phase 1 slice A of projects/express-integration/plan.md) to capture
-- ARTRN.YOUREF + ARTRNRM.REMARK per sales doc (doc_base = ARTRN.DOCNUM).
--
-- YOUREF is filled on ~99.8% of marketplace invoices (customers
-- หน้าร้านS/B/L) with the end-buyer's name + platform tag — it feeds the
-- SEPARATE marketplace-IV-matching work (#271/#272), which can key on
-- buyer-name instead of amount/date fuzzy-matching. Kept as a side table
-- rather than new columns on sales_transactions (plan.md §Phase 1) so this
-- migration touches no existing table.
--
-- Apply: restart the app (database.py::init_db() auto-applies).
-- Rollback: data/migrations/132_express_invoice_refs.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

CREATE TABLE IF NOT EXISTS express_invoice_refs (
    doc_base   TEXT PRIMARY KEY,
    youref     TEXT,
    remark     TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

COMMIT;
