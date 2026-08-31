-- Rollback for 179_advance_cap_warn_pct.sql — remove the seeded row only.
-- Safe to re-run: DELETE ... WHERE key=... is naturally idempotent.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DELETE FROM hr_config WHERE key = 'advance_warn_pct';

DELETE FROM applied_migrations WHERE filename = '179_advance_cap_warn_pct.sql';

COMMIT;
