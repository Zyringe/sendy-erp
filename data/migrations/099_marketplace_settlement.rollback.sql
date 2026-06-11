-- 099_marketplace_settlement.rollback.sql
-- SQLite cannot DROP columns — rebuild the table without the 3 new columns.
CREATE TABLE marketplace_orders_bk AS SELECT
    id, platform, order_sn, status, buyer_name, buyer_phone,
    ship_address, order_date, paid_date, item_total, marketplace_fee,
    payout, currency, source_file, raw_json, first_synced_at, last_synced_at
FROM marketplace_orders;

DROP TABLE marketplace_orders;

ALTER TABLE marketplace_orders_bk RENAME TO marketplace_orders;
