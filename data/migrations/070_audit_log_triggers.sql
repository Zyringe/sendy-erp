-- ============================================================================
-- Migration 070 — audit_log triggers for transactions + received_payments
--
-- Why
--   audit_log shipped with mig 023. Pre-existing triggers cover products,
--   customers, suppliers, regions, salespersons, companies, expense_*,
--   purchase_orders/lines/po_receipts, commission_*, listing_bundles,
--   product_families, product_images, product_price_tiers. The two highest-
--   value finance tables that were NOT covered:
--     - transactions      (stock-movement ledger — append-only by convention)
--     - received_payments (RE customer-payment imports)
--
-- Delta scope
--   ONLY adds the 4 missing triggers. Does NOT redefine pre-existing triggers
--   (that would double-log on every mutation). Other unaudited tables can be
--   added in later migrations as their need surfaces.
--
-- changed_by best-effort
--   SQLite triggers have no app-session context, so the `user` column is left
--   NULL. App-layer audit writes (where user identity matters) should insert
--   into audit_log directly. These triggers are a safety net for ANY mutation
--   — including direct sqlite3 CLI edits and bulk imports.
--
-- Style matches the existing audit_products_* triggers (mig 023):
--   - audit_log column shape: (table_name, row_id, action, changed_fields)
--   - changed_fields = JSON via json_object() for INSERT/DELETE, diff-only
--     json_group_object(field, json_array(old, new)) for UPDATE
--   - DELETE triggers use BEFORE DELETE so OLD is still queryable
-- ============================================================================

BEGIN;

-- ── transactions: INSERT only (append-only ledger) ──────────────────────────
CREATE TRIGGER audit_transactions_insert
AFTER INSERT ON transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'transactions', NEW.id, 'INSERT',
        json_object(
            'product_id',      NEW.product_id,
            'txn_type',        NEW.txn_type,
            'quantity_change', NEW.quantity_change,
            'unit_mode',       NEW.unit_mode,
            'reference_no',    NEW.reference_no,
            'note',            NEW.note
        )
    );
END;

-- ── received_payments: INSERT / UPDATE / DELETE ─────────────────────────────
CREATE TRIGGER audit_received_payments_insert
AFTER INSERT ON received_payments
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'received_payments', NEW.id, 'INSERT',
        json_object(
            're_no',       NEW.re_no,
            'date_iso',    NEW.date_iso,
            'customer',    NEW.customer,
            'salesperson', NEW.salesperson,
            'cancelled',   NEW.cancelled,
            'total',       NEW.total
        )
    );
END;

CREATE TRIGGER audit_received_payments_update
AFTER UPDATE ON received_payments
WHEN (
       OLD.re_no       IS NOT NEW.re_no
    OR OLD.date_iso    IS NOT NEW.date_iso
    OR OLD.customer    IS NOT NEW.customer
    OR OLD.salesperson IS NOT NEW.salesperson
    OR OLD.cancelled   IS NOT NEW.cancelled
    OR OLD.total       IS NOT NEW.total
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'received_payments', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 're_no'       AS field, OLD.re_no       AS old_v, NEW.re_no       AS new_v WHERE OLD.re_no       IS NOT NEW.re_no
        UNION ALL SELECT 'date_iso',    OLD.date_iso,    NEW.date_iso    WHERE OLD.date_iso    IS NOT NEW.date_iso
        UNION ALL SELECT 'customer',    OLD.customer,    NEW.customer    WHERE OLD.customer    IS NOT NEW.customer
        UNION ALL SELECT 'salesperson', OLD.salesperson, NEW.salesperson WHERE OLD.salesperson IS NOT NEW.salesperson
        UNION ALL SELECT 'cancelled',   OLD.cancelled,   NEW.cancelled   WHERE OLD.cancelled   IS NOT NEW.cancelled
        UNION ALL SELECT 'total',       OLD.total,       NEW.total       WHERE OLD.total       IS NOT NEW.total
    );
END;

CREATE TRIGGER audit_received_payments_delete
BEFORE DELETE ON received_payments
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'received_payments', OLD.id, 'DELETE',
        json_object(
            're_no',       OLD.re_no,
            'date_iso',    OLD.date_iso,
            'customer',    OLD.customer,
            'salesperson', OLD.salesperson,
            'cancelled',   OLD.cancelled,
            'total',       OLD.total
        )
    );
END;

-- Index for forensic queries. audit_log had no indexes before this; queries
-- like "what changed in received_payments last week" were full scans. With
-- transactions now triggering an audit row on every BSN sync (~thousands per
-- weekly import), audit_log grows fast and lookup speed matters.
CREATE INDEX IF NOT EXISTS idx_audit_log_table_time
    ON audit_log(table_name, created_at DESC);

COMMIT;
