-- data/migrations/129_cashbook_commission_writeback.sql
--
-- Phase 3 of the cashbook /new overhaul (projects/cashbook-entry-reconcile/
-- plan.md, decisions C1-C4/C7, D3, scrutiny findings #1/#6). Commission
-- payouts to IN-ENGINE salespersons become the SOURCE OF TRUTH on
-- /commission: recording a payout there auto-posts + locks a
-- `จ่ายค่าคอมมิชชั่น` cashbook_transactions row (mirrors salary, ADR 0006, and
-- the Phase 2 advance write-back). Off-system reps stay hybrid — manual entry
-- in the cashbook remains their home for those (plan.md ADR 0008).
--
-- (Numbered 129 — 127 was taken by a sibling branch (product_labels), 128 by
-- Phase 2 of this same plan; see the fetch-before-shared-branches rule.)
--
--   commission_payout_id — the "linked & locked row" FK, symmetric with
--                          payroll_item_id (salary) / salary_advance_id
--                          (advance). UNIQUE (one cashbook row <-> one
--                          payout); nullable, so every non-commission row
--                          keeps NULL and SQLite treats those as distinct,
--                          same as the existing idx_cashbook_txn_payroll_item
--                          / idx_cashbook_txn_salary_advance indexes.
--   salespersons.real_name — the D3 gate: a real-name alias (เจียรนัย,
--                          ทวีเกียรติ) shown in the commission recipient
--                          picker + used to detect an in-engine manual
--                          cashbook entry, so Put doesn't hand-type a
--                          commission the engine already auto-posts
--                          (the exact double-book D3 confirmed as real).
--   category           — `จ่ายค่าคอมมิชชั่น` already exists (setup, from an
--                          earlier migration); re-INSERT OR IGNORE here only
--                          so a from-empty schema.sql build (which backfills
--                          129 as already-applied) still has it if it's ever
--                          missing from schema.sql.
--
-- Idempotent + prod-safe. On a from-empty (schema.sql baseline) build this is
-- bootstrap-backfilled as already-applied (same as mig 125/126/128) — see
-- run_pending_migrations() in database.py.
--
-- NOTE: data/schema.sql was NOT regenerated for this migration. The live DB
-- dump_schema.py would read from also carries an UNMERGED sibling branch's
-- product_labels tables (mig 127, applied on the shared dev DB but not yet on
-- main) — regenerating now would leak those into the fresh-DB baseline. A
-- brand-new clone is therefore missing this migration's columns until
-- schema.sql is regenerated post-merge (the same pre-existing gap already
-- true of migration 127/128 relative to this worktree's schema.sql; not a
-- new problem introduced here).

BEGIN;

ALTER TABLE cashbook_transactions
    ADD COLUMN commission_payout_id INTEGER REFERENCES commission_payouts(id);

CREATE UNIQUE INDEX idx_cashbook_txn_commission_payout
    ON cashbook_transactions(commission_payout_id);

ALTER TABLE salespersons ADD COLUMN real_name TEXT;

UPDATE salespersons SET real_name = 'เจียรนัย'   WHERE code IN ('06', '06-L');
UPDATE salespersons SET real_name = 'ทวีเกียรติ' WHERE code = '03';

INSERT OR IGNORE INTO cashbook_categories (name, direction, source, sort_order)
    VALUES ('จ่ายค่าคอมมิชชั่น', 'expense', 'setup', 100);

COMMIT;
