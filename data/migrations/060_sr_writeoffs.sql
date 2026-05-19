-- 060_sr_writeoffs.sql
-- Persistent write-off marker for unattributable SR (credit-note) docs.
--
-- Design rationale:
--   SR rows in sales_transactions may reference an invoice that predates the
--   ERP import cutoff (IV65…/IV66…/IV0000000/HS… etc.) or carry no
--   ref_invoice at all.  These SRs already net against nothing in
--   payments_alloc (the `cn` CTE skips NULL/'' refs, and a ref pointing to a
--   non-existent invoice simply finds no matching inv row).  This table gives
--   a clean persistent record of that bookkeeping status without touching any
--   existing AR/cash-flow math.
--
-- Grain: one row per sales_transactions doc_no (a single line item of an SR).
--   A multi-line SR (SR6700163 has 16 lines) produces one row per doc_no.
--   This matches the dedup key used throughout the BSN import pipeline.
--
-- Classification:
--   no_ref      — ref_invoice IS NULL or '' after backfill pass.
--   pre_system  — ref_invoice is non-empty but no sales_transactions row has
--                 doc_base = that ref_invoice (pre-cutoff or HS… origin).
--
-- SR rows whose ref_invoice DOES match a real in-system invoice (doc_base)
--   are NOT written off — they net correctly through payments_alloc and need
--   no marker here.
--
-- Dedup key: UNIQUE(sr_doc_no) — stable, matches the doc_no identity used in
--   the rest of the pipeline.  ON CONFLICT DO UPDATE makes populate idempotent.
--
-- NOTE: do NOT self-insert into applied_migrations here.  The runner
--   records every migration it executes automatically.

BEGIN;

CREATE TABLE IF NOT EXISTS sr_writeoffs (
    id              INTEGER PRIMARY KEY,
    sr_doc_base     TEXT    NOT NULL,
    sr_doc_no       TEXT    NOT NULL,
    reason          TEXT    NOT NULL CHECK(reason IN ('pre_system','no_ref')),
    ref_invoice_raw TEXT,
    net_amount      REAL    NOT NULL DEFAULT 0.0,
    customer        TEXT,
    sr_date_iso     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(sr_doc_no)
);

CREATE INDEX IF NOT EXISTS idx_srwo_doc_base ON sr_writeoffs(sr_doc_base);
CREATE INDEX IF NOT EXISTS idx_srwo_reason   ON sr_writeoffs(reason);

COMMIT;
