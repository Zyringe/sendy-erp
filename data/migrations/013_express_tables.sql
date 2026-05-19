-- 013_express_tables.sql
-- Express ERP integration — Stage 6.
--
-- Adds the storage layer for the 5 weekly Express exports we now parse:
--   1) ใบลดหนี้                    (credit notes)
--   2) การรับชำระหนี้               (incoming payments — commission base)
--   3) ลูกหนี้คงค้างแบบละเอียด      (AR snapshot)
--   4) จ่ายชำระหนี้                 (outgoing supplier payments)
--   5) ขาย                          (sales-history-by-customer)
--
-- One ledger table per file plus child tables for the multi-line
-- structures (credit-note lines, invoice refs on receipts, receive-doc
-- refs on supplier payments). All tables carry batch_id → import_log
-- so re-importing a file is traceable and undoable.
--
-- Lookup FKs (customer_id, supplier_id, product_id) stay nullable —
-- backfilled by mapping logic AFTER the row is imported (similar to
-- how sales_transactions handles unmapped BSN codes today).
--
-- Stock is intentionally NOT touched. Per Put's note 2026-05-01,
-- current stock_levels is treated as authoritative — Express data
-- feeds AR / AP / commission only.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/013_express_tables.sql
--
-- Rollback: 013_express_tables.rollback.sql

BEGIN;

-- ── 1. import-batch tracking ──────────────────────────────────────────────
CREATE TABLE express_import_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_type         TEXT    NOT NULL CHECK(file_type IN
                                ('credit_notes','payments_in','ar_snapshot',
                                 'payments_out','sales')),
    source_filename   TEXT,
    record_count      INTEGER NOT NULL DEFAULT 0,     -- main rows
    line_count        INTEGER NOT NULL DEFAULT 0,     -- child rows (where applicable)
    snapshot_date_iso TEXT,                            -- for ar_snapshot file_type
    company_id        INTEGER REFERENCES companies(id),  -- BSN by default for now
    note              TEXT,
    status            TEXT    NOT NULL DEFAULT 'imported'
                              CHECK(status IN ('imported','failed','partial','superseded')),
    imported_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_express_import_log_type ON express_import_log(file_type, imported_at DESC);

-- ── 2. credit notes (ใบลดหนี้) ─────────────────────────────────────────────
CREATE TABLE express_credit_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES express_import_log(id) ON DELETE CASCADE,
    doc_no          TEXT    NOT NULL,                  -- GR6700001, GR69000001
    date_iso        TEXT    NOT NULL,                  -- YYYY-MM-DD (Gregorian)
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    supplier_name   TEXT    NOT NULL,                  -- raw name from Express
    supplier_id     INTEGER REFERENCES suppliers(id),  -- backfill via name lookup
    ref_doc         TEXT,                              -- RR6700025 etc
    discount_amount REAL    NOT NULL DEFAULT 0,
    vat_amount      REAL    NOT NULL DEFAULT 0,
    total_amount    REAL    NOT NULL DEFAULT 0,
    is_cleared      INTEGER NOT NULL DEFAULT 0 CHECK(is_cleared IN (0,1)),
    is_void         INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1)),
    type_code       INTEGER,
    note            TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(batch_id, doc_no)
);

CREATE INDEX idx_express_cn_doc ON express_credit_notes(doc_no);
CREATE INDEX idx_express_cn_date ON express_credit_notes(date_iso);
CREATE INDEX idx_express_cn_supplier ON express_credit_notes(supplier_id);

CREATE TABLE express_credit_note_lines (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_note_id    INTEGER NOT NULL REFERENCES express_credit_notes(id) ON DELETE CASCADE,
    line_no           INTEGER NOT NULL,
    product_code      TEXT,                            -- 561ต1060
    product_id        INTEGER REFERENCES products(id), -- backfill via product_code
    product_name_raw  TEXT,
    qty               REAL,
    unit              TEXT,
    unit_price        REAL,
    discount          TEXT,                             -- '5%', '25+5%', '26.00'
    line_total        REAL,
    is_cleared        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_express_cn_line_cn ON express_credit_note_lines(credit_note_id);
CREATE INDEX idx_express_cn_line_product ON express_credit_note_lines(product_code);

-- ── 3. incoming payments (การรับชำระหนี้) — COMMISSION BASE ───────────────
CREATE TABLE express_payments_in (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES express_import_log(id) ON DELETE CASCADE,
    doc_no            TEXT    NOT NULL,                -- RE6700001
    date_iso          TEXT    NOT NULL,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    customer_name     TEXT    NOT NULL,
    customer_code     TEXT,                            -- backfill via name lookup
    customer_id       TEXT REFERENCES customers(code), -- customers PK is code (TEXT)
    salesperson_code  TEXT,                            -- '02', '06', '06-L', or empty
    is_void           INTEGER NOT NULL DEFAULT 0,
    -- Money columns (right-aligned in source):
    deposit_applied   REAL    NOT NULL DEFAULT 0,
    invoice_amount    REAL    NOT NULL DEFAULT 0,
    cash_amount       REAL    NOT NULL DEFAULT 0,
    cheque_amount     REAL    NOT NULL DEFAULT 0,
    interest_amount   REAL    NOT NULL DEFAULT 0,
    discount_amount   REAL    NOT NULL DEFAULT 0,
    vat_amount        REAL    NOT NULL DEFAULT 0,
    -- Cheque trailer (mostly NULL on cash receipts):
    cheque_no         TEXT,
    cheque_date_iso   TEXT,
    bank              TEXT,
    cheque_status     TEXT,
    note              TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(batch_id, doc_no)
);

CREATE INDEX idx_express_pin_doc ON express_payments_in(doc_no);
CREATE INDEX idx_express_pin_date ON express_payments_in(date_iso);
CREATE INDEX idx_express_pin_sp ON express_payments_in(salesperson_code, date_iso);
CREATE INDEX idx_express_pin_customer ON express_payments_in(customer_id);

CREATE TABLE express_payment_in_invoice_refs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_in_id   INTEGER NOT NULL REFERENCES express_payments_in(id) ON DELETE CASCADE,
    invoice_no      TEXT    NOT NULL,                  -- IV6601903
    invoice_date_iso TEXT,
    amount          REAL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_express_pin_ref_pid ON express_payment_in_invoice_refs(payment_in_id);
CREATE INDEX idx_express_pin_ref_inv ON express_payment_in_invoice_refs(invoice_no);

-- ── 4. AR outstanding snapshot (ลูกหนี้คงค้างแบบละเอียด) ──────────────────
CREATE TABLE express_ar_outstanding (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER NOT NULL REFERENCES express_import_log(id) ON DELETE CASCADE,
    snapshot_date_iso   TEXT    NOT NULL,              -- 2026-04-30
    customer_code       TEXT    NOT NULL,
    customer_name       TEXT,
    customer_id         TEXT REFERENCES customers(code),
    customer_type       TEXT,                          -- ลูกค้าประจำ / ตัวแทนจำหน่าย / ฯลฯ
    doc_date_iso        TEXT,
    doc_no              TEXT    NOT NULL,
    is_anomalous        INTEGER NOT NULL DEFAULT 0,    -- ! prefix
    salesperson_code    TEXT,
    bill_amount         REAL    NOT NULL DEFAULT 0,
    paid_amount         REAL    NOT NULL DEFAULT 0,
    outstanding_amount  REAL    NOT NULL DEFAULT 0,
    has_warning         INTEGER NOT NULL DEFAULT 0,    -- *** marker
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_express_ar_snapshot ON express_ar_outstanding(snapshot_date_iso, customer_code);
CREATE INDEX idx_express_ar_customer ON express_ar_outstanding(customer_id);
CREATE INDEX idx_express_ar_doc ON express_ar_outstanding(doc_no);

-- ── 5. outgoing supplier payments (จ่ายชำระหนี้) ──────────────────────────
CREATE TABLE express_payments_out (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES express_import_log(id) ON DELETE CASCADE,
    doc_no          TEXT    NOT NULL,                  -- PS0001815, PS0000E02
    date_iso        TEXT    NOT NULL,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    supplier_name   TEXT    NOT NULL,
    supplier_id     INTEGER REFERENCES suppliers(id),
    is_void         INTEGER NOT NULL DEFAULT 0,
    -- Money breakdown:
    deposit_applied REAL    NOT NULL DEFAULT 0,
    invoice_amount  REAL    NOT NULL DEFAULT 0,
    cash_amount     REAL    NOT NULL DEFAULT 0,
    cheque_amount   REAL    NOT NULL DEFAULT 0,
    interest_amount REAL    NOT NULL DEFAULT 0,
    discount_amount REAL    NOT NULL DEFAULT 0,
    vat_amount      REAL    NOT NULL DEFAULT 0,
    -- Cheque trailer (mostly NULL):
    cheque_no       TEXT,
    cheque_date_iso TEXT,
    bank            TEXT,
    cheque_status   TEXT,
    note            TEXT,                              -- "BBL ต๋อโอน", "สด", multi-line "VAT 16,894"
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(batch_id, doc_no)
);

CREATE INDEX idx_express_pout_doc ON express_payments_out(doc_no);
CREATE INDEX idx_express_pout_date ON express_payments_out(date_iso);
CREATE INDEX idx_express_pout_supplier ON express_payments_out(supplier_id);

CREATE TABLE express_payment_out_receive_refs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_out_id    INTEGER NOT NULL REFERENCES express_payments_out(id) ON DELETE CASCADE,
    receive_doc       TEXT    NOT NULL,                -- RR6600291, GR6600016
    receive_date_iso  TEXT,
    invoice_ref       TEXT,                            -- supplier's invoice number
    amount            REAL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_express_pout_ref_pid ON express_payment_out_receive_refs(payment_out_id);
CREATE INDEX idx_express_pout_ref_doc ON express_payment_out_receive_refs(receive_doc);

-- ── 6. sales line items (ขาย) ─────────────────────────────────────────────
-- One row per Express sales line (IV / SR / HS doc + line_no).
CREATE TABLE express_sales (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES express_import_log(id) ON DELETE CASCADE,
    doc_no            TEXT    NOT NULL,                -- IV6900025, SR6700135, HS...
    line_no           INTEGER NOT NULL,
    doc_type          TEXT    NOT NULL CHECK(doc_type IN ('IV','SR','HS','HP')),
    date_iso          TEXT    NOT NULL,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    customer_code     TEXT,
    customer_name     TEXT,
    customer_id       TEXT REFERENCES customers(code),
    product_code      TEXT,
    product_id        INTEGER REFERENCES products(id),
    product_name_raw  TEXT,
    qty               REAL,
    unit              TEXT,
    return_flag       TEXT,                             -- 'Y' on SR rows, else ''
    unit_price        REAL,
    vat_type          INTEGER,                          -- 0 / 1 / 2 (no-VAT / excluded / included)
    discount          TEXT,                              -- '5%' / '25+5%' / numeric string
    total             REAL    NOT NULL DEFAULT 0,
    total_discount    REAL    NOT NULL DEFAULT 0,
    net               REAL    NOT NULL DEFAULT 0,
    ref_doc           TEXT,                              -- SO0007002-1, IV6801171-1
    is_warning        INTEGER NOT NULL DEFAULT 0,        -- *** "below cost" marker
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(batch_id, doc_no, line_no)
);

CREATE INDEX idx_express_sales_doc ON express_sales(doc_no);
CREATE INDEX idx_express_sales_date ON express_sales(date_iso);
CREATE INDEX idx_express_sales_customer ON express_sales(customer_id);
CREATE INDEX idx_express_sales_product ON express_sales(product_id);
CREATE INDEX idx_express_sales_doctype ON express_sales(doc_type, date_iso);

COMMIT;
