-- 059_credit_note_imports.sql
-- Side table for SR rows parsed from the standalone ใบลดหนี้ file that
-- have NO matching row in sales_transactions (doc_no not present in DB).
--
-- Design rationale:
--   All 270 entries in the May-2026 cumulative file already exist in
--   sales_transactions via prior weekly BSN imports.  The table exists as
--   a safety net for future imports: any genuinely-new SR that does not
--   yet appear in sales_transactions is recorded here rather than
--   force-inserted into sales_transactions with potentially incomplete
--   weekly-row semantics (product_id=NULL, no unit normalization, no
--   stock sync).
--
-- Fields mirror the parse_weekly.parse_credit_notes() output dict so the
-- raw parsed values are preserved verbatim (no interpretation beyond the
-- parser).  product_id is left for a future mapping step.
--
-- Dedup key: UNIQUE(doc_no) — the natural line-level identity emitted by
-- the parser (doc_base + seq, e.g. "SR6700009-2").
--
-- NOTE: do NOT self-insert into applied_migrations here.  The runner
-- records every migration it executes automatically.

BEGIN;

CREATE TABLE IF NOT EXISTS credit_note_imports (
    id               INTEGER PRIMARY KEY,
    doc_no           TEXT    NOT NULL,          -- e.g. "SR6700009-2"
    doc_base         TEXT    NOT NULL,          -- e.g. "SR6700009"
    date_iso         TEXT    NOT NULL,
    customer         TEXT,
    salesperson      TEXT,
    ref_invoice      TEXT,
    ref_invoice_line TEXT,
    vat_type         INTEGER DEFAULT 1,
    bsn_code         TEXT,
    product_name_raw TEXT,
    qty              REAL    DEFAULT 0,
    unit             TEXT,
    unit_price       REAL    DEFAULT 0,
    discount         TEXT,
    total            REAL    DEFAULT 0,
    net              REAL    DEFAULT 0,
    cancelled        INTEGER DEFAULT 0,
    imported_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(doc_no)
);

CREATE INDEX IF NOT EXISTS idx_cni_doc_base  ON credit_note_imports(doc_base);
CREATE INDEX IF NOT EXISTS idx_cni_ref_inv   ON credit_note_imports(ref_invoice);

COMMIT;
