-- ============================================================================
-- Migration 076 — audit_log coverage for remaining money-path tables
--
-- Adds INSERT/UPDATE/DELETE triggers for three money-path tables that fell
-- through the audit-coverage roadmap:
--
--   1. paid_invoices       — links a `received_payments` row to an invoice
--                            (IV/SR). Editing iv_no or amount silently
--                            re-routes which invoice gets credited.
--
--   2. credit_note_amounts — authoritative ใบลดหนี้ master amounts. Drift
--                            here caused the phantom-overpay ฿105k bug
--                            (fixed by mig 062). Triggers protect the
--                            denormalized cache going forward.
--
--   3. cashbook_transactions — raw cashflow rows from the Excel round-trip.
--                              Direct admin edits via SQL shell were
--                              completely silent.
--
-- Same shape as mig 070 (transactions + received_payments). Rollback drops
-- all 9 triggers.
-- ============================================================================

BEGIN;

-- Drop any prior versions so re-applying this mig (manual dev re-run, etc.)
-- always lands the latest trigger body. The runner is filename-keyed so it
-- won't re-execute on its own, but a dev rolling back + re-running this SQL
-- by hand must get the current shape, not whatever older copy was on disk.
DROP TRIGGER IF EXISTS audit_paid_invoices_insert;
DROP TRIGGER IF EXISTS audit_paid_invoices_update;
DROP TRIGGER IF EXISTS audit_paid_invoices_delete;
DROP TRIGGER IF EXISTS audit_credit_note_amounts_insert;
DROP TRIGGER IF EXISTS audit_credit_note_amounts_update;
DROP TRIGGER IF EXISTS audit_credit_note_amounts_delete;
DROP TRIGGER IF EXISTS audit_cashbook_transactions_insert;
DROP TRIGGER IF EXISTS audit_cashbook_transactions_update;
DROP TRIGGER IF EXISTS audit_cashbook_transactions_delete;

-- ── 1. paid_invoices ────────────────────────────────────────────────────────
CREATE TRIGGER audit_paid_invoices_insert
AFTER INSERT ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', NEW.id, 'INSERT',
        json_object(
            're_id',  NEW.re_id,
            'iv_no',  NEW.iv_no,
            'amount', NEW.amount
        )
    );
END;

CREATE TRIGGER audit_paid_invoices_update
AFTER UPDATE ON paid_invoices
WHEN (
       OLD.re_id  IS NOT NEW.re_id
    OR OLD.iv_no  IS NOT NEW.iv_no
    OR OLD.amount IS NOT NEW.amount
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'paid_invoices', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 're_id'  AS field, OLD.re_id  AS old_v, NEW.re_id  AS new_v WHERE OLD.re_id  IS NOT NEW.re_id
        UNION ALL SELECT 'iv_no',  OLD.iv_no,  NEW.iv_no  WHERE OLD.iv_no  IS NOT NEW.iv_no
        UNION ALL SELECT 'amount', OLD.amount, NEW.amount WHERE OLD.amount IS NOT NEW.amount
    );
END;

CREATE TRIGGER audit_paid_invoices_delete
BEFORE DELETE ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', OLD.id, 'DELETE',
        json_object('re_id', OLD.re_id, 'iv_no', OLD.iv_no, 'amount', OLD.amount)
    );
END;

-- ── 2. credit_note_amounts ──────────────────────────────────────────────────
CREATE TRIGGER audit_credit_note_amounts_insert
AFTER INSERT ON credit_note_amounts
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'credit_note_amounts', NEW.id, 'INSERT',
        json_object(
            'sr_doc_base',     NEW.sr_doc_base,
            'ref_invoice',     NEW.ref_invoice,
            'credited_amount', NEW.credited_amount,
            'sr_date_iso',     NEW.sr_date_iso,
            'customer',        NEW.customer,
            'source',          NEW.source
        )
    );
END;

CREATE TRIGGER audit_credit_note_amounts_update
AFTER UPDATE ON credit_note_amounts
WHEN (
       OLD.sr_doc_base     IS NOT NEW.sr_doc_base
    OR OLD.ref_invoice     IS NOT NEW.ref_invoice
    OR OLD.credited_amount IS NOT NEW.credited_amount
    OR OLD.sr_date_iso     IS NOT NEW.sr_date_iso
    OR OLD.customer        IS NOT NEW.customer
    OR OLD.source          IS NOT NEW.source
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'credit_note_amounts', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'sr_doc_base'     AS field, OLD.sr_doc_base     AS old_v, NEW.sr_doc_base     AS new_v WHERE OLD.sr_doc_base     IS NOT NEW.sr_doc_base
        UNION ALL SELECT 'ref_invoice',     OLD.ref_invoice,     NEW.ref_invoice     WHERE OLD.ref_invoice     IS NOT NEW.ref_invoice
        UNION ALL SELECT 'credited_amount', OLD.credited_amount, NEW.credited_amount WHERE OLD.credited_amount IS NOT NEW.credited_amount
        UNION ALL SELECT 'sr_date_iso',     OLD.sr_date_iso,     NEW.sr_date_iso     WHERE OLD.sr_date_iso     IS NOT NEW.sr_date_iso
        UNION ALL SELECT 'customer',        OLD.customer,        NEW.customer        WHERE OLD.customer        IS NOT NEW.customer
        UNION ALL SELECT 'source',          OLD.source,          NEW.source          WHERE OLD.source          IS NOT NEW.source
    );
END;

CREATE TRIGGER audit_credit_note_amounts_delete
BEFORE DELETE ON credit_note_amounts
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'credit_note_amounts', OLD.id, 'DELETE',
        json_object(
            'sr_doc_base',     OLD.sr_doc_base,
            'ref_invoice',     OLD.ref_invoice,
            'credited_amount', OLD.credited_amount,
            'sr_date_iso',     OLD.sr_date_iso,
            'customer',        OLD.customer,
            'source',          OLD.source
        )
    );
END;

-- ── 3. cashbook_transactions ────────────────────────────────────────────────
CREATE TRIGGER audit_cashbook_transactions_insert
AFTER INSERT ON cashbook_transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'cashbook_transactions', NEW.id, 'INSERT',
        json_object(
            'account_id',       NEW.account_id,
            'txn_date',         NEW.txn_date,
            'direction',        NEW.direction,
            'category',         NEW.category,
            'user_category',    NEW.user_category,
            'amount',           NEW.amount,
            'description',      NEW.description,
            'note',             NEW.note,
            'source_file',      NEW.source_file,
            'source_sheet',     NEW.source_sheet,
            'source_row',       NEW.source_row,
            'import_batch_id',  NEW.import_batch_id
        )
    );
END;

CREATE TRIGGER audit_cashbook_transactions_update
AFTER UPDATE ON cashbook_transactions
WHEN (
       OLD.account_id      IS NOT NEW.account_id
    OR OLD.txn_date        IS NOT NEW.txn_date
    OR OLD.direction       IS NOT NEW.direction
    OR OLD.category        IS NOT NEW.category
    OR OLD.user_category   IS NOT NEW.user_category
    OR OLD.amount          IS NOT NEW.amount
    OR OLD.description     IS NOT NEW.description
    OR OLD.note            IS NOT NEW.note
    OR OLD.source_file     IS NOT NEW.source_file
    OR OLD.source_sheet    IS NOT NEW.source_sheet
    OR OLD.source_row      IS NOT NEW.source_row
    OR OLD.import_batch_id IS NOT NEW.import_batch_id
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'cashbook_transactions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'account_id'      AS field, OLD.account_id      AS old_v, NEW.account_id      AS new_v WHERE OLD.account_id      IS NOT NEW.account_id
        UNION ALL SELECT 'txn_date',        OLD.txn_date,        NEW.txn_date        WHERE OLD.txn_date        IS NOT NEW.txn_date
        UNION ALL SELECT 'direction',       OLD.direction,       NEW.direction       WHERE OLD.direction       IS NOT NEW.direction
        UNION ALL SELECT 'category',        OLD.category,        NEW.category        WHERE OLD.category        IS NOT NEW.category
        UNION ALL SELECT 'user_category',   OLD.user_category,   NEW.user_category   WHERE OLD.user_category   IS NOT NEW.user_category
        UNION ALL SELECT 'amount',          OLD.amount,          NEW.amount          WHERE OLD.amount          IS NOT NEW.amount
        UNION ALL SELECT 'description',     OLD.description,     NEW.description     WHERE OLD.description     IS NOT NEW.description
        UNION ALL SELECT 'note',            OLD.note,            NEW.note            WHERE OLD.note            IS NOT NEW.note
        UNION ALL SELECT 'source_file',     OLD.source_file,     NEW.source_file     WHERE OLD.source_file     IS NOT NEW.source_file
        UNION ALL SELECT 'source_sheet',    OLD.source_sheet,    NEW.source_sheet    WHERE OLD.source_sheet    IS NOT NEW.source_sheet
        UNION ALL SELECT 'source_row',      OLD.source_row,      NEW.source_row      WHERE OLD.source_row      IS NOT NEW.source_row
        UNION ALL SELECT 'import_batch_id', OLD.import_batch_id, NEW.import_batch_id WHERE OLD.import_batch_id IS NOT NEW.import_batch_id
    );
END;

CREATE TRIGGER audit_cashbook_transactions_delete
BEFORE DELETE ON cashbook_transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'cashbook_transactions', OLD.id, 'DELETE',
        json_object(
            'account_id',      OLD.account_id,
            'txn_date',        OLD.txn_date,
            'direction',       OLD.direction,
            'category',        OLD.category,
            'user_category',   OLD.user_category,
            'amount',          OLD.amount,
            'description',     OLD.description,
            'note',            OLD.note,
            'source_file',     OLD.source_file,
            'source_sheet',    OLD.source_sheet,
            'source_row',      OLD.source_row,
            'import_batch_id', OLD.import_batch_id
        )
    );
END;

COMMIT;
