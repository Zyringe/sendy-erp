-- ============================================================================
-- Migration 088 — Express entity-tag AR + new AP outstanding table
--
-- Apply:    drop this file + 088_express_entity_tag_and_ap.rollback.sql into
--           data/migrations/ then restart sendy (runner auto-applies on init_db).
-- Rollback: 088_express_entity_tag_and_ap.rollback.sql (run manually, then
--           DELETE the applied_migrations row — the rollback does both).
--
-- Why
--   Express is becoming the authoritative source for BSN AR/AP. Today
--   `express_ar_outstanding` holds Sendai Trading (SD) data ONLY — 171 rows,
--   one snapshot (2026-04-30). We are about to import BSN snapshots alongside
--   SD into the same table, so rows must be entity-tagged to keep the two
--   companies' receivables separate. The existing SD-only `/express/ar`
--   dashboard reads MAX(snapshot_date_iso); without an entity column a BSN
--   import would clobber that view's result set.
--
--   There is no payables-side table yet. The Express "เจ้าหนี้คงค้าง" file is
--   structured: group ประเภทผู้จำหน่าย (supplier_type) → supplier name →
--   rows of [date, เอกสาร# (RR doc_no), เลขที่บิล (supplier invoice no),
--   ยอดในบิล (bill), ยอดชำระ (paid), ยอดคงค้าง (outstanding)]. We mirror the
--   AR table's batch/snapshot pattern so AP imports get the same shape.
--
-- What
--   1. ADD express_ar_outstanding.entity TEXT NOT NULL DEFAULT 'SD'.
--      The 171 existing rows backfill to 'SD' automatically via the DEFAULT —
--      they are the SD snapshot. New BSN imports must set entity='BSN'.
--   2. ADD index idx_express_ar_entity_snapshot (entity, snapshot_date_iso) so
--      entity-scoped MAX(snapshot_date_iso) lookups stay fast once both
--      entities coexist.
--   3. CREATE TABLE express_ap_outstanding (supplier/payables side), entity-
--      tagged, DEFAULT 'BSN' (the AP file is BSN's). Indexes mirror AR:
--      (entity, snapshot_date_iso) + supplier_id + doc_no.
--
-- How
--   Pure additive: ALTER TABLE ADD COLUMN (no table rebuild) + CREATE TABLE +
--   CREATE INDEX. No triggers or views reference express_ar_outstanding
--   (verified 2026-05-29: sqlite_master has zero trigger/view rows mentioning
--   it), so ADD COLUMN cannot drift any dependent object. No backfill UPDATE
--   needed — the DEFAULT handles the existing rows.
--
-- FK hazard: same recipe as recent migs — PRAGMA foreign_keys = OFF before
-- BEGIN. This mig adds no FK and rebuilds no table, but the pragma keeps the
-- ceremony consistent and harmless.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- 1) Entity-tag the AR table. Existing 171 SD rows backfill to 'SD' via DEFAULT.
ALTER TABLE express_ar_outstanding
    ADD COLUMN entity TEXT NOT NULL DEFAULT 'SD';

-- 2) Index for entity-scoped snapshot lookups (mirrors idx_express_ar_snapshot
--    but leads with entity so per-company MAX(snapshot_date_iso) is covered).
CREATE INDEX idx_express_ar_entity_snapshot
    ON express_ar_outstanding(entity, snapshot_date_iso);

-- 3) New payables-side table. Mirrors the AR batch/snapshot pattern.
CREATE TABLE express_ap_outstanding (
    id                  INTEGER PRIMARY KEY,
    batch_id            INTEGER,
    entity              TEXT NOT NULL DEFAULT 'BSN',
    snapshot_date_iso   TEXT,
    supplier_type       TEXT,                 -- ประเภทผู้จำหน่าย (group header)
    supplier_name       TEXT,
    supplier_code       TEXT,
    supplier_id         INTEGER,
    doc_no              TEXT,                 -- เอกสาร# (RR receive doc)
    supplier_invoice_no TEXT,                 -- เลขที่บิล (supplier's bill no)
    doc_date_iso        TEXT,                 -- row date
    bill_amount         REAL,                 -- ยอดในบิล
    paid_amount         REAL,                 -- ยอดชำระ
    outstanding_amount  REAL,                 -- ยอดคงค้าง
    created_at          TEXT DEFAULT (datetime('now'))
);

-- 4) Indexes mirroring the AR table (snapshot/customer/doc → snapshot/supplier/doc).
CREATE INDEX idx_express_ap_entity_snapshot
    ON express_ap_outstanding(entity, snapshot_date_iso);
CREATE INDEX idx_express_ap_supplier
    ON express_ap_outstanding(supplier_id);
CREATE INDEX idx_express_ap_doc
    ON express_ap_outstanding(doc_no);

COMMIT;

PRAGMA foreign_keys = ON;
