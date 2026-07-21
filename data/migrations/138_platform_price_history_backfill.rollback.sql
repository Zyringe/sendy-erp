-- 138_platform_price_history_backfill.rollback.sql
-- Remove only the seed rows this migration inserted (identified by source tag).

BEGIN;

DELETE FROM platform_price_history
 WHERE source = 'backfill:campaign-2026-07 (best-effort)';

COMMIT;
