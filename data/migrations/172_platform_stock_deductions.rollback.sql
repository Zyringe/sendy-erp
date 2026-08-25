-- Rollback 172. The table is pure provenance written after the fact: dropping
-- it loses the ability to reverse future deductions but does not change any
-- stock figure, so no compensating write is needed.
DROP INDEX IF EXISTS idx_platform_stock_deductions_sku;
DROP TABLE IF EXISTS platform_stock_deductions;
