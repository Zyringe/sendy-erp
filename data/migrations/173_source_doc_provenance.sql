-- 173 — provenance for the two source-document tables.
--
-- audit_log covers 38 tables through 111 triggers and held ZERO rows for these
-- two, which carry every invoice and every purchase line. When 47 invoices were
-- moved to a different customer on 2026-08-24 nothing recorded that it happened.
--
-- WHY THE REASON LIVES ON THE ROW rather than in a side table: a shared context
-- row is only safe while the declare and the mutation share one transaction, and
-- a second connection can otherwise stamp its reason onto the first one's edit —
-- reproduced, see projects/express-integration/spike/provenance-2026-08-25/ in
-- the brain repo. A per-connection temp table would fix that, except SQLite
-- refuses to build a persistent trigger that references temp at all.
--
-- SCOPE: only UPDATE of a MEANINGFUL column is blocked. INSERT and DELETE are
-- recorded, not blocked — the importer replaces a changed line with
-- DELETE+INSERT, and a DELETE has no NEW row to carry a reason on.
-- synced_to_stock / batch_id are exempt: bsn_sync rewrites them constantly.
--
-- ⚠ `NEW.change_source IS NULL` is a SEPARATE term on purpose. `NULL NOT IN
-- (...)` and `NULL = 'manual'` both evaluate to NULL, so an UPDATE that supplies
-- a fresh token and actor but a NULL source made the whole OR chain NULL — and a
-- trigger whose WHEN is NULL does not fire. That was a working bypass (Codex,
-- 2026-08-25); every other term is TRUE/FALSE, this one restores that property.
--
-- ⚠ The UPDATE audit row is keyed on the OLD identity. If a change renames
-- doc_no or bsn_code, the event belongs in the history of the line you were
-- looking at; the diff itself names where it went.
--
-- ⚠ RE-RUNNABILITY, precisely: every trigger is DROP ... IF EXISTS first, so the
-- trigger half re-applies cleanly. The ALTER TABLE ADD COLUMN statements are NOT
-- idempotent and cannot be — SQLite has no ADD COLUMN IF NOT EXISTS — so a second
-- execution of the whole file stops at `duplicate column name: change_source`.
-- The runner never repeats an applied migration; a hand re-run against a DB that
-- already has the columns should start from the first DROP TRIGGER below. The
-- rollback drops the columns as well, so rollback-then-reapply DOES work.

ALTER TABLE audit_log ADD COLUMN change_source TEXT;
ALTER TABLE audit_log ADD COLUMN change_reason TEXT;

ALTER TABLE sales_transactions ADD COLUMN change_source TEXT;
ALTER TABLE sales_transactions ADD COLUMN change_actor TEXT;
ALTER TABLE sales_transactions ADD COLUMN change_reason TEXT;
ALTER TABLE sales_transactions ADD COLUMN change_token TEXT;

ALTER TABLE purchase_transactions ADD COLUMN change_source TEXT;
ALTER TABLE purchase_transactions ADD COLUMN change_actor TEXT;
ALTER TABLE purchase_transactions ADD COLUMN change_reason TEXT;
ALTER TABLE purchase_transactions ADD COLUMN change_token TEXT;


DROP TRIGGER IF EXISTS sales_transactions_change_needs_declaration;
CREATE TRIGGER sales_transactions_change_needs_declaration
BEFORE UPDATE ON sales_transactions
WHEN (
        (   OLD.date_iso IS NOT NEW.date_iso
           OR OLD.doc_no IS NOT NEW.doc_no
           OR OLD.doc_base IS NOT NEW.doc_base
           OR OLD.product_id IS NOT NEW.product_id
           OR OLD.bsn_code IS NOT NEW.bsn_code
           OR OLD.product_name_raw IS NOT NEW.product_name_raw
           OR OLD.customer IS NOT NEW.customer
           OR OLD.customer_code IS NOT NEW.customer_code
           OR OLD.qty IS NOT NEW.qty
           OR OLD.unit IS NOT NEW.unit
           OR OLD.unit_price IS NOT NEW.unit_price
           OR OLD.vat_type IS NOT NEW.vat_type
           OR OLD.discount IS NOT NEW.discount
           OR OLD.total IS NOT NEW.total
           OR OLD.net IS NOT NEW.net
           OR OLD.ref_invoice IS NOT NEW.ref_invoice
        )
    AND (
           NEW.change_token  IS NULL
        OR NEW.change_token  IS OLD.change_token
        OR NEW.change_source IS NULL
        OR NEW.change_source NOT IN ('import', 'manual')
        OR NEW.change_actor  IS NULL
        OR trim(NEW.change_actor) = ''
        OR (NEW.change_source = 'manual'
            AND (NEW.change_reason IS NULL OR length(trim(NEW.change_reason)) < 12))
    )
)
BEGIN
    SELECT RAISE(ABORT,
        'ห้ามแก้เอกสารต้นทางโดยไม่ระบุที่มา — ต้องส่ง change_source (import/manual), change_actor, change_token ใหม่ และถ้าเป็น manual ต้องมี change_reason ที่อธิบายได้จริง');
END;

DROP TRIGGER IF EXISTS audit_sales_transactions_update;
CREATE TRIGGER audit_sales_transactions_update
AFTER UPDATE ON sales_transactions
WHEN (   OLD.date_iso IS NOT NEW.date_iso
           OR OLD.doc_no IS NOT NEW.doc_no
           OR OLD.doc_base IS NOT NEW.doc_base
           OR OLD.product_id IS NOT NEW.product_id
           OR OLD.bsn_code IS NOT NEW.bsn_code
           OR OLD.product_name_raw IS NOT NEW.product_name_raw
           OR OLD.customer IS NOT NEW.customer
           OR OLD.customer_code IS NOT NEW.customer_code
           OR OLD.qty IS NOT NEW.qty
           OR OLD.unit IS NOT NEW.unit
           OR OLD.unit_price IS NOT NEW.unit_price
           OR OLD.vat_type IS NOT NEW.vat_type
           OR OLD.discount IS NOT NEW.discount
           OR OLD.total IS NOT NEW.total
           OR OLD.net IS NOT NEW.net
           OR OLD.ref_invoice IS NOT NEW.ref_invoice
     )
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields,
                           user, change_source, change_reason)
    SELECT 'sales_transactions', NEW.id, OLD.doc_no || '|' || COALESCE(OLD.bsn_code, '∅'), 'UPDATE',
           json_group_object(field, json_array(old_v, new_v)),
           NEW.change_actor, NEW.change_source, NEW.change_reason
    FROM (
                  SELECT 'date_iso'         AS field, OLD.date_iso         AS old_v, NEW.date_iso         AS new_v WHERE OLD.date_iso         IS NOT NEW.date_iso
        UNION ALL SELECT 'doc_no',                     OLD.doc_no,            NEW.doc_no            WHERE OLD.doc_no           IS NOT NEW.doc_no
        UNION ALL SELECT 'doc_base',                   OLD.doc_base,          NEW.doc_base          WHERE OLD.doc_base         IS NOT NEW.doc_base
        UNION ALL SELECT 'product_id',                 OLD.product_id,        NEW.product_id        WHERE OLD.product_id       IS NOT NEW.product_id
        UNION ALL SELECT 'bsn_code',                   OLD.bsn_code,          NEW.bsn_code          WHERE OLD.bsn_code         IS NOT NEW.bsn_code
        UNION ALL SELECT 'product_name_raw',           OLD.product_name_raw,  NEW.product_name_raw  WHERE OLD.product_name_raw IS NOT NEW.product_name_raw
        UNION ALL SELECT 'customer',                   OLD.customer,          NEW.customer          WHERE OLD.customer         IS NOT NEW.customer
        UNION ALL SELECT 'customer_code',              OLD.customer_code,     NEW.customer_code     WHERE OLD.customer_code    IS NOT NEW.customer_code
        UNION ALL SELECT 'qty',                        OLD.qty,               NEW.qty               WHERE OLD.qty              IS NOT NEW.qty
        UNION ALL SELECT 'unit',                       OLD.unit,              NEW.unit              WHERE OLD.unit             IS NOT NEW.unit
        UNION ALL SELECT 'unit_price',                 OLD.unit_price,        NEW.unit_price        WHERE OLD.unit_price       IS NOT NEW.unit_price
        UNION ALL SELECT 'vat_type',                   OLD.vat_type,          NEW.vat_type          WHERE OLD.vat_type         IS NOT NEW.vat_type
        UNION ALL SELECT 'discount',                   OLD.discount,          NEW.discount          WHERE OLD.discount         IS NOT NEW.discount
        UNION ALL SELECT 'total',                      OLD.total,             NEW.total             WHERE OLD.total            IS NOT NEW.total
        UNION ALL SELECT 'net',                        OLD.net,               NEW.net               WHERE OLD.net              IS NOT NEW.net
        UNION ALL SELECT 'ref_invoice',                OLD.ref_invoice,       NEW.ref_invoice       WHERE OLD.ref_invoice      IS NOT NEW.ref_invoice
    );
END;

DROP TRIGGER IF EXISTS audit_sales_transactions_insert;
CREATE TRIGGER audit_sales_transactions_insert
AFTER INSERT ON sales_transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields,
                           user, change_source, change_reason)
    VALUES ('sales_transactions', NEW.id, NEW.doc_no || '|' || COALESCE(NEW.bsn_code, '∅'), 'INSERT',
            json_object('doc_no', NEW.doc_no, 'bsn_code', NEW.bsn_code, 'net', NEW.net),
            NEW.change_actor, NEW.change_source, NEW.change_reason);
END;

DROP TRIGGER IF EXISTS audit_sales_transactions_delete;
CREATE TRIGGER audit_sales_transactions_delete
AFTER DELETE ON sales_transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields,
                           user, change_source, change_reason)
    VALUES ('sales_transactions', OLD.id, OLD.doc_no || '|' || COALESCE(OLD.bsn_code, '∅'), 'DELETE',
            json_object('doc_no', OLD.doc_no, 'bsn_code', OLD.bsn_code, 'net', OLD.net),
            OLD.change_actor, OLD.change_source, OLD.change_reason);
END;

DROP TRIGGER IF EXISTS purchase_transactions_change_needs_declaration;
CREATE TRIGGER purchase_transactions_change_needs_declaration
BEFORE UPDATE ON purchase_transactions
WHEN (
        (   OLD.date_iso IS NOT NEW.date_iso
           OR OLD.doc_no IS NOT NEW.doc_no
           OR OLD.doc_base IS NOT NEW.doc_base
           OR OLD.product_id IS NOT NEW.product_id
           OR OLD.bsn_code IS NOT NEW.bsn_code
           OR OLD.product_name_raw IS NOT NEW.product_name_raw
           OR OLD.supplier IS NOT NEW.supplier
           OR OLD.supplier_code IS NOT NEW.supplier_code
           OR OLD.supplier_id IS NOT NEW.supplier_id
           OR OLD.qty IS NOT NEW.qty
           OR OLD.unit IS NOT NEW.unit
           OR OLD.unit_price IS NOT NEW.unit_price
           OR OLD.vat_type IS NOT NEW.vat_type
           OR OLD.discount IS NOT NEW.discount
           OR OLD.total IS NOT NEW.total
           OR OLD.net IS NOT NEW.net
           OR OLD.line_seq IS NOT NEW.line_seq
        )
    AND (
           NEW.change_token  IS NULL
        OR NEW.change_token  IS OLD.change_token
        OR NEW.change_source IS NULL
        OR NEW.change_source NOT IN ('import', 'manual')
        OR NEW.change_actor  IS NULL
        OR trim(NEW.change_actor) = ''
        OR (NEW.change_source = 'manual'
            AND (NEW.change_reason IS NULL OR length(trim(NEW.change_reason)) < 12))
    )
)
BEGIN
    SELECT RAISE(ABORT,
        'ห้ามแก้เอกสารต้นทางโดยไม่ระบุที่มา — ต้องส่ง change_source (import/manual), change_actor, change_token ใหม่ และถ้าเป็น manual ต้องมี change_reason ที่อธิบายได้จริง');
END;

DROP TRIGGER IF EXISTS audit_purchase_transactions_update;
CREATE TRIGGER audit_purchase_transactions_update
AFTER UPDATE ON purchase_transactions
WHEN (   OLD.date_iso IS NOT NEW.date_iso
           OR OLD.doc_no IS NOT NEW.doc_no
           OR OLD.doc_base IS NOT NEW.doc_base
           OR OLD.product_id IS NOT NEW.product_id
           OR OLD.bsn_code IS NOT NEW.bsn_code
           OR OLD.product_name_raw IS NOT NEW.product_name_raw
           OR OLD.supplier IS NOT NEW.supplier
           OR OLD.supplier_code IS NOT NEW.supplier_code
           OR OLD.supplier_id IS NOT NEW.supplier_id
           OR OLD.qty IS NOT NEW.qty
           OR OLD.unit IS NOT NEW.unit
           OR OLD.unit_price IS NOT NEW.unit_price
           OR OLD.vat_type IS NOT NEW.vat_type
           OR OLD.discount IS NOT NEW.discount
           OR OLD.total IS NOT NEW.total
           OR OLD.net IS NOT NEW.net
           OR OLD.line_seq IS NOT NEW.line_seq
     )
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields,
                           user, change_source, change_reason)
    SELECT 'purchase_transactions', NEW.id, OLD.doc_no || '|' || COALESCE(OLD.bsn_code, '∅') || '|' || OLD.line_seq, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v)),
           NEW.change_actor, NEW.change_source, NEW.change_reason
    FROM (
                  SELECT 'date_iso'         AS field, OLD.date_iso         AS old_v, NEW.date_iso         AS new_v WHERE OLD.date_iso         IS NOT NEW.date_iso
        UNION ALL SELECT 'doc_no',                     OLD.doc_no,            NEW.doc_no            WHERE OLD.doc_no           IS NOT NEW.doc_no
        UNION ALL SELECT 'doc_base',                   OLD.doc_base,          NEW.doc_base          WHERE OLD.doc_base         IS NOT NEW.doc_base
        UNION ALL SELECT 'product_id',                 OLD.product_id,        NEW.product_id        WHERE OLD.product_id       IS NOT NEW.product_id
        UNION ALL SELECT 'bsn_code',                   OLD.bsn_code,          NEW.bsn_code          WHERE OLD.bsn_code         IS NOT NEW.bsn_code
        UNION ALL SELECT 'product_name_raw',           OLD.product_name_raw,  NEW.product_name_raw  WHERE OLD.product_name_raw IS NOT NEW.product_name_raw
        UNION ALL SELECT 'supplier',                   OLD.supplier,          NEW.supplier          WHERE OLD.supplier         IS NOT NEW.supplier
        UNION ALL SELECT 'supplier_code',              OLD.supplier_code,     NEW.supplier_code     WHERE OLD.supplier_code    IS NOT NEW.supplier_code
        UNION ALL SELECT 'supplier_id',                OLD.supplier_id,       NEW.supplier_id       WHERE OLD.supplier_id      IS NOT NEW.supplier_id
        UNION ALL SELECT 'qty',                        OLD.qty,               NEW.qty               WHERE OLD.qty              IS NOT NEW.qty
        UNION ALL SELECT 'unit',                       OLD.unit,              NEW.unit              WHERE OLD.unit             IS NOT NEW.unit
        UNION ALL SELECT 'unit_price',                 OLD.unit_price,        NEW.unit_price        WHERE OLD.unit_price       IS NOT NEW.unit_price
        UNION ALL SELECT 'vat_type',                   OLD.vat_type,          NEW.vat_type          WHERE OLD.vat_type         IS NOT NEW.vat_type
        UNION ALL SELECT 'discount',                   OLD.discount,          NEW.discount          WHERE OLD.discount         IS NOT NEW.discount
        UNION ALL SELECT 'total',                      OLD.total,             NEW.total             WHERE OLD.total            IS NOT NEW.total
        UNION ALL SELECT 'net',                        OLD.net,               NEW.net               WHERE OLD.net              IS NOT NEW.net
        UNION ALL SELECT 'line_seq',                   OLD.line_seq,          NEW.line_seq          WHERE OLD.line_seq         IS NOT NEW.line_seq
    );
END;

DROP TRIGGER IF EXISTS audit_purchase_transactions_insert;
CREATE TRIGGER audit_purchase_transactions_insert
AFTER INSERT ON purchase_transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields,
                           user, change_source, change_reason)
    VALUES ('purchase_transactions', NEW.id, NEW.doc_no || '|' || COALESCE(NEW.bsn_code, '∅') || '|' || NEW.line_seq, 'INSERT',
            json_object('doc_no', NEW.doc_no, 'bsn_code', NEW.bsn_code, 'net', NEW.net),
            NEW.change_actor, NEW.change_source, NEW.change_reason);
END;

DROP TRIGGER IF EXISTS audit_purchase_transactions_delete;
CREATE TRIGGER audit_purchase_transactions_delete
AFTER DELETE ON purchase_transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields,
                           user, change_source, change_reason)
    VALUES ('purchase_transactions', OLD.id, OLD.doc_no || '|' || COALESCE(OLD.bsn_code, '∅') || '|' || OLD.line_seq, 'DELETE',
            json_object('doc_no', OLD.doc_no, 'bsn_code', OLD.bsn_code, 'net', OLD.net),
            OLD.change_actor, OLD.change_source, OLD.change_reason);
END;
