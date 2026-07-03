-- data/migrations/126_add_users_default_cashbook_account.sql
--
-- Phase 1a of the cashbook /new overhaul (projects/cashbook-entry-reconcile/
-- plan.md, decisions A1-A3): each LOGIN USER gets a default cashbook account
-- that /cashbook/new pre-selects (still changeable per row). Distinct from
-- employees.default_cashbook_account_id (mig 119's from_account_id lineage is
-- salary_advances, NOT this) — that is the "pay FROM" account for an
-- employee's salary; this is the "log in AS" account for a data-entry user.
--
-- Seed is prod-safe: looked up by cashbook_accounts.code + users.username, no
-- hardcoded ids (account ids/order can differ across environments). Idempotent
-- — reruns are no-ops once the FK already points at the right row. On a
-- from-empty build (schema.sql baseline) this migration is bootstrap-backfilled
-- as already-applied and the seed UPDATEs never execute (same as mig 125's
-- house-salesperson seed) — harmless, the column stays NULL until a real admin
-- sets it via /users.
--   admin (Put)       -> cashbook_accounts.code = '392'
--   mamaput (mother)  -> cashbook_accounts.code = 'ชฎามาศ'
--   s (Siang/กิติยา)  -> cashbook_accounts.code = 'กิติยา'

BEGIN;

ALTER TABLE users
    ADD COLUMN default_cashbook_account_id INTEGER REFERENCES cashbook_accounts(id);

UPDATE users SET default_cashbook_account_id =
    (SELECT id FROM cashbook_accounts WHERE code = '392')
    WHERE username = 'admin';

UPDATE users SET default_cashbook_account_id =
    (SELECT id FROM cashbook_accounts WHERE code = 'ชฎามาศ')
    WHERE username = 'mamaput';

UPDATE users SET default_cashbook_account_id =
    (SELECT id FROM cashbook_accounts WHERE code = 'กิติยา')
    WHERE username = 's';

COMMIT;
