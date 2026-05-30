-- Rollback 092 — restore the mig-080 triggers without ROUND (data left clean;
-- the already-reconciled stock_levels values are correct and need no revert).

DROP TRIGGER IF EXISTS after_transaction_insert;
CREATE TRIGGER after_transaction_insert
    AFTER INSERT ON transactions
    BEGIN
        INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0)
            ON CONFLICT(product_id) DO NOTHING;
        UPDATE stock_levels
           SET quantity = quantity + NEW.quantity_change
         WHERE product_id = NEW.product_id;
    END;

DROP TRIGGER IF EXISTS after_transaction_update;
CREATE TRIGGER after_transaction_update
AFTER UPDATE ON transactions
WHEN (OLD.product_id      IS NOT NEW.product_id
   OR OLD.quantity_change IS NOT NEW.quantity_change)
BEGIN
    UPDATE stock_levels
       SET quantity = quantity - OLD.quantity_change
     WHERE product_id = OLD.product_id;

    INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0)
        ON CONFLICT(product_id) DO NOTHING;

    UPDATE stock_levels
       SET quantity = quantity + NEW.quantity_change
     WHERE product_id = NEW.product_id;
END;

DROP TRIGGER IF EXISTS after_transaction_delete;
CREATE TRIGGER after_transaction_delete
AFTER DELETE ON transactions
BEGIN
    UPDATE stock_levels
       SET quantity = quantity - OLD.quantity_change
     WHERE product_id = OLD.product_id;
END;
