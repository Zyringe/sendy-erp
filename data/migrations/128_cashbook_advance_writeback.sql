-- data/migrations/128_cashbook_advance_writeback.sql
--
-- Phase 2 of the cashbook /new overhaul (projects/cashbook-entry-reconcile/
-- plan.md, decision C5 + scrutiny finding #2). The cashbook becomes the SOURCE
-- OF TRUTH for salary advances: saving a `เงินเดือน (เบิกล่วงหน้า)` row on
-- /cashbook/new inserts BOTH a cashbook_transactions row and a salary_advances
-- row in one commit, linked by salary_advance_id. /hr/advances becomes a
-- read-only mirror.
--
-- (Numbered 128, not 127 — a sibling branch used 127_product_labels; see the
-- fetch-before-shared-branches rule.)
--
--   salary_advance_id  — the "linked & locked row" FK, symmetric with
--                        payroll_item_id (salary). UNIQUE (one cashbook row
--                        <-> one advance); nullable, so every non-advance row
--                        keeps its NULL and SQLite treats those NULLs as
--                        distinct (multiple allowed), same as the existing
--                        idx_cashbook_txn_payroll_item on payroll_item_id.
--   category           — `เงินเดือน (เบิกล่วงหน้า)` (expense, source='setup');
--                        INSERT OR IGNORE keys off UNIQUE(name, direction) so a
--                        re-run is a no-op.
--
-- Idempotent + prod-safe. On a from-empty (schema.sql baseline) build this is
-- bootstrap-backfilled as already-applied (same as mig 125/126).

BEGIN;

ALTER TABLE cashbook_transactions
    ADD COLUMN salary_advance_id INTEGER REFERENCES salary_advances(id);

CREATE UNIQUE INDEX idx_cashbook_txn_salary_advance
    ON cashbook_transactions(salary_advance_id);

INSERT OR IGNORE INTO cashbook_categories (name, direction, source, sort_order)
    VALUES ('เงินเดือน (เบิกล่วงหน้า)', 'expense', 'setup', 100);

COMMIT;
