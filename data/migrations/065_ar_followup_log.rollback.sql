-- 065_ar_followup_log.rollback.sql
BEGIN;
DROP INDEX IF EXISTS idx_ar_followup_log_date;
DROP INDEX IF EXISTS idx_ar_followup_next_action;
DROP INDEX IF EXISTS idx_ar_followup_customer;
DROP TABLE IF EXISTS ar_followup_log;
DELETE FROM applied_migrations WHERE filename = '065_ar_followup_log.sql';
COMMIT;
