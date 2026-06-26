BEGIN;
ALTER TABLE leave_requests DROP COLUMN approved_by;
ALTER TABLE leave_requests DROP COLUMN approved_at;
DELETE FROM applied_migrations WHERE filename='118_leave_approval_columns.sql';
COMMIT;
