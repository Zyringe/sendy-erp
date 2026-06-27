-- data/migrations/119_salary_advance_from_account.sql
-- Groundwork for future cashbook auto-posting of advances: which cash/bank
-- account the advance was paid FROM. Nullable — Phase 7 stores it but does NOT
-- post to cashbook (cashbook is Excel-loaded; auto-posting later must not
-- double-count). NOTE: do NOT self-insert into applied_migrations.
BEGIN;
ALTER TABLE salary_advances
    ADD COLUMN from_account_id INTEGER REFERENCES cashbook_accounts(id);
COMMIT;
