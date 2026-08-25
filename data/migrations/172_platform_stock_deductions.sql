-- 172 · platform_stock_deductions — make the marketplace deduction reversible
--
-- _sync_bsn_to_stock decrements platform_skus.stock when a marketplace sale
-- first syncs and records NOTHING about it, so the deduction can never be
-- undone. PR #424 stopped it being applied twice; this table is what lets it
-- be taken BACK when the sale it came from is corrected or removed.
--
-- Per (source row, listing), not per source row: the deduction walks a
-- product's listings ORDER BY stock DESC and clamps each write at
-- MAX(0, stock - n), so neither the intended amount nor a per-row total can
-- be reversed correctly. `units` is what the write ACTUALLY moved.
--
-- SIGNED. `units` is the net amount this source row REMOVED from that
-- listing: positive for a sale, NEGATIVE for a customer return (SR), which
-- puts units back because Shopee and Lazada restock a returned item
-- themselves once the refund completes (Put, 2026-08-25) and this column
-- mirrors their number. One sign convention means reversal is always
-- `stock = stock + units`, and a corrected or deleted RETURN line is as
-- reversible as a corrected sale — recording credits unsigned would have
-- left a fresh un-reversible write inside the change that exists to remove
-- exactly that.
--
-- No backfill. Deductions applied before this migration have no provenance and
-- stay irreversible — exactly today's position, so nothing regresses.
--
-- Drop-first so a hand-applied rehearsal can be re-run (see
-- .claude/rules/erp-engineering-discipline.md).

DROP INDEX IF EXISTS idx_platform_stock_deductions_sku;

CREATE TABLE IF NOT EXISTS platform_stock_deductions (
    source_table    TEXT    NOT NULL CHECK(source_table IN ('sales_transactions')),
    source_id       INTEGER NOT NULL,
    platform_sku_id INTEGER NOT NULL REFERENCES platform_skus(id),
    units           INTEGER NOT NULL CHECK(units <> 0),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (source_table, source_id, platform_sku_id)
);

-- Reversal reads by source row (the PK covers that). This index serves the
-- other direction: "what is still charged against this listing", which is how
-- an operator or a future audit checks a listing's estimate.
CREATE INDEX idx_platform_stock_deductions_sku
    ON platform_stock_deductions (platform_sku_id);
