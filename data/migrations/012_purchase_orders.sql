-- 012_purchase_orders.sql
-- Phase E1 of the schema refactor.
--
-- Adds the missing "ใบสั่งซื้อ" layer between deciding to order and
-- the BSN sync arriving a week later. Today, the gap between
-- "called the supplier" and "ของมาถึง + BSN sync" is opaque — there
-- is no record of what is on order, by which company, for how much.
--
-- Four new tables:
--
--   po_sequences         — yearly per-company counter for PO numbers.
--                          Format: BSN-PO-2026-0001, SD-PO-2026-0001.
--                          Resets to 0001 on January 1 (Q1=a, Q2=a).
--                          NOT audited (internal counter).
--
--   purchase_orders      — header. status enum: draft / submitted /
--                          completed / cancelled. No 'partial_received'
--                          state — partial receipt is computed from
--                          line.qty_ordered vs SUM(po_receipts.qty)
--                          (Q3=c).
--
--   purchase_order_lines — one row per (PO, product). qty_ordered is
--                          the plan; actual receipt lives in po_receipts.
--
--   po_receipts          — append-only log of receipts against a line.
--                          One PO line can be received in multiple
--                          batches (Q4=b), each with its own date and
--                          (optionally) BSN doc_no.
--
-- BSN sync (Q5=c): NOT auto-linked. purchase_transactions stays a
-- separate stream. Linking PO ↔ BSN is a future phase.
--
-- ON DELETE CASCADE on purchase_order_lines.po_id and po_receipts.line_id
-- is declared for documentation only — the app runs with foreign_keys=OFF.
--
-- Decisions locked with Put on 2026-04-30:
--   Q1=(a) yearly reset
--   Q2=(a) Gregorian year (ค.ศ.)
--   Q3=(c) status has no partial_received; compute from lines
--   Q4=(b) separate po_receipts table
--   Q5=(c) no auto-link to BSN purchase_transactions
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/012_purchase_orders.sql
--
-- Rollback: 012_purchase_orders.rollback.sql

BEGIN;

-- ── po_sequences (counter, no audit) ──────────────────────────────────────
-- Atomic generation pattern (run inside the same transaction as the
-- purchase_orders INSERT):
--   INSERT OR IGNORE INTO po_sequences (company_id, year) VALUES (?, ?);
--   UPDATE po_sequences
--      SET last_seq = last_seq + 1,
--          updated_at = datetime('now','localtime')
--    WHERE company_id = ? AND year = ?;
--   SELECT last_seq FROM po_sequences WHERE company_id = ? AND year = ?;
--   -> format f"{companies.code}-PO-{year}-{seq:04d}"
CREATE TABLE po_sequences (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    year       INTEGER NOT NULL,
    last_seq   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (company_id, year)
);

-- ── purchase_orders ───────────────────────────────────────────────────────
CREATE TABLE purchase_orders (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    po_number             TEXT    UNIQUE NOT NULL,                 -- BSN-PO-2026-0001
    company_id            INTEGER NOT NULL REFERENCES companies(id),
    supplier_id           INTEGER NOT NULL REFERENCES suppliers(id),
    order_date            TEXT    NOT NULL,                         -- YYYY-MM-DD
    expected_arrival_date TEXT,                                      -- YYYY-MM-DD, NULL if unknown
    status                TEXT    NOT NULL DEFAULT 'draft'
                                  CHECK(status IN ('draft','submitted','completed','cancelled')),
    total_pre_vat         REAL    NOT NULL DEFAULT 0,                -- sum of line subtotals
    vat_amount            REAL    NOT NULL DEFAULT 0,
    note                  TEXT,
    created_by            TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_po_company    ON purchase_orders(company_id);
CREATE INDEX idx_po_supplier   ON purchase_orders(supplier_id);
CREATE INDEX idx_po_status     ON purchase_orders(status);
CREATE INDEX idx_po_order_date ON purchase_orders(order_date);

-- ── purchase_order_lines ──────────────────────────────────────────────────
CREATE TABLE purchase_order_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id         INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    qty_ordered   INTEGER NOT NULL,
    unit_price    REAL    NOT NULL,
    line_subtotal REAL    NOT NULL,                                   -- qty_ordered × unit_price (denormalised for reporting)
    note          TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_pol_po      ON purchase_order_lines(po_id);
CREATE INDEX idx_pol_product ON purchase_order_lines(product_id);

-- ── po_receipts ───────────────────────────────────────────────────────────
CREATE TABLE po_receipts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id       INTEGER NOT NULL REFERENCES purchase_order_lines(id) ON DELETE CASCADE,
    qty_received  INTEGER NOT NULL,
    received_date TEXT    NOT NULL,                                   -- YYYY-MM-DD
    doc_no        TEXT,                                                -- BSN doc_no when known (manually filled)
    note          TEXT,
    received_by   TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_por_line ON po_receipts(line_id);
CREATE INDEX idx_por_date ON po_receipts(received_date);

-- ── audit triggers: purchase_orders ───────────────────────────────────────
CREATE TRIGGER audit_purchase_orders_insert
AFTER INSERT ON purchase_orders
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'purchase_orders', NEW.id, 'INSERT',
        json_object(
            'po_number', NEW.po_number, 'company_id', NEW.company_id,
            'supplier_id', NEW.supplier_id, 'order_date', NEW.order_date,
            'status', NEW.status, 'total_pre_vat', NEW.total_pre_vat
        )
    );
END;

CREATE TRIGGER audit_purchase_orders_update
AFTER UPDATE ON purchase_orders
WHEN (
       OLD.po_number             IS NOT NEW.po_number
    OR OLD.company_id             IS NOT NEW.company_id
    OR OLD.supplier_id            IS NOT NEW.supplier_id
    OR OLD.order_date             IS NOT NEW.order_date
    OR OLD.expected_arrival_date  IS NOT NEW.expected_arrival_date
    OR OLD.status                 IS NOT NEW.status
    OR OLD.total_pre_vat          IS NOT NEW.total_pre_vat
    OR OLD.vat_amount             IS NOT NEW.vat_amount
    OR OLD.note                   IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'purchase_orders', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'po_number'             AS field, OLD.po_number             AS old_v, NEW.po_number             AS new_v WHERE OLD.po_number             IS NOT NEW.po_number
        UNION ALL SELECT 'company_id',                     OLD.company_id,                     NEW.company_id                     WHERE OLD.company_id            IS NOT NEW.company_id
        UNION ALL SELECT 'supplier_id',                    OLD.supplier_id,                    NEW.supplier_id                    WHERE OLD.supplier_id           IS NOT NEW.supplier_id
        UNION ALL SELECT 'order_date',                     OLD.order_date,                     NEW.order_date                     WHERE OLD.order_date            IS NOT NEW.order_date
        UNION ALL SELECT 'expected_arrival_date',          OLD.expected_arrival_date,          NEW.expected_arrival_date          WHERE OLD.expected_arrival_date IS NOT NEW.expected_arrival_date
        UNION ALL SELECT 'status',                         OLD.status,                         NEW.status                         WHERE OLD.status                IS NOT NEW.status
        UNION ALL SELECT 'total_pre_vat',                  OLD.total_pre_vat,                  NEW.total_pre_vat                  WHERE OLD.total_pre_vat         IS NOT NEW.total_pre_vat
        UNION ALL SELECT 'vat_amount',                     OLD.vat_amount,                     NEW.vat_amount                     WHERE OLD.vat_amount            IS NOT NEW.vat_amount
        UNION ALL SELECT 'note',                           OLD.note,                           NEW.note                           WHERE OLD.note                  IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_purchase_orders_delete
BEFORE DELETE ON purchase_orders
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'purchase_orders', OLD.id, 'DELETE',
        json_object(
            'po_number', OLD.po_number, 'company_id', OLD.company_id,
            'supplier_id', OLD.supplier_id, 'status', OLD.status,
            'total_pre_vat', OLD.total_pre_vat
        )
    );
END;

-- ── audit triggers: purchase_order_lines ──────────────────────────────────
CREATE TRIGGER audit_purchase_order_lines_insert
AFTER INSERT ON purchase_order_lines
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'purchase_order_lines', NEW.id, 'INSERT',
        json_object(
            'po_id', NEW.po_id, 'product_id', NEW.product_id,
            'qty_ordered', NEW.qty_ordered, 'unit_price', NEW.unit_price,
            'line_subtotal', NEW.line_subtotal
        )
    );
END;

CREATE TRIGGER audit_purchase_order_lines_update
AFTER UPDATE ON purchase_order_lines
WHEN (
       OLD.po_id          IS NOT NEW.po_id
    OR OLD.product_id     IS NOT NEW.product_id
    OR OLD.qty_ordered    IS NOT NEW.qty_ordered
    OR OLD.unit_price     IS NOT NEW.unit_price
    OR OLD.line_subtotal  IS NOT NEW.line_subtotal
    OR OLD.note           IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'purchase_order_lines', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'po_id'         AS field, OLD.po_id         AS old_v, NEW.po_id         AS new_v WHERE OLD.po_id         IS NOT NEW.po_id
        UNION ALL SELECT 'product_id',             OLD.product_id,             NEW.product_id             WHERE OLD.product_id    IS NOT NEW.product_id
        UNION ALL SELECT 'qty_ordered',            OLD.qty_ordered,            NEW.qty_ordered            WHERE OLD.qty_ordered   IS NOT NEW.qty_ordered
        UNION ALL SELECT 'unit_price',             OLD.unit_price,             NEW.unit_price             WHERE OLD.unit_price    IS NOT NEW.unit_price
        UNION ALL SELECT 'line_subtotal',          OLD.line_subtotal,          NEW.line_subtotal          WHERE OLD.line_subtotal IS NOT NEW.line_subtotal
        UNION ALL SELECT 'note',                   OLD.note,                   NEW.note                   WHERE OLD.note          IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_purchase_order_lines_delete
BEFORE DELETE ON purchase_order_lines
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'purchase_order_lines', OLD.id, 'DELETE',
        json_object(
            'po_id', OLD.po_id, 'product_id', OLD.product_id,
            'qty_ordered', OLD.qty_ordered, 'unit_price', OLD.unit_price
        )
    );
END;

-- ── audit triggers: po_receipts ───────────────────────────────────────────
CREATE TRIGGER audit_po_receipts_insert
AFTER INSERT ON po_receipts
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'po_receipts', NEW.id, 'INSERT',
        json_object(
            'line_id', NEW.line_id, 'qty_received', NEW.qty_received,
            'received_date', NEW.received_date, 'doc_no', NEW.doc_no,
            'received_by', NEW.received_by
        )
    );
END;

CREATE TRIGGER audit_po_receipts_update
AFTER UPDATE ON po_receipts
WHEN (
       OLD.line_id        IS NOT NEW.line_id
    OR OLD.qty_received   IS NOT NEW.qty_received
    OR OLD.received_date  IS NOT NEW.received_date
    OR OLD.doc_no         IS NOT NEW.doc_no
    OR OLD.note           IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'po_receipts', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'line_id'       AS field, OLD.line_id       AS old_v, NEW.line_id       AS new_v WHERE OLD.line_id       IS NOT NEW.line_id
        UNION ALL SELECT 'qty_received',           OLD.qty_received,           NEW.qty_received           WHERE OLD.qty_received  IS NOT NEW.qty_received
        UNION ALL SELECT 'received_date',          OLD.received_date,          NEW.received_date          WHERE OLD.received_date IS NOT NEW.received_date
        UNION ALL SELECT 'doc_no',                 OLD.doc_no,                 NEW.doc_no                 WHERE OLD.doc_no        IS NOT NEW.doc_no
        UNION ALL SELECT 'note',                   OLD.note,                   NEW.note                   WHERE OLD.note          IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_po_receipts_delete
BEFORE DELETE ON po_receipts
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'po_receipts', OLD.id, 'DELETE',
        json_object(
            'line_id', OLD.line_id, 'qty_received', OLD.qty_received,
            'received_date', OLD.received_date, 'doc_no', OLD.doc_no
        )
    );
END;

COMMIT;
