-- 166_express_import_watermark.sql
-- A dedicated freshness watermark for the destructive Express DBF import.
--
-- Why (Codex P1, 2026-08-18): the stale-zip guard read its watermark from
-- MAX(snapshot_date_iso) on the AR/AP outstanding tables. Those snapshots are
-- deliberately ISOLATED — a snapshot may refuse while the ledger and payment
-- import commit — so a day where the money went in but the snapshot did not
-- left the watermark at yesterday. Yesterday's zip then read as same-date and
-- was accepted, and its ARRCPIT can DELETE paid_invoices links written since.
-- That is precisely the failure the guard exists to prevent.
--
-- It also closes a check-then-act race: the comparison ran before a 13-18s
-- import with nothing shared between gunicorn's two workers, so an older and a
-- newer upload could both pass against the same prior state. The route now
-- claims this row (BEGIN IMMEDIATE, compare, advance, commit) BEFORE importing,
-- so the second worker sees the advanced value.
--
-- Advancing before the import is deliberate: if the import then fails, a retry
-- of the SAME zip is still accepted (the comparison is strictly-older) while an
-- OLDER one stays refused, which is the correct answer either way.
--
-- Seeded from the existing snapshot dates so today's prod state is not treated
-- as "never imported" on first boot.
--
-- Apply: restart the app (database.py::init_db() auto-applies).
-- Rollback: data/migrations/166_express_import_watermark.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

CREATE TABLE IF NOT EXISTS express_import_watermark (
    entity           TEXT PRIMARY KEY,
    last_export_date TEXT,                 -- ISO date of the newest zip accepted
    last_export_at   TEXT,                 -- its full stamp, for support questions
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO express_import_watermark (entity, last_export_date, last_export_at)
SELECT 'BSN',
       MAX(d),
       NULL
  FROM (SELECT MAX(snapshot_date_iso) d FROM express_ar_outstanding WHERE entity='BSN'
        UNION ALL
        SELECT MAX(snapshot_date_iso) d FROM express_ap_outstanding WHERE entity='BSN')
 WHERE (SELECT COUNT(*) FROM express_import_watermark WHERE entity='BSN') = 0;

COMMIT;
