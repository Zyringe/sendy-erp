-- ============================================================================
-- 179 — Advance cap warning: hr_config seed for advance_warn_pct.
--
-- See projects/payroll-carry-forward/plan.md, "P2 — advance cap warning".
-- Put's ruling: warn at 50% of what a month can pay, and require an extra
-- confirm when a salary advance entry would exceed what is actually
-- collectable this month (not a hard block — Put is the only person who
-- keys advances, so there is no second approver to gate on).
--
-- Adds ONE hr_config row: advance_warn_pct = 0.5. hr.py::_load_config's code
-- default for this key MUST match this value exactly — `sso_max_base`
-- already drifted this way (code default 15000, prod holds 17500; plan.md
-- "Nit"), and this migration exists precisely to not repeat that.
-- tests/test_advance_cap_warning.py::test_advance_warn_pct_code_default_matches_migration
-- pins the two together.
--
-- INSERT OR IGNORE, not drop-first: hr_config.key is TEXT PRIMARY KEY and
-- this migration only ever adds a row, never redefines an existing one, so
-- the migration is naturally re-runnable without a DROP.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

INSERT OR IGNORE INTO hr_config (key, value, note) VALUES (
    'advance_warn_pct', '0.5',
    'สัดส่วนของฐานเงินเดือนเดือนนั้น (ก่อนหักอื่นๆ) ที่ทำให้ขึ้นแถบเตือนสีเหลืองตอนคีย์เบิกล่วงหน้า — ไม่บล็อก แค่เตือน (Put 2026-08-31)'
);

INSERT OR IGNORE INTO applied_migrations (filename, applied_by)
VALUES ('179_advance_cap_warn_pct.sql', 'auto');

COMMIT;
