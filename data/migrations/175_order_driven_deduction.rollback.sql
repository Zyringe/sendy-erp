-- Rollback 175. Narrows platform_stock_deductions back to sales_transactions-
-- only and drops platform_skus.stock_as_of.
--
-- Rebuild from the CURRENT table (rename-safety rule 1), not a pre-migration
-- snapshot, so any 'sales_transactions' row written after the forward
-- migration shipped (the BSN walk keeps using this table) survives. Any
-- 'marketplace_orders' row is dropped — under the old regime that source
-- doesn't exist, so there is nothing for those rows to mean; dropping them
-- IS what rolling back means here.
DROP TABLE IF EXISTS platform_stock_deductions_old;
CREATE TABLE platform_stock_deductions_old (
    source_table    TEXT    NOT NULL CHECK(source_table IN ('sales_transactions')),
    source_id       INTEGER NOT NULL,
    platform_sku_id INTEGER NOT NULL REFERENCES platform_skus(id),
    units           INTEGER NOT NULL CHECK(units <> 0),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (source_table, source_id, platform_sku_id)
);
INSERT INTO platform_stock_deductions_old
    SELECT * FROM platform_stock_deductions WHERE source_table = 'sales_transactions';
DROP TABLE platform_stock_deductions;
ALTER TABLE platform_stock_deductions_old RENAME TO platform_stock_deductions;
DROP INDEX IF EXISTS idx_platform_stock_deductions_sku;
CREATE INDEX idx_platform_stock_deductions_sku ON platform_stock_deductions (platform_sku_id);

-- prod SQLite is modern (3.35+): DROP COLUMN directly, no table rebuild needed.
ALTER TABLE platform_skus DROP COLUMN stock_as_of;
