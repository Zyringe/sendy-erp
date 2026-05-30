-- Migration 092 — stop stock_levels.quantity from accumulating IEEE-754 noise.
--
-- stock_levels.quantity is REAL. The mig-080 triggers maintain it with
--   quantity = quantity + NEW.quantity_change
-- Many movements are 0.1-aligned (e.g. a product sold by กิโลกรัม but in แพ็ค
-- with ratio 0.1 → -0.1, -0.2, -0.5 per sale). 0.1 is not exactly representable
-- in binary double, so summing ~40 such rows drifted a product to
-- 23.399999999999984 instead of 23.4 (and could make a true 0 read as -1e-14,
-- a phantom negative). Fix: ROUND the trigger result to 4 dp. The finest real
-- movement is 0.1, so 4 dp is lossless for real quantities while erasing the
-- <1e-13 float noise. Triggers are otherwise byte-identical to mig 080.
--
-- Also one-time reconcile the handful of already-noisy rows. This UPDATE fires
-- NO trigger (triggers are on `transactions`, not `stock_levels`), so there is
-- no double-count; rounding does not change real quantities and is idempotent.

DROP TRIGGER IF EXISTS after_transaction_insert;
CREATE TRIGGER after_transaction_insert
    AFTER INSERT ON transactions
    BEGIN
        INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0)
            ON CONFLICT(product_id) DO NOTHING;
        UPDATE stock_levels
           SET quantity = ROUND(quantity + NEW.quantity_change, 4)
         WHERE product_id = NEW.product_id;
    END;

DROP TRIGGER IF EXISTS after_transaction_update;
CREATE TRIGGER after_transaction_update
AFTER UPDATE ON transactions
WHEN (OLD.product_id      IS NOT NEW.product_id
   OR OLD.quantity_change IS NOT NEW.quantity_change)
BEGIN
    -- Reverse OLD effect on OLD product
    UPDATE stock_levels
       SET quantity = ROUND(quantity - OLD.quantity_change, 4)
     WHERE product_id = OLD.product_id;

    -- Ensure row exists for NEW product (no-op if same as OLD)
    INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0)
        ON CONFLICT(product_id) DO NOTHING;

    -- Apply NEW effect on NEW product
    UPDATE stock_levels
       SET quantity = ROUND(quantity + NEW.quantity_change, 4)
     WHERE product_id = NEW.product_id;
END;

DROP TRIGGER IF EXISTS after_transaction_delete;
CREATE TRIGGER after_transaction_delete
AFTER DELETE ON transactions
BEGIN
    UPDATE stock_levels
       SET quantity = ROUND(quantity - OLD.quantity_change, 4)
     WHERE product_id = OLD.product_id;
END;

-- One-time cleanup of rows that already carry float noise.
UPDATE stock_levels SET quantity = ROUND(quantity, 4) WHERE quantity <> ROUND(quantity, 4);
