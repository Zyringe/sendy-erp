-- ============================================================================
-- Migration 080 — stock_levels integrity triggers on transactions UPDATE/DELETE
--
-- Background
--   `after_transaction_insert` (canonical · pre-mig-023) keeps stock_levels in
--   sync when transactions rows are inserted. There were no matching UPDATE
--   or DELETE business triggers — the "append-only ledger" convention from
--   mig 070's audit triggers implicitly relied on no one ever mutating or
--   deleting transactions rows.
--
--   Mig 079 (2026-05-25) added audit triggers acknowledging UPDATE/DELETE DO
--   happen in practice. Mig 080 closes the data-integrity gap: when a
--   transactions row is updated or deleted, stock_levels now reconciles
--   automatically (mirroring the INSERT trigger's accounting).
--
-- Coverage
--   * after_transaction_update — fires only when product_id or quantity_change
--     change (no-op note-only UPDATEs do NOT touch stock). Handles both:
--       (a) same product, quantity_change changed → delta-adjust
--       (b) product_id changed (with or without quantity change) → move stock
--   * after_transaction_delete — reverses OLD.quantity_change on OLD.product_id.
--
-- Out of scope
--   This migration does NOT recompute historical drift. If `stock_levels` is
--   currently out-of-sync with `SUM(quantity_change) GROUP BY product_id`
--   (e.g. legacy cleanups before mig 080 was deployed), use the documented
--   manual recovery: DELETE stock_levels WHERE product_id=? then INSERT
--   recalculated total. Recomputation is deliberately deferred so a forward
--   migration cannot silently rewrite stock numbers.
-- ============================================================================

BEGIN;

CREATE TRIGGER after_transaction_update
AFTER UPDATE ON transactions
WHEN (OLD.product_id      IS NOT NEW.product_id
   OR OLD.quantity_change IS NOT NEW.quantity_change)
BEGIN
    -- Reverse OLD effect on OLD product
    UPDATE stock_levels
       SET quantity = quantity - OLD.quantity_change
     WHERE product_id = OLD.product_id;

    -- Ensure row exists for NEW product (no-op if same as OLD)
    INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0)
        ON CONFLICT(product_id) DO NOTHING;

    -- Apply NEW effect on NEW product
    UPDATE stock_levels
       SET quantity = quantity + NEW.quantity_change
     WHERE product_id = NEW.product_id;
END;

CREATE TRIGGER after_transaction_delete
AFTER DELETE ON transactions
BEGIN
    UPDATE stock_levels
       SET quantity = quantity - OLD.quantity_change
     WHERE product_id = OLD.product_id;
END;

COMMIT;
