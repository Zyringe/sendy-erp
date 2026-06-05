-- Migration 095 — durable AR write-off ledger.
--
-- Records bad-debt write-offs and credit write-backs that the team/accountant
-- has DECIDED on, so the canonical collectable-AR figure (cashflow.BSN_AR_PREDICATE,
-- shared by ar_aging / customer_ranking / get_customer_debt_summary / the dunning
-- page) excludes them PERMANENTLY — even after the next ลูกหนี้คงค้าง import does
-- its DELETE+INSERT on express_ar_outstanding.
--
-- WHY a separate table (not a column on express_ar_outstanding): that snapshot
-- table is fully replaced on every import (scripts/import_express.run_import does
-- DELETE+INSERT per entity/snapshot_date). A flag there would be wiped on the
-- next import. ar_writeoffs is keyed on doc_no and survives re-imports; the AR
-- queries LEFT-anti-join it by doc_no so a written-off doc stays excluded across
-- snapshots.
--
-- type: 'expense'   = ตัดหนี้สูญ (positive receivable → Dr bad-debt expense)
--       'writeback' = กลับรายการเครดิต → รายได้อื่น (negative/credit balance → Cr other income)
-- amount is the snapshot outstanding at decision time (signed: + for expense,
-- − for writeback), kept for audit/reconciliation; the EXCLUSION is by doc_no.
--
-- Purely additive (1 table + indexes). Nothing existing is touched.

BEGIN;

CREATE TABLE IF NOT EXISTS ar_writeoffs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no         TEXT    NOT NULL,            -- the express_ar_outstanding doc_no written off
    customer_code  TEXT,                        -- '01อ35' etc (for grouping; nullable for legacy)
    customer_name  TEXT,
    amount         REAL    NOT NULL DEFAULT 0,  -- signed snapshot outstanding at decision time
    type           TEXT    NOT NULL CHECK(type IN ('expense','writeback')),
    writeoff_date  TEXT    NOT NULL,            -- ISO date the decision was recorded
    reason         TEXT,                        -- e.g. 'legacy 2014 dead account', 'Put 2026-06-05'
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(doc_no)                              -- one write-off decision per doc
);

CREATE INDEX IF NOT EXISTS idx_ar_writeoffs_doc      ON ar_writeoffs(doc_no);
CREATE INDEX IF NOT EXISTS idx_ar_writeoffs_customer ON ar_writeoffs(customer_code);

-- NB: the migration runner records this file in applied_migrations (with
-- sha256 + duration_ms) — do NOT self-insert here (convention since mig 092-094).

COMMIT;
