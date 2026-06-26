BEGIN;
ALTER TABLE leave_requests ADD COLUMN approved_by TEXT;
ALTER TABLE leave_requests ADD COLUMN approved_at TEXT;
COMMIT;
