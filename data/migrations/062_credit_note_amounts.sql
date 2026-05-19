-- 062_credit_note_amounts.sql
-- Authoritative per-SR credited value, parsed from the standalone ใบลดหนี้
-- (SR / credit-note) master line ("รวมทั้งสิ้น" column = post-doc-discount,
-- post-VAT-policy total the customer is actually credited).
--
-- WHY THIS EXISTS
-- ---------------
-- payments_alloc's `cn` CTE previously netted the SR against the original
-- invoice using `SUM(sales_transactions.net)` over the SR's *detail* lines.
-- That `net` is the line-item amount BEFORE the SR master's document-level
-- discount (e.g. SR6900009 has detail net 2340.00 but the master credits
-- only 2293.20 after a 2% doc discount). Using the detail-line sum
-- over-credits the invoice, which then reads as fantasy "overpaid" — the
-- false ฿105,604 customer-credit balance.
--
-- The ใบลดหนี้ master row's "รวมทั้งสิ้น" column is the single authoritative
-- credited figure (it already incorporates the doc discount + VAT policy).
-- parse_weekly._SR_MASTER_RE captures it as `total_amt`. This table caches
-- one row per SR document (doc_base, NOT line) so payments_alloc can join
-- ref_invoice → invoice doc_base and net the EXACT credited amount.
--
-- GRAIN: one row per SR doc_base (e.g. SR6900009). The standalone file is
--   cumulative; a multi-detail SR still has ONE master row / ONE credited
--   total, so doc_base is the natural unique key. UNIQUE(sr_doc_base) +
--   ON CONFLICT DO UPDATE makes the upsert idempotent across re-imports.
--
-- This table is ADDITIVE and READ-ONLY w.r.t. existing AR/cash data — it
-- never mutates sales_transactions / paid_invoices / received_payments.
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner records
--   every migration it executes automatically; a self-insert would
--   duplicate-key crash on boot (see migration 058/059/060 preamble).
--
-- Apply:    via database.py::run_pending_migrations (automatic on boot)
-- Rollback: 062_credit_note_amounts.rollback.sql

BEGIN;

CREATE TABLE IF NOT EXISTS credit_note_amounts (
    id              INTEGER PRIMARY KEY,
    sr_doc_base     TEXT    NOT NULL,
    ref_invoice     TEXT,
    credited_amount REAL    NOT NULL DEFAULT 0.0,
    sr_date_iso     TEXT,
    customer        TEXT,
    source          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(sr_doc_base)
);

CREATE INDEX IF NOT EXISTS idx_cna_ref_invoice ON credit_note_amounts(ref_invoice);

COMMIT;
