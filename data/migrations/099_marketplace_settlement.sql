-- 099_marketplace_settlement.sql
-- Add settlement tracking columns to marketplace_orders.
-- actual_payout = amount Shopee actually transferred (from Income Transfer file col 37).
-- settled_at    = date Shopee transferred (col 11 in Income Transfer).
-- settlement_source = filename of the Income Transfer file, for traceability.

ALTER TABLE marketplace_orders ADD COLUMN actual_payout REAL;
ALTER TABLE marketplace_orders ADD COLUMN settled_at TEXT;
ALTER TABLE marketplace_orders ADD COLUMN settlement_source TEXT;
