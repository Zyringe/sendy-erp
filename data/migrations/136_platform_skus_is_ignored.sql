-- 136: platform_skus.is_ignored — closes out marketplace SKU rows that have no
-- real Sendy product (zero-unmapped project 2026-07-14). Mirrors
-- ecommerce_listings.is_ignored: the row keeps its full audit trail but leaves
-- the /ecommerce tabs, summary counts, and mapping export. The import upsert
-- never touches this column (safe-upsert contract), so re-imports cannot
-- resurrect an ignored row into the worklist.
ALTER TABLE platform_skus ADD COLUMN is_ignored INTEGER NOT NULL DEFAULT 0;
