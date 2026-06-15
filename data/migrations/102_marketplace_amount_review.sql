-- 102_marketplace_amount_review.sql
-- A manager's acknowledgement that an order's billed≠payout discrepancy has been
-- checked and is OK (e.g. a legitimate Shopee fee adjustment) — so it stops
-- showing as a yellow "ยอดต่าง" row on the reconciliation page.
--
-- Kept in its OWN table (not on marketplace_order_invoice, whose 'auto' rows are
-- rebuilt on every re-import) so the acknowledgement survives re-matching. It is
-- tied to the specific (doc_base, d_bill) reviewed: if the match or the amount
-- later changes, the acknowledgement no longer applies and the row re-flags.

BEGIN;

CREATE TABLE IF NOT EXISTS marketplace_amount_review (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT    NOT NULL,
    order_sn     TEXT    NOT NULL,
    doc_base     TEXT    NOT NULL,          -- the invoice that was reviewed
    d_bill       REAL    NOT NULL,          -- billed − payout at review time
    note         TEXT,
    reviewed_by  TEXT,
    reviewed_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, order_sn)
);

COMMIT;
