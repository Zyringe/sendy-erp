-- 123_cashbook_manual_entry.sql
-- Groundwork for manual cashbook entry + salary pay-event posting (Phase 1 of
-- the cashbook-manual-entry plan; see sendy_erp/docs/adr/0005-*.md, 0006-*.md).
--
-- NOTE: plan.md said "next number = 122", but by the time this shipped a
-- sibling in-flight worktree (feat/product-creation-consolidation) had already
-- claimed 122_product_created_via.sql against the shared local dev DB (not yet
-- merged to main). Renumbered to 123 to avoid a filename collision; the two
-- migrations are schema-orthogonal (different tables), so no functional clash.
--
-- cashbook_transactions: created_by (who entered/edited a manual row) +
--   payroll_run_id/payroll_item_id (link to the payroll item a salary
--   pay-event row was posted for; NULL = manual row).
-- employees.default_cashbook_account_id: per-employee default pay-from
--   account for the future salary pay-event UI (Phase 3/4); nullable,
--   overridable at pay time.
--
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).
BEGIN;

ALTER TABLE cashbook_transactions ADD COLUMN created_by      TEXT;
ALTER TABLE cashbook_transactions ADD COLUMN payroll_run_id  INTEGER REFERENCES payroll_runs(id);
ALTER TABLE cashbook_transactions ADD COLUMN payroll_item_id INTEGER REFERENCES payroll_items(id);

-- UNIQUE: one salary pay-event row per payroll item (DB-level idempotency —
-- the app's check-then-insert is racy under gunicorn -w 2 / double-submit).
-- SQLite treats NULLs as distinct, so the many manual rows (payroll_item_id
-- NULL) are unconstrained; only salary rows get one-per-item enforcement.
CREATE UNIQUE INDEX idx_cashbook_txn_payroll_item ON cashbook_transactions(payroll_item_id);

ALTER TABLE employees ADD COLUMN default_cashbook_account_id INTEGER REFERENCES cashbook_accounts(id);

-- Best-effort seed of the per-employee default pay-from account from history:
-- the account of that employee's most-recent hand-typed เงินเดือน row. The
-- historical rows tag by FIRST NAME (e.g. แต/สันติ/วฤทธิ์), so we match
-- user_category against nickname OR the first token of full_name OR full_name
-- (mirrors the emp-resolver precedence: first name is Put's everyday key). Only
-- non-transfer accounts are eligible (the pay-from dropdown excludes transfers).
-- Harmless if wrong — a UI default, overridable per-payment. On a fresh DB with
-- no cashbook rows (init_db test build) it changes 0 rows; the real seed effect
-- is verified against the live DB (see the migration test header note).
UPDATE employees
   SET default_cashbook_account_id = (
       SELECT t.account_id
         FROM cashbook_transactions t
         JOIN cashbook_accounts a ON a.id = t.account_id AND a.is_transfer = 0
        WHERE t.category = 'เงินเดือน'
          AND t.user_category IN (
              NULLIF(employees.nickname, ''),
              employees.full_name,
              CASE WHEN instr(employees.full_name, ' ') > 0
                   THEN substr(employees.full_name, 1, instr(employees.full_name, ' ') - 1)
                   ELSE employees.full_name END)
        ORDER BY t.txn_date DESC, t.id DESC
        LIMIT 1)
 WHERE default_cashbook_account_id IS NULL
   AND EXISTS (
       SELECT 1
         FROM cashbook_transactions t2
         JOIN cashbook_accounts a2 ON a2.id = t2.account_id AND a2.is_transfer = 0
        WHERE t2.category = 'เงินเดือน'
          AND t2.user_category IN (
              NULLIF(employees.nickname, ''),
              employees.full_name,
              CASE WHEN instr(employees.full_name, ' ') > 0
                   THEN substr(employees.full_name, 1, instr(employees.full_name, ' ') - 1)
                   ELSE employees.full_name END));

COMMIT;
