-- 101_marketplace_order_invoice.sql
-- Map a marketplace order (Shopee/Lazada) to its Express invoice (IV doc_base).
--
-- There is NO stored order_sn↔IV key: the team books each order as one Express
-- IV under customer codes Zหน้าร้าน (Shopee) / Lหน้าร้าน (Lazada) at the NET
-- payout amount. We link them by amount (IV net == order actual_payout) + a
-- date window, confirming the ambiguous ones by hand. This table stores the
-- resulting per-order link.
--
--   match_method 'auto'   = picked by marketplace_match.run_automatch (amount+date greedy match)
--                'manual' = a human confirmed it on the settlement page (never clobbered by auto)
--   confidence   'confident' = the order had a UNIQUE same-amount IV in the window (exact IV certain)
--                'probable'  = assigned among several same-amount IVs (amount certain; the exact IV
--                              may be swapped with an identical-amount sibling — immaterial for the
--                              reconciliation numbers, which are equal either way)
--                'review'    = FUZZY guess: nearest in-window invoice within a few baht (the team
--                              booked a slightly different amount than Shopee finally paid). Best-guess
--                              only — the exact invoice needs human confirmation via the picker.
--                'manual'    = human-confirmed (stored on link_manual)
--
-- UNIQUE(platform, order_sn): one IV per order (the 1 order = 1 IV booking model).

BEGIN;

CREATE TABLE IF NOT EXISTS marketplace_order_invoice (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT    NOT NULL,
    order_sn      TEXT    NOT NULL,
    doc_base      TEXT    NOT NULL,                  -- Express invoice, e.g. IV6900827
    customer_code TEXT,                              -- Zหน้าร้าน / Lหน้าร้าน
    match_method  TEXT    NOT NULL CHECK(match_method IN ('auto','manual')),
    confidence    TEXT    CHECK(confidence IN ('confident','probable','review','manual')),
    confirmed_by  TEXT,                              -- username for manual confirms
    confirmed_at  TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, order_sn)
);

CREATE INDEX IF NOT EXISTS idx_marketplace_order_invoice_doc_base
    ON marketplace_order_invoice(doc_base);

COMMIT;
