-- schema.sql — COMPLETE current Sendy schema baseline (AUTO-GENERATED).
-- Do not hand-edit. Regenerate with: scripts/dump_schema.py
--
-- Applied by database.init_db() to build a brand-new DB in one shot (bare
-- `git clone` + first `sendy-up`), instead of replaying the migration history.
-- After it applies, run_pending_migrations() backfills all shipped migrations
-- as already-applied (it keys on the `brands` table existing).
--
-- Re-run dump_schema.py and commit whenever a migration changes the schema.

PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE applied_migrations (
    filename     TEXT    PRIMARY KEY,
    applied_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    applied_by   TEXT,
    sha256       TEXT,
    duration_ms  INTEGER
);

CREATE TABLE ar_followup_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer          TEXT    NOT NULL,
    customer_code     TEXT,
    log_date          TEXT    NOT NULL,
    channel           TEXT    NOT NULL
                              CHECK(channel IN ('phone','line','sms','email','visit','other')),
    contact_person    TEXT,
    result            TEXT    NOT NULL
                              CHECK(result IN (
                                  'promised','partial_paid','paid_full',
                                  'denied','no_answer','wrong_number',
                                  'closed','snooze','other'
                              )),
    promised_amount   REAL,
    promised_date     TEXT,
    next_action_date  TEXT,
    notes             TEXT,
    created_by        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT
);

CREATE TABLE ar_writeoffs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no         TEXT    NOT NULL,            -- the express_ar_outstanding doc_no written off
    customer_code  TEXT,                        -- '01อ35' etc (for grouping; nullable for legacy)
    customer_name  TEXT,
    amount         REAL    NOT NULL DEFAULT 0,  -- signed snapshot outstanding at decision time
    type           TEXT    NOT NULL CHECK(type IN ('expense','writeback')),
    writeoff_date  TEXT    NOT NULL,            -- ISO date the decision was recorded
    reason         TEXT,                        -- e.g. 'legacy 2014 dead account', 'Put 2026-06-05'
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(doc_no)                              -- one write-off decision per doc
);

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT    NOT NULL,
    row_id          INTEGER NOT NULL,
    action          TEXT    NOT NULL CHECK(action IN ('INSERT','UPDATE','DELETE')),
    changed_fields  TEXT,           -- JSON: {"field": [old, new], ...}
    user            TEXT,           -- session username (nullable for system writes)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE brands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    UNIQUE NOT NULL,
    name         TEXT    NOT NULL,            -- canonical display name (e.g. 'Sendai')
    name_th      TEXT,                         -- Thai display (e.g. 'เซ็นได')
    is_own_brand INTEGER NOT NULL DEFAULT 0 CHECK(is_own_brand IN (0,1)),
    sort_order   INTEGER NOT NULL DEFAULT 100,
    note         TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, short_code TEXT);

CREATE TABLE bsn_unit_alias (
    acronym TEXT PRIMARY KEY,
    full    TEXT NOT NULL
);

CREATE TABLE cashbook_accounts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT    UNIQUE NOT NULL,   -- '392','LEX','SPX','ชฎามาศ','กิติยา','904'
    display_name       TEXT,
    bank_name          TEXT,
    bank_account_no    TEXT,
    account_owner_name TEXT,
    note               TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    sort_order         INTEGER NOT NULL DEFAULT 100,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, is_transfer INTEGER NOT NULL DEFAULT 0
    CHECK(is_transfer IN (0,1)));

CREATE TABLE cashbook_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK(direction IN ('income','expense')),
    source      TEXT    CHECK(source IN ('setup','imported') OR source IS NULL),
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    sort_order  INTEGER NOT NULL DEFAULT 100,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(name, direction)
);

CREATE TABLE "cashbook_transactions" (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES cashbook_accounts(id),
    txn_date        TEXT    NOT NULL,
    direction       TEXT    NOT NULL CHECK(direction IN ('income','expense')),
    category        TEXT,
    user_category   TEXT,
    amount          REAL    NOT NULL,
    description     TEXT,
    note            TEXT,
    source_file     TEXT,
    source_sheet    TEXT,
    source_row      INTEGER,
    import_batch_id TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    UNIQUE NOT NULL,
    name_th     TEXT    NOT NULL,
    parent_id   INTEGER REFERENCES categories(id),
    sort_order  INTEGER NOT NULL DEFAULT 100,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, short_code TEXT);

CREATE TABLE color_finish_codes (
    code        TEXT PRIMARY KEY,         -- 'AC', 'PAB', 'SS', ...
    name_th     TEXT NOT NULL,             -- canonical Thai name shown to customers
    sort_order  INTEGER NOT NULL DEFAULT 100,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE commission_assignments (
    salesperson_code TEXT    PRIMARY KEY REFERENCES salespersons(code),
    tier_id          INTEGER NOT NULL REFERENCES commission_tiers(id),
    effective_from   TEXT    NOT NULL,                          -- YYYY-MM-DD
    note             TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE commission_overrides (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id            INTEGER REFERENCES products(id),
    brand_id              INTEGER REFERENCES brands(id),
    salesperson_code      TEXT,
    fixed_per_unit        REAL,
    custom_rate_pct       REAL,
    apply_when_price_gt   REAL    NOT NULL DEFAULT 0,
    apply_when_price_lte  REAL,
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                  TEXT,
    effective_from        TEXT    NOT NULL DEFAULT (date('now')),
    created_at            TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK ((product_id IS NOT NULL) OR (brand_id IS NOT NULL)),
    CHECK ((fixed_per_unit IS NOT NULL) OR (custom_rate_pct IS NOT NULL))
);

CREATE TABLE commission_payouts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month        TEXT    NOT NULL,                       -- 'YYYY-MM'
    salesperson_code  TEXT    NOT NULL REFERENCES salespersons(code),
    amount_paid       REAL    NOT NULL,
    paid_date         TEXT    NOT NULL,                       -- 'YYYY-MM-DD'
    paid_method       TEXT,                                    -- 'cash', 'transfer', 'cheque', etc
    note              TEXT,
    paid_by           TEXT,                                    -- user that marked
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, invoice_no TEXT);

CREATE TABLE commission_tiers (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    code                   TEXT    UNIQUE NOT NULL,           -- 'A', 'B', 'C'
    name_th                TEXT    NOT NULL,
    description            TEXT,
    rate_own_pct           REAL    NOT NULL DEFAULT 0,        -- below threshold (or always if no threshold)
    rate_third_pct         REAL    NOT NULL DEFAULT 0,
    threshold_amount       REAL,                               -- NULL = no threshold
    above_rate_own_pct     REAL,                               -- only used if threshold IS NOT NULL
    above_rate_third_pct   REAL,
    is_active              INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                   TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    UNIQUE NOT NULL,            -- 'BSN', 'STD'
    name_th     TEXT    NOT NULL,                    -- legal Thai name
    short_name  TEXT,                                -- display short
    tax_id      TEXT,                                -- เลขผู้เสียภาษี 13 หลัก (NULL until known)
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE company_holidays (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER REFERENCES companies(id),
    holiday_date TEXT    NOT NULL,
    name_th      TEXT,
    year         INTEGER
);

CREATE TABLE conversion_cost_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                output_product_id INTEGER NOT NULL REFERENCES products(id),
                reference_no      TEXT,
                event_date        TEXT    NOT NULL,
                output_qty        REAL    NOT NULL,
                total_input_cost  REAL    NOT NULL,
                unit_cost         REAL    NOT NULL,
                created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            , writeoff_qty INTEGER NOT NULL DEFAULT 0);

CREATE TABLE conversion_formula_inputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    formula_id  INTEGER NOT NULL REFERENCES conversion_formulas(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL
);

CREATE TABLE conversion_formulas (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    output_product_id INTEGER NOT NULL REFERENCES products(id),
    output_qty        INTEGER NOT NULL DEFAULT 1,
    note              TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE credit_note_amounts (
    id              INTEGER PRIMARY KEY,
    sr_doc_base     TEXT    NOT NULL,
    ref_invoice     TEXT,
    credited_amount REAL    NOT NULL DEFAULT 0.0,
    sr_date_iso     TEXT,
    customer        TEXT,
    source          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(sr_doc_base)
);

CREATE TABLE credit_note_imports (
    id               INTEGER PRIMARY KEY,
    doc_no           TEXT    NOT NULL,          -- e.g. "SR6700009-2"
    doc_base         TEXT    NOT NULL,          -- e.g. "SR6700009"
    date_iso         TEXT    NOT NULL,
    customer         TEXT,
    salesperson      TEXT,
    ref_invoice      TEXT,
    ref_invoice_line TEXT,
    vat_type         INTEGER DEFAULT 1,
    bsn_code         TEXT,
    product_name_raw TEXT,
    qty              REAL    DEFAULT 0,
    unit             TEXT,
    unit_price       REAL    DEFAULT 0,
    discount         TEXT,
    total            REAL    DEFAULT 0,
    net              REAL    DEFAULT 0,
    cancelled        INTEGER DEFAULT 0,
    imported_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(doc_no)
);

CREATE TABLE customer_call_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('note','call','data_flag')),
    body          TEXT,
    created_by    TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    deleted_at    TEXT,
    deleted_by    TEXT
);

CREATE TABLE customer_contact_review (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code     TEXT    NOT NULL,
    original_json     TEXT    NOT NULL,
    proposed_name     TEXT,
    proposed_nickname TEXT,
    proposed_phone    TEXT,
    proposed_fax      TEXT,
    proposed_contact  TEXT,
    proposed_address  TEXT,
    proposed_region   TEXT,
    confidence        TEXT    NOT NULL CHECK (confidence IN ('auto','review')),
    issues_json       TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','applied','confirmed','skipped')),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_by       TEXT,
    reviewed_at       TEXT
, proposed_note TEXT);

CREATE TABLE customer_crm (
    customer_code    TEXT    PRIMARY KEY,
    tags             TEXT,
    next_call_date   TEXT,
    call_target_days INTEGER,
    updated_by       TEXT,
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE customer_regions (
        customer_code TEXT PRIMARY KEY,
        region        TEXT,
        salesperson   TEXT
    );

CREATE TABLE customers (
    code          TEXT    PRIMARY KEY,
    name          TEXT    NOT NULL,
    salesperson   TEXT,
    zone          TEXT,
    customer_type TEXT,
    address       TEXT,
    phone         TEXT,
    tax_id        TEXT,
    credit_days   INTEGER NOT NULL DEFAULT 0,
    contact       TEXT,
    lat           REAL,
    lng           REAL,
    geocoded_at   TEXT,
    imported_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, region_id INTEGER REFERENCES regions(id), plus_code    TEXT, gmap_name    TEXT, gmap_address TEXT, fax                   TEXT, nickname              TEXT, contact_orig_json     TEXT, contact_normalized_at TEXT, contact_normalized_by TEXT, contact_note TEXT);

CREATE TABLE ecommerce_listings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                platform     TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
                item_name    TEXT    NOT NULL,
                variation    TEXT,
                seller_sku   TEXT,
                listing_key  TEXT    NOT NULL UNIQUE,
                sample_price REAL,
                product_id   INTEGER REFERENCES products(id),
                is_ignored   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            , qty_per_sale REAL NOT NULL DEFAULT 1);

CREATE TABLE employee_leave_entitlements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
    year          INTEGER NOT NULL,
    quota_days    REAL,
    note          TEXT,
    UNIQUE(employee_id, leave_type_id, year)
);

CREATE TABLE employee_salary_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id),
    effective_date  TEXT    NOT NULL,
    monthly_salary  REAL    NOT NULL,
    reason          TEXT    NOT NULL DEFAULT 'initial'
                            CHECK(reason IN ('initial','post_probation','raise','adjust')),
    note            TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(employee_id, effective_date)
);

CREATE TABLE employees (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_code            TEXT    UNIQUE NOT NULL,
    full_name           TEXT    NOT NULL,
    nickname            TEXT,
    national_id         TEXT,
    gender              TEXT    CHECK(gender IN ('M','F')),
    phone               TEXT,
    address             TEXT,
    position            TEXT,
    company_id          INTEGER REFERENCES companies(id),
    employment_type     TEXT    NOT NULL DEFAULT 'monthly'
                                CHECK(employment_type IN ('monthly','daily','contract')),
    start_date          TEXT,
    probation_days      INTEGER NOT NULL DEFAULT 90,
    probation_end_date  TEXT,
    end_date            TEXT,
    sso_enrolled        INTEGER NOT NULL DEFAULT 1 CHECK(sso_enrolled IN (0,1)),
    diligence_allowance REAL    NOT NULL DEFAULT 0,
    bank_name           TEXT,
    bank_branch         TEXT,
    bank_account_no     TEXT,
    bank_account_name   TEXT,
    salesperson_code    TEXT,
    user_id             INTEGER REFERENCES users(id),
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, sort_order INTEGER NOT NULL DEFAULT 100, on_payroll INTEGER NOT NULL DEFAULT 1
                                  CHECK(on_payroll IN (0,1)));

CREATE TABLE expense_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    UNIQUE NOT NULL,
    name_th     TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 100,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE expense_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date_iso        TEXT    NOT NULL,                -- YYYY-MM-DD (Gregorian)
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    category_id     INTEGER NOT NULL REFERENCES expense_categories(id),
    amount_pre_vat  REAL    NOT NULL,                 -- ยอดก่อน VAT
    vat_amount      REAL    NOT NULL DEFAULT 0,        -- VAT input ที่จ่าย (0 ถ้าไม่มี)
    description     TEXT,                              -- รายละเอียดสั้น ๆ
    doc_no          TEXT,                              -- เลขใบกำกับ (NULL ได้)
    created_by      TEXT,                              -- username (NULL ถ้า system)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE express_ap_outstanding (
    id                  INTEGER PRIMARY KEY,
    batch_id            INTEGER,
    entity              TEXT NOT NULL DEFAULT 'BSN',
    snapshot_date_iso   TEXT,
    supplier_type       TEXT,                 -- ประเภทผู้จำหน่าย (group header)
    supplier_name       TEXT,
    supplier_code       TEXT,
    supplier_id         INTEGER,
    doc_no              TEXT,                 -- เอกสาร# (RR receive doc)
    supplier_invoice_no TEXT,                 -- เลขที่บิล (supplier's bill no)
    doc_date_iso        TEXT,                 -- row date
    bill_amount         REAL,                 -- ยอดในบิล
    paid_amount         REAL,                 -- ยอดชำระ
    outstanding_amount  REAL,                 -- ยอดคงค้าง
    created_at          TEXT DEFAULT (datetime('now'))
);

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
, entity TEXT NOT NULL DEFAULT 'SD');

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

CREATE TABLE "express_import_log" (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_type         TEXT    NOT NULL CHECK(file_type IN
                                ('credit_notes','payments_in','ar_snapshot',
                                 'ap_snapshot','payments_out','sales')),
    source_filename   TEXT,
    record_count      INTEGER NOT NULL DEFAULT 0,
    line_count        INTEGER NOT NULL DEFAULT 0,
    snapshot_date_iso TEXT,
    company_id        INTEGER REFERENCES companies(id),
    note              TEXT,
    status            TEXT    NOT NULL DEFAULT 'imported'
                              CHECK(status IN ('imported','failed','partial','superseded')),
    imported_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE express_payment_in_invoice_refs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_in_id   INTEGER NOT NULL REFERENCES express_payments_in(id) ON DELETE CASCADE,
    invoice_no      TEXT    NOT NULL,                  -- IV6601903
    invoice_date_iso TEXT,
    amount          REAL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE express_payment_out_receive_refs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_out_id    INTEGER NOT NULL REFERENCES express_payments_out(id) ON DELETE CASCADE,
    receive_doc       TEXT    NOT NULL,                -- RR6600291, GR6600016
    receive_date_iso  TEXT,
    invoice_ref       TEXT,                            -- supplier's invoice number
    amount            REAL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

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

CREATE TABLE hr_config (
    key   TEXT PRIMARY KEY,
    value TEXT,
    note  TEXT
);

CREATE TABLE import_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT    NOT NULL,
    rows_imported   INTEGER NOT NULL,
    rows_skipped    INTEGER NOT NULL,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    notes           TEXT
);

CREATE TABLE lazada_statement_settlement (
    statement   TEXT PRIMARY KEY,   -- รอบบิล, e.g. THJ2K7MP-2026-0531
    settled_at  TEXT NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS' (precise, from wallet)
    amount      REAL NOT NULL       -- settlement amount for the รอบบิล (cross-check)
);

CREATE TABLE leave_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id    INTEGER NOT NULL REFERENCES leave_types(id),
    start_date       TEXT    NOT NULL,
    end_date         TEXT    NOT NULL,
    days             REAL    NOT NULL,           -- 0.5 allowed (half-day)
    reason           TEXT,
    has_medical_cert INTEGER NOT NULL DEFAULT 0 CHECK(has_medical_cert IN (0,1)),
    status           TEXT    NOT NULL DEFAULT 'approved'
                             CHECK(status IN ('pending','approved','rejected','cancelled')),
    created_by       TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE leave_types (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    code                     TEXT    UNIQUE NOT NULL,
    name_th                  TEXT    NOT NULL,
    default_quota_days       REAL,                       -- NULL = unlimited
    is_paid                  INTEGER NOT NULL DEFAULT 1 CHECK(is_paid IN (0,1)),
    affects_diligence        INTEGER NOT NULL DEFAULT 0 CHECK(affects_diligence IN (0,1)),
    requires_cert_after_days INTEGER,
    quota_basis              TEXT    CHECK(quota_basis IN ('after_1yr') OR quota_basis IS NULL),
    max_paid_days            REAL,
    sort_order               INTEGER NOT NULL DEFAULT 100,
    is_active                INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                     TEXT
);

CREATE TABLE legacy_product_sku_map (
    product_id INTEGER PRIMARY KEY,
    sku        INTEGER NOT NULL
);

CREATE TABLE listing_bundles (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id            INTEGER NOT NULL REFERENCES ecommerce_listings(id) ON DELETE CASCADE,
    component_product_id  INTEGER NOT NULL REFERENCES products(id),
    qty_per_sale          REAL    NOT NULL DEFAULT 1 CHECK (qty_per_sale > 0),
    note                  TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(listing_id, component_product_id)
);

CREATE TABLE marketplace_amount_review (
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

CREATE TABLE marketplace_order_fees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    order_sn        TEXT NOT NULL,
    item_value      REAL,
    fee_commission  REAL DEFAULT 0,
    fee_service     REAL DEFAULT 0,
    fee_transaction REAL DEFAULT 0,
    fee_platform    REAL DEFAULT 0,
    fee_ads_escrow  REAL DEFAULT 0,
    fee_tax         REAL DEFAULT 0,
    shipping_net    REAL DEFAULT 0,
    fee_saver       REAL DEFAULT 0,
    fee_total       REAL,
    net_payout      REAL,
    fee_pct         TEXT,
    fee_raw_json    TEXT,
    source_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, order_sn)
);

CREATE TABLE marketplace_order_invoice (
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

CREATE TABLE marketplace_order_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL REFERENCES marketplace_orders(id) ON DELETE CASCADE,
    platform            TEXT    NOT NULL,
    order_sn            TEXT    NOT NULL,
    line_key            TEXT    NOT NULL,
    seller_sku          TEXT,
    variation_id        TEXT,
    item_name           TEXT,
    variation_name      TEXT,
    internal_product_id INTEGER REFERENCES products(id),
    qty                 REAL    NOT NULL DEFAULT 0,
    unit_price          REAL,
    item_subtotal       REAL,
    raw_json            TEXT,
    UNIQUE(platform, order_sn, line_key)
);

CREATE TABLE marketplace_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    order_sn         TEXT    NOT NULL,        -- Shopee order_sn / Lazada order number
    status           TEXT,                    -- marketplace order status
    buyer_name       TEXT,
    buyer_phone      TEXT,
    ship_address     TEXT,
    order_date       TEXT,                    -- ISO; order create time
    paid_date        TEXT,                    -- ISO; settlement/payment time (nullable until settled)
    item_total       REAL,                    -- sum of line subtotals, pre-fee
    marketplace_fee  REAL,                    -- หักค่าบริการ (nullable until settled)
    payout           REAL,                    -- ยอดรวมหลังหักค่าคอม (nullable until settled)
    currency         TEXT    NOT NULL DEFAULT 'THB',
    source_file      TEXT,                    -- export filename this row was imported from
    raw_json         TEXT,                    -- full export row(s) for forensics
    first_synced_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    last_synced_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')), actual_payout REAL, settled_at TEXT, settlement_source TEXT, payout_batch_id INTEGER, payout_id INTEGER,
    UNIQUE(platform, order_sn)
);

CREATE TABLE marketplace_payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,
    deposit_date TEXT NOT NULL,
    amount       REAL NOT NULL,
    n_orders     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'reconciled',
    source_file  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE marketplace_wallet_txns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    txn_time        TEXT NOT NULL,
    txn_type        TEXT NOT NULL,          -- income | withdrawal | adjustment
    order_sn        TEXT,
    amount          REAL NOT NULL,
    running_balance REAL,
    description     TEXT,
    source_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, txn_time, txn_type, order_sn, amount)
);

CREATE TABLE "paid_invoices" (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    re_id      INTEGER NOT NULL REFERENCES received_payments(id),
    doc_no     TEXT    NOT NULL,
    doc_kind   TEXT    NOT NULL CHECK(doc_kind IN ('IV','SR')),
    amount     REAL,
    UNIQUE(re_id, doc_no)
);

CREATE TABLE payout_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deposit_date   TEXT    NOT NULL,
    deposit_amount REAL    NOT NULL,
    bank_ref       TEXT,
    note           TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    created_by     TEXT,
    is_baseline    INTEGER NOT NULL DEFAULT 0
    -- is_baseline=1: a one-time "ยอดยกมา" row that absorbs all pre-tracking
    -- settled orders so the greedy matcher sees only post-baseline orders.
    -- No bank-tie check applies to baseline rows.
);

CREATE TABLE payroll_items (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES payroll_runs(id),
    employee_id             INTEGER NOT NULL REFERENCES employees(id),
    salary_rate             REAL    NOT NULL DEFAULT 0,
    base_amount             REAL    NOT NULL DEFAULT 0,
    unpaid_leave_days       REAL    NOT NULL DEFAULT 0,
    unpaid_leave_deduction  REAL    NOT NULL DEFAULT 0,
    diligence_allowance     REAL    NOT NULL DEFAULT 0,
    diligence_forfeited     INTEGER NOT NULL DEFAULT 0 CHECK(diligence_forfeited IN (0,1)),
    diligence_forfeit_reason TEXT   CHECK(diligence_forfeit_reason IN ('leave','late')
                                          OR diligence_forfeit_reason IS NULL),
    bonus                   REAL    NOT NULL DEFAULT 0,
    other_additions         REAL    NOT NULL DEFAULT 0,
    other_additions_note    TEXT,
    other_deductions        REAL    NOT NULL DEFAULT 0,
    other_deductions_note   TEXT,
    sso_employee            REAL    NOT NULL DEFAULT 0,
    sso_employer            REAL    NOT NULL DEFAULT 0,
    commission_amount       REAL    NOT NULL DEFAULT 0,
    gross                   REAL    NOT NULL DEFAULT 0,
    net_pay                 REAL    NOT NULL DEFAULT 0,
    note                    TEXT,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now','localtime')), salary_advance_deduction REAL NOT NULL DEFAULT 0,
    UNIQUE(run_id, employee_id)
);

CREATE TABLE payroll_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month   TEXT    NOT NULL,               -- 'YYYY-MM'
    company_id   INTEGER REFERENCES companies(id),
    status       TEXT    NOT NULL DEFAULT 'draft'
                         CHECK(status IN ('draft','finalized')),
    run_date     TEXT,
    finalized_at TEXT,
    created_by   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(year_month, company_id)
);

CREATE TABLE pending_product_suggestions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bsn_code            TEXT    NOT NULL UNIQUE,
    bsn_name            TEXT    NOT NULL,

    -- Suggested structured fields (editable before approve)
    suggested_name      TEXT,                   -- final product_name to create
    category            TEXT,
    series              TEXT,
    brand_id            INTEGER REFERENCES brands(id),
    model               TEXT,
    size                TEXT,
    color_th            TEXT,
    color_code          TEXT    REFERENCES color_finish_codes(code),
    packaging           TEXT,                   -- check trigger applies on products, not here
    condition           TEXT,
    pack_variant        TEXT,

    -- Suggested operational fields
    suggested_cost      REAL    DEFAULT 0.0,    -- from latest purchase_transactions.unit_price
    suggested_unit_type TEXT    DEFAULT 'ตัว',
    units_per_carton    INTEGER,
    units_per_box       INTEGER,

    -- Workflow
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved')),
    suggested_by_user_id INTEGER REFERENCES users(id),
    reviewed_by_user_id  INTEGER REFERENCES users(id),
    approved_product_id  INTEGER REFERENCES products(id), -- set when approved
    notes               TEXT,                   -- free-text for staff/manager comments

    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_at         TEXT
, brand_other_name      TEXT, color_code_other      TEXT, packaging_other       TEXT, bsn_unit              TEXT, unit_conversion_ratio REAL);

CREATE TABLE platform_products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    platform          TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    product_id_str    TEXT    NOT NULL,
    parent_sku        TEXT,
    product_name      TEXT,
    name_en           TEXT,
    description       TEXT,
    category_id_str   TEXT,
    category_name     TEXT,
    brand             TEXT,
    place_of_origin   TEXT,
    material          TEXT,
    warranty_policy   TEXT,
    warranty_period   TEXT,
    status            TEXT,
    cover_image_url   TEXT,
    image_urls        TEXT,    -- JSON array of gallery image URLs
    dts_info          TEXT,
    raw_json          TEXT,
    imported_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, product_id_str)
);

CREATE TABLE platform_skus (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    platform             TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    product_id_str       TEXT,
    product_name         TEXT    NOT NULL,
    variation_id         TEXT,
    variation_name       TEXT,
    parent_sku           TEXT,
    seller_sku           TEXT,
    price                REAL,
    special_price        REAL,
    stock                INTEGER,
    internal_product_id  INTEGER REFERENCES products(id),
    qty_per_sale         REAL    NOT NULL DEFAULT 1,
    raw_json             TEXT,
    imported_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')), weight_kg REAL, length_cm REAL, width_cm REAL, height_cm REAL, gtin TEXT, special_price_start TEXT, special_price_end TEXT, variation_image_url TEXT,
    UNIQUE(platform, variation_id)
);

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

CREATE TABLE po_sequences (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    year       INTEGER NOT NULL,
    last_seq   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (company_id, year)
);

CREATE TABLE product_barcodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  barcode     TEXT    NOT NULL UNIQUE,
  is_primary  INTEGER NOT NULL DEFAULT 0,
  source      TEXT,
  note        TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE "product_code_mapping" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bsn_code TEXT NOT NULL,
  bsn_name TEXT NOT NULL,
  product_id INTEGER REFERENCES products(id),
  is_ignored INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ignore_reason TEXT,
  UNIQUE(bsn_code)
);

CREATE TABLE product_cost_ledger (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id   INTEGER NOT NULL REFERENCES products(id),
                event_type   TEXT    NOT NULL,
                event_date   TEXT    NOT NULL,
                qty_change   REAL    NOT NULL,
                unit_cost    REAL    NOT NULL,
                stock_after  REAL    NOT NULL,
                wacc_after   REAL    NOT NULL,
                reference_no TEXT,
                note         TEXT,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );

CREATE TABLE product_families (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    family_code  TEXT    UNIQUE NOT NULL,        -- e.g. 'SD-170', stable forever
    display_name TEXT    NOT NULL,                -- Thai, shown on catalog card
    brand_id     INTEGER REFERENCES brands(id),
    sort_order   INTEGER NOT NULL DEFAULT 100,
    note         TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, display_format TEXT DEFAULT 'single', catalogue_label TEXT);

CREATE TABLE product_images (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id        INTEGER NOT NULL REFERENCES product_families(id) ON DELETE CASCADE,
    sku_id           INTEGER REFERENCES products(id) ON DELETE SET NULL,
    image_path       TEXT    NOT NULL,
    presentation_tag TEXT,
    sort_order       INTEGER NOT NULL DEFAULT 100,
    note             TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(family_id, image_path)
);

CREATE TABLE product_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    floor_no    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE product_price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    field_name  TEXT    NOT NULL CHECK(field_name IN (
                    'cost_price',
                    'base_sell_price',
                    'low_stock_threshold'
                )),
    old_value   REAL,
    new_value   REAL,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE product_price_tiers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    qty_label   TEXT    NOT NULL,        -- e.g. '1 กิโล', '1 ลัง', '1 ตัว', '1 แผง'
    price       REAL    NOT NULL CHECK (price >= 0),
    note        TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 100,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(product_id, qty_label)
);

CREATE TABLE "products" (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name             TEXT    NOT NULL,
    units_per_carton         INTEGER NOT NULL DEFAULT 1,
    units_per_box            INTEGER NOT NULL DEFAULT 1,
    unit_type                TEXT    NOT NULL DEFAULT 'ตัว',
    hard_to_sell             INTEGER NOT NULL DEFAULT 0,
    cost_price               REAL    NOT NULL DEFAULT 0.0,
    base_sell_price          REAL    NOT NULL DEFAULT 0.0,
    low_stock_threshold      INTEGER NOT NULL DEFAULT 10,
    is_active                INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    shopee_stock             INTEGER NOT NULL DEFAULT 0,
    lazada_stock             INTEGER NOT NULL DEFAULT 0,
    brand_id                 INTEGER REFERENCES brands(id),
    category_id              INTEGER REFERENCES categories(id),
    family_id                INTEGER REFERENCES product_families(id),
    color_code               TEXT    REFERENCES color_finish_codes(code),
    packaging_th             TEXT,
    series                   TEXT,
    model                    TEXT,
    size                     TEXT,
    condition                TEXT,
    pack_variant             TEXT,
    sub_category             TEXT,
    sku_code                 TEXT,
    sku_code_locked          INTEGER NOT NULL DEFAULT 0
                                  CHECK(sku_code_locked IN (0, 1)),
    sub_category_short_code  TEXT,
    packaging_short          TEXT
, opening_cost REAL NOT NULL DEFAULT 0.0);

CREATE TABLE "promotions" (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    promo_name        TEXT    NOT NULL,
    promo_type        TEXT    NOT NULL,
    discount_value    REAL,                                            -- was NOT NULL; now nullable
    date_start        TEXT,
    date_end          TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),

    -- NEW columns (all nullable; CHECK enforces presence per promo_type)
    bundle_buy        INTEGER,
    bundle_free       INTEGER,
    bundle_unit       TEXT,
    bundle_condition  TEXT,
    bundle_tiers_json TEXT,
    gift_desc         TEXT,
    gift_qty          TEXT,

    -- Type enum + shape integrity per type
    CHECK (
        promo_type IN ('percent','fixed','bundle','mixed','gift')
        AND (bundle_condition IS NULL OR bundle_condition IN ('ยกลัง','ยกล่อง'))
        AND CASE promo_type
            WHEN 'percent' THEN
                discount_value IS NOT NULL
                AND discount_value BETWEEN 0 AND 100
                AND bundle_buy IS NULL AND bundle_free IS NULL
                AND bundle_tiers_json IS NULL
                AND gift_desc IS NULL AND gift_qty IS NULL
            WHEN 'fixed' THEN
                discount_value IS NOT NULL
                AND discount_value > 0
                AND bundle_buy IS NULL AND bundle_free IS NULL
                AND bundle_tiers_json IS NULL
                AND gift_desc IS NULL AND gift_qty IS NULL
            WHEN 'bundle' THEN
                bundle_buy IS NOT NULL AND bundle_free IS NOT NULL
                AND gift_desc IS NULL AND gift_qty IS NULL
                AND discount_value IS NULL
            WHEN 'gift' THEN
                gift_desc IS NOT NULL AND gift_qty IS NOT NULL
                AND bundle_buy IS NULL AND bundle_free IS NULL
                AND bundle_tiers_json IS NULL
                AND discount_value IS NULL
            WHEN 'mixed' THEN
                -- At least one structured field populated; any combination valid.
                (discount_value IS NOT NULL
                 OR bundle_buy IS NOT NULL
                 OR gift_desc IS NOT NULL)
        END
    )
);

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

CREATE TABLE purchase_transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER REFERENCES import_log(id),
    date_iso            TEXT NOT NULL,
    doc_no              TEXT NOT NULL,
    product_id          INTEGER REFERENCES products(id),
    bsn_code            TEXT,
    product_name_raw    TEXT,
    supplier            TEXT,
    supplier_code       TEXT,
    qty                 REAL,
    unit                TEXT,
    unit_price          REAL,
    vat_type            INTEGER,
    discount            TEXT,
    total               REAL,
    net                 REAL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
, synced_to_stock INTEGER NOT NULL DEFAULT 0, doc_base TEXT, supplier_id INTEGER REFERENCES suppliers(id), line_seq INTEGER NOT NULL DEFAULT 1);

CREATE TABLE received_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    re_no        TEXT    NOT NULL UNIQUE,
    date_iso     TEXT    NOT NULL,
    customer     TEXT    NOT NULL,
    salesperson  TEXT,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    imported_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, total REAL);

CREATE TABLE regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    UNIQUE NOT NULL,           -- 2-3 char Thai (กท, ทว, ขก)
    name_th    TEXT,                               -- ชื่อเต็ม (กรุงเทพฯ, ทวีวัฒนา) — fillable later
    parent_id  INTEGER REFERENCES regions(id),
    sort_order INTEGER NOT NULL DEFAULT 100,
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE "salary_advances" (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER REFERENCES employees(id),
    advance_date       TEXT    NOT NULL,
    amount             REAL    NOT NULL,
    raw_name           TEXT,
    note               TEXT,
    deducted_in_run_id INTEGER REFERENCES payroll_runs(id),
    source_file        TEXT,
    import_batch_id    TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE sales_transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER REFERENCES import_log(id),
    date_iso            TEXT NOT NULL,
    doc_no              TEXT NOT NULL,
    product_id          INTEGER REFERENCES products(id),
    bsn_code            TEXT,
    product_name_raw    TEXT,
    customer            TEXT,
    customer_code       TEXT,
    qty                 REAL,
    unit                TEXT,
    unit_price          REAL,
    vat_type            INTEGER,
    discount            TEXT,
    total               REAL,
    net                 REAL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
, synced_to_stock INTEGER NOT NULL DEFAULT 0, doc_base TEXT, ref_invoice TEXT);

CREATE TABLE salespersons (
    code       TEXT    PRIMARY KEY,                -- numeric route code: '02', '06', '06-L'
    name       TEXT    NOT NULL,                   -- display: 'น้อย /02', 'TOU /06-L'
    is_active  INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE sr_writeoffs (
    id              INTEGER PRIMARY KEY,
    sr_doc_base     TEXT    NOT NULL,
    sr_doc_no       TEXT    NOT NULL,
    reason          TEXT    NOT NULL CHECK(reason IN ('pre_system','no_ref')),
    ref_invoice_raw TEXT,
    net_amount      REAL    NOT NULL DEFAULT 0.0,
    customer        TEXT,
    sr_date_iso     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(sr_doc_no)
);

CREATE TABLE stock_levels (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id),
    quantity    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE supplier_catalogue_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    name_raw            TEXT    NOT NULL,           -- as it appeared in catalogue (latest seen)
    name_normalized     TEXT    NOT NULL,           -- stable identity within supplier
    name_tokens         TEXT,                        -- JSON array of tokens, for fuzzy search
    category_hint       TEXT,                        -- sub-category from **markers** (latest seen)
    sheet_name          TEXT,                        -- which Thai-consonant sheet (latest seen)
    unit                TEXT,                        -- e.g. ตัว / โหล / แผ่น
    min_order_qty       REAL,
    list_price          REAL,                        -- latest list price (THB)
    trade_discount_pct  REAL,                        -- e.g. 25.0 means 25%
    cash_discount_pct   REAL,                        -- e.g. 5.0 means 5%
    net_cash_price      REAL,                        -- list_price * (1 - trade) * (1 - cash), latest
    price_change_flag   TEXT CHECK(price_change_flag IN ('same','changed','new','preorder','unknown')),
    first_seen_version_id INTEGER REFERENCES supplier_catalogue_versions(id),
    last_seen_version_id  INTEGER REFERENCES supplier_catalogue_versions(id),
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(supplier_id, name_normalized)
);

CREATE TABLE supplier_catalogue_price_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             INTEGER NOT NULL REFERENCES supplier_catalogue_items(id) ON DELETE CASCADE,
    version_id          INTEGER NOT NULL REFERENCES supplier_catalogue_versions(id) ON DELETE CASCADE,
    list_price          REAL,
    trade_discount_pct  REAL,
    cash_discount_pct   REAL,
    net_cash_price      REAL,
    unit                TEXT,
    price_change_flag   TEXT CHECK(price_change_flag IN ('same','changed','new','preorder','unknown')),
    captured_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(item_id, version_id)
);

CREATE TABLE supplier_catalogue_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    source_file     TEXT    NOT NULL,           -- original filename
    catalogue_date  TEXT,                        -- ISO YYYY-MM (the month the catalogue covers)
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    imported_by     TEXT,                        -- session username
    note            TEXT
);

CREATE TABLE supplier_product_mapping (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    catalogue_item_id   INTEGER REFERENCES supplier_catalogue_items(id) ON DELETE CASCADE,
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,
    purchase_name_raw   TEXT,
    supplier_unit       TEXT,
    erp_unit            TEXT,
    ratio               REAL DEFAULT 1.0,
    is_ignored          INTEGER NOT NULL DEFAULT 0 CHECK(is_ignored IN (0,1)),
    confidence          TEXT CHECK(confidence IN ('manual','suggested','imported')),
    note                TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK(catalogue_item_id IS NOT NULL OR purchase_name_raw IS NOT NULL),
    CHECK(catalogue_item_id IS NOT NULL OR product_id IS NOT NULL OR is_ignored = 1),
    UNIQUE(supplier_id, catalogue_item_id, purchase_name_raw)
);

CREATE TABLE supplier_quick_updates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    item_id             INTEGER REFERENCES supplier_catalogue_items(id) ON DELETE SET NULL,
    name_raw            TEXT    NOT NULL,           -- in case item not yet in catalogue
    new_list_price      REAL,
    new_net_cash_price  REAL,
    effective_date      TEXT,                        -- ISO date when change takes effect
    source              TEXT,                        -- 'line', 'phone', 'sms', etc.
    note                TEXT,
    captured_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    captured_by         TEXT
);

CREATE TABLE suppliers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,   -- exact name as it appears in purchase_transactions.supplier
    display_name    TEXT,                       -- optional friendlier label
    contact_info    TEXT,                       -- free-form (phone, line, address)
    note            TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
, code TEXT, tax_id TEXT, payment_terms_days INTEGER, default_currency TEXT NOT NULL DEFAULT 'THB');

CREATE TABLE transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    txn_type        TEXT    NOT NULL CHECK(txn_type IN ('IN','OUT','ADJUST')),
    quantity_change INTEGER NOT NULL,
    unit_mode       TEXT    NOT NULL CHECK(unit_mode IN ('unit','box','carton')),
    reference_no    TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE txn_review_docs (
    doc_base        TEXT PRIMARY KEY,
    date_iso        TEXT NOT NULL,
    customer        TEXT,
    customer_code   TEXT,
    line_count      INTEGER NOT NULL DEFAULT 0,
    flag_count      INTEGER NOT NULL DEFAULT 0,
    max_severity    TEXT,
    free_goods_note TEXT,
    scanned_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE txn_review_flags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_base      TEXT NOT NULL REFERENCES txn_review_docs(doc_base) ON DELETE CASCADE,
    txn_id        INTEGER,
    doc_no        TEXT NOT NULL,
    rule_code     TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
    message_th    TEXT NOT NULL,
    details_json  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE unit_conversions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    bsn_unit    TEXT    NOT NULL,
    ratio       REAL    NOT NULL DEFAULT 1.0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(product_id, bsn_unit)
);

CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    display_name  TEXT,
    role          TEXT    NOT NULL DEFAULT 'staff'
                          CHECK(role IN ('admin','manager','staff')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_ar_followup_customer       ON ar_followup_log(customer);

CREATE INDEX idx_ar_followup_log_date       ON ar_followup_log(log_date DESC);

CREATE INDEX idx_ar_followup_next_action    ON ar_followup_log(next_action_date)
    WHERE next_action_date IS NOT NULL;

CREATE INDEX idx_ar_writeoffs_customer ON ar_writeoffs(customer_code);

CREATE INDEX idx_ar_writeoffs_doc      ON ar_writeoffs(doc_no);

CREATE INDEX idx_audit_log_table_time
    ON audit_log(table_name, created_at DESC);

CREATE INDEX idx_audit_table_row ON audit_log(table_name, row_id);

CREATE UNIQUE INDEX idx_brands_short_code
    ON brands(short_code) WHERE short_code IS NOT NULL;

CREATE INDEX idx_call_log_customer ON customer_call_log(customer_code, created_at);

CREATE INDEX idx_cashbook_accounts_code ON cashbook_accounts(code);

CREATE INDEX idx_cashbook_txn_account_date ON cashbook_transactions(account_id, txn_date);

CREATE INDEX idx_cashbook_txn_category     ON cashbook_transactions(category);

CREATE INDEX idx_cashbook_txn_date         ON cashbook_transactions(txn_date);

CREATE INDEX idx_catalogue_items_name_norm
    ON supplier_catalogue_items(name_normalized);

CREATE INDEX idx_catalogue_items_supplier_active
    ON supplier_catalogue_items(supplier_id, is_active);

CREATE INDEX idx_catalogue_price_history_item
    ON supplier_catalogue_price_history(item_id, version_id);

CREATE INDEX idx_catalogue_versions_supplier
    ON supplier_catalogue_versions(supplier_id, catalogue_date);

CREATE UNIQUE INDEX idx_categories_short_code ON categories(short_code) WHERE short_code IS NOT NULL;

CREATE UNIQUE INDEX idx_ccr_customer ON customer_contact_review(customer_code);

CREATE INDEX idx_ccr_status ON customer_contact_review(status, confidence);

CREATE INDEX idx_cna_ref_invoice ON credit_note_amounts(ref_invoice);

CREATE INDEX idx_cni_doc_base  ON credit_note_imports(doc_base);

CREATE INDEX idx_cni_ref_inv   ON credit_note_imports(ref_invoice);

CREATE INDEX idx_co_brand   ON commission_overrides(brand_id);

CREATE INDEX idx_co_product ON commission_overrides(product_id);

CREATE INDEX idx_company_holidays_company ON company_holidays(company_id);

CREATE INDEX idx_cp_invoice_sp ON commission_payouts(invoice_no, salesperson_code);

CREATE INDEX idx_cp_month_sp ON commission_payouts(year_month, salesperson_code);

CREATE INDEX idx_cp_paid_date ON commission_payouts(paid_date);

CREATE INDEX idx_customers_region ON customers(region_id);

CREATE INDEX idx_el_platform ON ecommerce_listings(platform, product_id);

CREATE INDEX idx_employees_active    ON employees(is_active);

CREATE INDEX idx_employees_company  ON employees(company_id);

CREATE INDEX idx_employees_user      ON employees(user_id);

CREATE INDEX idx_expense_log_category ON expense_log(category_id);

CREATE INDEX idx_expense_log_company  ON expense_log(company_id);

CREATE INDEX idx_expense_log_date     ON expense_log(date_iso);

CREATE INDEX idx_express_ap_doc
    ON express_ap_outstanding(doc_no);

CREATE INDEX idx_express_ap_entity_snapshot
    ON express_ap_outstanding(entity, snapshot_date_iso);

CREATE INDEX idx_express_ap_supplier
    ON express_ap_outstanding(supplier_id);

CREATE INDEX idx_express_ar_customer ON express_ar_outstanding(customer_id);

CREATE INDEX idx_express_ar_doc ON express_ar_outstanding(doc_no);

CREATE INDEX idx_express_ar_entity_snapshot
    ON express_ar_outstanding(entity, snapshot_date_iso);

CREATE INDEX idx_express_ar_snapshot ON express_ar_outstanding(snapshot_date_iso, customer_code);

CREATE INDEX idx_express_cn_date ON express_credit_notes(date_iso);

CREATE INDEX idx_express_cn_doc ON express_credit_notes(doc_no);

CREATE INDEX idx_express_cn_line_cn ON express_credit_note_lines(credit_note_id);

CREATE INDEX idx_express_cn_line_product ON express_credit_note_lines(product_code);

CREATE INDEX idx_express_cn_supplier ON express_credit_notes(supplier_id);

CREATE INDEX idx_express_import_log_type ON express_import_log(file_type, imported_at DESC);

CREATE INDEX idx_express_pin_customer ON express_payments_in(customer_id);

CREATE INDEX idx_express_pin_date ON express_payments_in(date_iso);

CREATE INDEX idx_express_pin_doc ON express_payments_in(doc_no);

CREATE INDEX idx_express_pin_ref_inv ON express_payment_in_invoice_refs(invoice_no);

CREATE INDEX idx_express_pin_ref_pid ON express_payment_in_invoice_refs(payment_in_id);

CREATE INDEX idx_express_pin_sp ON express_payments_in(salesperson_code, date_iso);

CREATE INDEX idx_express_pout_date ON express_payments_out(date_iso);

CREATE INDEX idx_express_pout_doc ON express_payments_out(doc_no);

CREATE INDEX idx_express_pout_ref_doc ON express_payment_out_receive_refs(receive_doc);

CREATE INDEX idx_express_pout_ref_pid ON express_payment_out_receive_refs(payment_out_id);

CREATE INDEX idx_express_pout_supplier ON express_payments_out(supplier_id);

CREATE INDEX idx_express_sales_customer ON express_sales(customer_id);

CREATE INDEX idx_express_sales_date ON express_sales(date_iso);

CREATE INDEX idx_express_sales_doc ON express_sales(doc_no);

CREATE INDEX idx_express_sales_doctype ON express_sales(doc_type, date_iso);

CREATE INDEX idx_express_sales_product ON express_sales(product_id);

CREATE INDEX idx_leave_entl_emp ON employee_leave_entitlements(employee_id);

CREATE INDEX idx_leave_req_dates  ON leave_requests(start_date, end_date);

CREATE INDEX idx_leave_req_emp    ON leave_requests(employee_id);

CREATE INDEX idx_leave_req_type   ON leave_requests(leave_type_id);

CREATE INDEX idx_listing_bundles_listing ON listing_bundles(listing_id);

CREATE INDEX idx_marketplace_items_order
    ON marketplace_order_items(order_id);

CREATE INDEX idx_marketplace_items_unmapped
    ON marketplace_order_items(internal_product_id) WHERE internal_product_id IS NULL;

CREATE INDEX idx_marketplace_order_invoice_doc_base
    ON marketplace_order_invoice(doc_base);

CREATE INDEX idx_marketplace_orders_date
    ON marketplace_orders(platform, order_date DESC);

CREATE INDEX idx_marketplace_orders_payout_batch
    ON marketplace_orders(payout_batch_id);

CREATE INDEX idx_marketplace_orders_payout_id ON marketplace_orders(payout_id);

CREATE INDEX idx_marketplace_orders_status
    ON marketplace_orders(status);

CREATE INDEX idx_payroll_items_emp ON payroll_items(employee_id);

CREATE INDEX idx_payroll_items_run ON payroll_items(run_id);

CREATE INDEX idx_payroll_runs_company ON payroll_runs(company_id);

CREATE INDEX idx_pcl_product ON product_cost_ledger(product_id, event_date, id);

CREATE INDEX idx_pi_doc_no ON paid_invoices(doc_no);

CREATE INDEX idx_platform_products_parent_sku
    ON platform_products(platform, parent_sku);

CREATE INDEX idx_po_company    ON purchase_orders(company_id);

CREATE INDEX idx_po_order_date ON purchase_orders(order_date);

CREATE INDEX idx_po_status     ON purchase_orders(status);

CREATE INDEX idx_po_supplier   ON purchase_orders(supplier_id);

CREATE INDEX idx_pol_po      ON purchase_order_lines(po_id);

CREATE INDEX idx_pol_product ON purchase_order_lines(product_id);

CREATE INDEX idx_por_date ON po_receipts(received_date);

CREATE INDEX idx_por_line ON po_receipts(line_id);

CREATE INDEX idx_pph_product_time
    ON product_price_history(product_id, changed_at DESC);

CREATE INDEX idx_pps_bsn_code ON pending_product_suggestions(bsn_code);

CREATE INDEX idx_pps_status ON pending_product_suggestions(status);

CREATE INDEX idx_product_barcodes_product ON product_barcodes(product_id);

CREATE INDEX idx_product_families_brand ON product_families(brand_id);

CREATE INDEX idx_product_images_family ON product_images(family_id);

CREATE INDEX idx_product_images_sku    ON product_images(sku_id);

CREATE INDEX idx_product_price_tiers_product ON product_price_tiers(product_id);

CREATE INDEX idx_products_brand        ON products(brand_id);

CREATE INDEX idx_products_category     ON products(category_id);

CREATE INDEX idx_products_color_code   ON products(color_code);

CREATE INDEX idx_products_family       ON products(family_id);

CREATE INDEX idx_products_packaging_th ON products(packaging_th);

CREATE UNIQUE INDEX idx_products_sku_code ON products(sku_code) WHERE sku_code IS NOT NULL;

CREATE INDEX idx_products_sub_category ON products(sub_category);

CREATE INDEX idx_promotions_active  ON promotions(is_active, product_id);

CREATE INDEX idx_promotions_product ON promotions(product_id);

CREATE INDEX idx_pt_date_iso
    ON purchase_transactions(date_iso);

CREATE INDEX idx_pt_doc_base ON purchase_transactions(doc_base);

CREATE INDEX idx_pt_supplier_id ON purchase_transactions(supplier_id);

CREATE INDEX idx_quick_updates_supplier
    ON supplier_quick_updates(supplier_id, effective_date);

CREATE INDEX idx_regions_parent ON regions(parent_id);

CREATE INDEX idx_salary_advances_emp ON salary_advances(employee_id);

CREATE INDEX idx_salary_advances_run ON salary_advances(deducted_in_run_id);

CREATE INDEX idx_salary_hist_emp ON employee_salary_history(employee_id);

CREATE INDEX idx_srwo_doc_base ON sr_writeoffs(sr_doc_base);

CREATE INDEX idx_srwo_reason   ON sr_writeoffs(reason);

CREATE INDEX idx_st_customer
    ON sales_transactions(customer);

CREATE INDEX idx_st_customer_code
    ON sales_transactions(customer_code);

CREATE INDEX idx_st_date_iso
    ON sales_transactions(date_iso);

CREATE INDEX idx_st_doc_base ON sales_transactions(doc_base);

CREATE INDEX idx_st_product_id
    ON sales_transactions(product_id);

CREATE INDEX idx_st_ref_invoice ON sales_transactions(ref_invoice);

CREATE INDEX idx_supplier_mapping_catalogue ON supplier_product_mapping(catalogue_item_id);

CREATE INDEX idx_supplier_mapping_product ON supplier_product_mapping(product_id);

CREATE INDEX idx_supplier_mapping_purchase ON supplier_product_mapping(supplier_id, purchase_name_raw);

CREATE UNIQUE INDEX idx_suppliers_code ON suppliers(code) WHERE code IS NOT NULL;

CREATE INDEX idx_txn_created_at
    ON transactions(created_at);

CREATE INDEX idx_txn_product_id
    ON transactions(product_id);

CREATE INDEX idx_txn_review_docs_date ON txn_review_docs(date_iso DESC);

CREATE INDEX idx_txn_review_flags_doc ON txn_review_flags(doc_base);

CREATE INDEX idx_wallet_txns_platform_time ON marketplace_wallet_txns(platform, txn_time, id);

CREATE TRIGGER after_transaction_delete
AFTER DELETE ON transactions
BEGIN
    UPDATE stock_levels
       SET quantity = ROUND(quantity - OLD.quantity_change, 4)
     WHERE product_id = OLD.product_id;
END;

CREATE TRIGGER after_transaction_insert
    AFTER INSERT ON transactions
    BEGIN
        INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0)
            ON CONFLICT(product_id) DO NOTHING;
        UPDATE stock_levels
           SET quantity = ROUND(quantity + NEW.quantity_change, 4)
         WHERE product_id = NEW.product_id;
    END;

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

CREATE TRIGGER audit_commission_assignments_delete
BEFORE DELETE ON commission_assignments
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_assignments', OLD.rowid, 'DELETE',
        json_object('salesperson_code', OLD.salesperson_code, 'tier_id', OLD.tier_id));
END;

CREATE TRIGGER audit_commission_assignments_insert
AFTER INSERT ON commission_assignments
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_assignments', NEW.rowid, 'INSERT',
        json_object('salesperson_code', NEW.salesperson_code,
                    'tier_id', NEW.tier_id,
                    'effective_from', NEW.effective_from,
                    'note', NEW.note));
END;

CREATE TRIGGER audit_commission_assignments_update
AFTER UPDATE ON commission_assignments
WHEN (
       OLD.tier_id        IS NOT NEW.tier_id
    OR OLD.effective_from IS NOT NEW.effective_from
    OR OLD.note           IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'commission_assignments', NEW.rowid, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'tier_id'        AS field, OLD.tier_id        AS old_v, NEW.tier_id        AS new_v WHERE OLD.tier_id        IS NOT NEW.tier_id
        UNION ALL SELECT 'effective_from',          OLD.effective_from,          NEW.effective_from          WHERE OLD.effective_from IS NOT NEW.effective_from
        UNION ALL SELECT 'note',                    OLD.note,                    NEW.note                    WHERE OLD.note           IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_commission_overrides_delete
BEFORE DELETE ON commission_overrides
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_overrides', OLD.id, 'DELETE',
        json_object(
            'product_id',       OLD.product_id,
            'brand_id',         OLD.brand_id,
            'salesperson_code', OLD.salesperson_code,
            'fixed_per_unit',   OLD.fixed_per_unit,
            'custom_rate_pct',  OLD.custom_rate_pct,
            'note',             OLD.note
        ));
END;

CREATE TRIGGER audit_commission_overrides_insert
AFTER INSERT ON commission_overrides
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_overrides', NEW.id, 'INSERT',
        json_object(
            'product_id',           NEW.product_id,
            'brand_id',             NEW.brand_id,
            'salesperson_code',     NEW.salesperson_code,
            'fixed_per_unit',       NEW.fixed_per_unit,
            'custom_rate_pct',      NEW.custom_rate_pct,
            'apply_when_price_gt',  NEW.apply_when_price_gt,
            'apply_when_price_lte', NEW.apply_when_price_lte,
            'is_active',            NEW.is_active,
            'effective_from',       NEW.effective_from,
            'note',                 NEW.note
        ));
END;

CREATE TRIGGER audit_commission_overrides_update
AFTER UPDATE ON commission_overrides
WHEN (
       OLD.product_id           IS NOT NEW.product_id
    OR OLD.brand_id             IS NOT NEW.brand_id
    OR OLD.salesperson_code     IS NOT NEW.salesperson_code
    OR OLD.fixed_per_unit       IS NOT NEW.fixed_per_unit
    OR OLD.custom_rate_pct      IS NOT NEW.custom_rate_pct
    OR OLD.apply_when_price_gt  IS NOT NEW.apply_when_price_gt
    OR OLD.apply_when_price_lte IS NOT NEW.apply_when_price_lte
    OR OLD.is_active            IS NOT NEW.is_active
    OR OLD.effective_from       IS NOT NEW.effective_from
    OR OLD.note                 IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'commission_overrides', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'product_id'           AS field, OLD.product_id           AS old_v, NEW.product_id           AS new_v WHERE OLD.product_id           IS NOT NEW.product_id
        UNION ALL SELECT 'brand_id',                     OLD.brand_id,                     NEW.brand_id                     WHERE OLD.brand_id             IS NOT NEW.brand_id
        UNION ALL SELECT 'salesperson_code',             OLD.salesperson_code,             NEW.salesperson_code             WHERE OLD.salesperson_code     IS NOT NEW.salesperson_code
        UNION ALL SELECT 'fixed_per_unit',               OLD.fixed_per_unit,               NEW.fixed_per_unit               WHERE OLD.fixed_per_unit       IS NOT NEW.fixed_per_unit
        UNION ALL SELECT 'custom_rate_pct',              OLD.custom_rate_pct,              NEW.custom_rate_pct              WHERE OLD.custom_rate_pct      IS NOT NEW.custom_rate_pct
        UNION ALL SELECT 'apply_when_price_gt',          OLD.apply_when_price_gt,          NEW.apply_when_price_gt          WHERE OLD.apply_when_price_gt  IS NOT NEW.apply_when_price_gt
        UNION ALL SELECT 'apply_when_price_lte',         OLD.apply_when_price_lte,         NEW.apply_when_price_lte         WHERE OLD.apply_when_price_lte IS NOT NEW.apply_when_price_lte
        UNION ALL SELECT 'is_active',                    OLD.is_active,                    NEW.is_active                    WHERE OLD.is_active            IS NOT NEW.is_active
        UNION ALL SELECT 'effective_from',               OLD.effective_from,               NEW.effective_from               WHERE OLD.effective_from       IS NOT NEW.effective_from
        UNION ALL SELECT 'note',                         OLD.note,                         NEW.note                         WHERE OLD.note                 IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_commission_payouts_delete
BEFORE DELETE ON commission_payouts
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_payouts', OLD.id, 'DELETE',
        json_object('year_month', OLD.year_month,
                    'salesperson_code', OLD.salesperson_code,
                    'amount_paid', OLD.amount_paid,
                    'paid_date', OLD.paid_date));
END;

CREATE TRIGGER audit_commission_payouts_insert
AFTER INSERT ON commission_payouts
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields, user)
    VALUES ('commission_payouts', NEW.id, 'INSERT',
        json_object('year_month', NEW.year_month,
                    'salesperson_code', NEW.salesperson_code,
                    'amount_paid', NEW.amount_paid,
                    'paid_date', NEW.paid_date,
                    'paid_method', NEW.paid_method),
        NEW.paid_by);
END;

CREATE TRIGGER audit_commission_payouts_update
AFTER UPDATE ON commission_payouts
WHEN (
       OLD.amount_paid  IS NOT NEW.amount_paid
    OR OLD.paid_date    IS NOT NEW.paid_date
    OR OLD.paid_method  IS NOT NEW.paid_method
    OR OLD.note         IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'commission_payouts', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'amount_paid' AS field, OLD.amount_paid AS old_v, NEW.amount_paid AS new_v WHERE OLD.amount_paid IS NOT NEW.amount_paid
        UNION ALL SELECT 'paid_date',            OLD.paid_date,            NEW.paid_date            WHERE OLD.paid_date    IS NOT NEW.paid_date
        UNION ALL SELECT 'paid_method',          OLD.paid_method,          NEW.paid_method          WHERE OLD.paid_method  IS NOT NEW.paid_method
        UNION ALL SELECT 'note',                 OLD.note,                 NEW.note                 WHERE OLD.note         IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_commission_tiers_delete
BEFORE DELETE ON commission_tiers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_tiers', OLD.id, 'DELETE',
        json_object('code', OLD.code, 'name_th', OLD.name_th));
END;

CREATE TRIGGER audit_commission_tiers_insert
AFTER INSERT ON commission_tiers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_tiers', NEW.id, 'INSERT',
        json_object('code', NEW.code, 'name_th', NEW.name_th,
                    'rate_own_pct', NEW.rate_own_pct,
                    'rate_third_pct', NEW.rate_third_pct,
                    'threshold_amount', NEW.threshold_amount,
                    'above_rate_own_pct', NEW.above_rate_own_pct,
                    'above_rate_third_pct', NEW.above_rate_third_pct));
END;

CREATE TRIGGER audit_commission_tiers_update
AFTER UPDATE ON commission_tiers
WHEN (
       OLD.code                  IS NOT NEW.code
    OR OLD.name_th               IS NOT NEW.name_th
    OR OLD.description           IS NOT NEW.description
    OR OLD.rate_own_pct          IS NOT NEW.rate_own_pct
    OR OLD.rate_third_pct        IS NOT NEW.rate_third_pct
    OR OLD.threshold_amount      IS NOT NEW.threshold_amount
    OR OLD.above_rate_own_pct    IS NOT NEW.above_rate_own_pct
    OR OLD.above_rate_third_pct  IS NOT NEW.above_rate_third_pct
    OR OLD.is_active             IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'commission_tiers', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'                  AS field, OLD.code                  AS old_v, NEW.code                  AS new_v WHERE OLD.code                  IS NOT NEW.code
        UNION ALL SELECT 'name_th',                       OLD.name_th,                       NEW.name_th                       WHERE OLD.name_th               IS NOT NEW.name_th
        UNION ALL SELECT 'description',                   OLD.description,                   NEW.description                   WHERE OLD.description           IS NOT NEW.description
        UNION ALL SELECT 'rate_own_pct',                  OLD.rate_own_pct,                  NEW.rate_own_pct                  WHERE OLD.rate_own_pct          IS NOT NEW.rate_own_pct
        UNION ALL SELECT 'rate_third_pct',                OLD.rate_third_pct,                NEW.rate_third_pct                WHERE OLD.rate_third_pct        IS NOT NEW.rate_third_pct
        UNION ALL SELECT 'threshold_amount',              OLD.threshold_amount,              NEW.threshold_amount              WHERE OLD.threshold_amount      IS NOT NEW.threshold_amount
        UNION ALL SELECT 'above_rate_own_pct',            OLD.above_rate_own_pct,            NEW.above_rate_own_pct            WHERE OLD.above_rate_own_pct    IS NOT NEW.above_rate_own_pct
        UNION ALL SELECT 'above_rate_third_pct',          OLD.above_rate_third_pct,          NEW.above_rate_third_pct          WHERE OLD.above_rate_third_pct  IS NOT NEW.above_rate_third_pct
        UNION ALL SELECT 'is_active',                     OLD.is_active,                     NEW.is_active                     WHERE OLD.is_active             IS NOT NEW.is_active
    );
END;

CREATE TRIGGER audit_companies_delete
BEFORE DELETE ON companies
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'companies', OLD.id, 'DELETE',
        json_object('code', OLD.code, 'name_th', OLD.name_th, 'tax_id', OLD.tax_id)
    );
END;

CREATE TRIGGER audit_companies_insert
AFTER INSERT ON companies
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'companies', NEW.id, 'INSERT',
        json_object(
            'code', NEW.code, 'name_th', NEW.name_th,
            'short_name', NEW.short_name, 'tax_id', NEW.tax_id,
            'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_companies_update
AFTER UPDATE ON companies
WHEN (
       OLD.code       IS NOT NEW.code
    OR OLD.name_th    IS NOT NEW.name_th
    OR OLD.short_name IS NOT NEW.short_name
    OR OLD.tax_id     IS NOT NEW.tax_id
    OR OLD.is_active  IS NOT NEW.is_active
    OR OLD.note       IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'companies', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'       AS field, OLD.code       AS old_v, NEW.code       AS new_v WHERE OLD.code       IS NOT NEW.code
        UNION ALL SELECT 'name_th',             OLD.name_th,            NEW.name_th            WHERE OLD.name_th    IS NOT NEW.name_th
        UNION ALL SELECT 'short_name',          OLD.short_name,         NEW.short_name         WHERE OLD.short_name IS NOT NEW.short_name
        UNION ALL SELECT 'tax_id',              OLD.tax_id,             NEW.tax_id             WHERE OLD.tax_id     IS NOT NEW.tax_id
        UNION ALL SELECT 'is_active',           OLD.is_active,          NEW.is_active          WHERE OLD.is_active  IS NOT NEW.is_active
        UNION ALL SELECT 'note',                OLD.note,               NEW.note               WHERE OLD.note       IS NOT NEW.note
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

CREATE TRIGGER audit_customer_crm_delete
BEFORE DELETE ON customer_crm
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customer_crm',
        (SELECT rowid FROM customer_crm WHERE customer_code = OLD.customer_code),
        'DELETE',
        json_object(
            'customer_code', OLD.customer_code,
            'tags', OLD.tags,
            'next_call_date', OLD.next_call_date,
            'call_target_days', OLD.call_target_days
        )
    );
END;

CREATE TRIGGER audit_customer_crm_insert
AFTER INSERT ON customer_crm
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customer_crm',
        (SELECT MAX(rowid) FROM customer_crm WHERE customer_code = NEW.customer_code),
        'INSERT',
        json_object(
            'customer_code', NEW.customer_code,
            'tags', NEW.tags,
            'next_call_date', NEW.next_call_date,
            'call_target_days', NEW.call_target_days,
            'updated_by', NEW.updated_by
        )
    );
END;

CREATE TRIGGER audit_customer_crm_update
AFTER UPDATE ON customer_crm
WHEN (
       OLD.tags             IS NOT NEW.tags
    OR OLD.next_call_date   IS NOT NEW.next_call_date
    OR OLD.call_target_days IS NOT NEW.call_target_days
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'customer_crm',
           (SELECT rowid FROM customer_crm WHERE customer_code = NEW.customer_code),
           'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'tags'             AS field, OLD.tags             AS old_v, NEW.tags             AS new_v WHERE OLD.tags             IS NOT NEW.tags
        UNION ALL SELECT 'next_call_date',            OLD.next_call_date,            NEW.next_call_date            WHERE OLD.next_call_date   IS NOT NEW.next_call_date
        UNION ALL SELECT 'call_target_days',          OLD.call_target_days,          NEW.call_target_days          WHERE OLD.call_target_days IS NOT NEW.call_target_days
    );
END;

CREATE TRIGGER audit_customers_delete
BEFORE DELETE ON customers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customers',
        (SELECT rowid FROM customers WHERE code = OLD.code),
        'DELETE',
        json_object(
            'code', OLD.code, 'name', OLD.name, 'phone', OLD.phone,
            'address', OLD.address, 'tax_id', OLD.tax_id
        )
    );
END;

CREATE TRIGGER audit_customers_insert
AFTER INSERT ON customers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customers',
        (SELECT MAX(rowid) FROM customers WHERE code = NEW.code),
        'INSERT',
        json_object(
            'code', NEW.code, 'name', NEW.name,
            'salesperson', NEW.salesperson, 'zone', NEW.zone,
            'phone', NEW.phone, 'credit_days', NEW.credit_days
        )
    );
END;

CREATE TRIGGER audit_customers_update
AFTER UPDATE ON customers
WHEN (
       OLD.name         IS NOT NEW.name
    OR OLD.salesperson  IS NOT NEW.salesperson
    OR OLD.zone         IS NOT NEW.zone
    OR OLD.region_id    IS NOT NEW.region_id
    OR OLD.address      IS NOT NEW.address
    OR OLD.phone        IS NOT NEW.phone
    OR OLD.fax          IS NOT NEW.fax
    OR OLD.nickname     IS NOT NEW.nickname
    OR OLD.contact_note IS NOT NEW.contact_note
    OR OLD.tax_id       IS NOT NEW.tax_id
    OR OLD.credit_days  IS NOT NEW.credit_days
    OR OLD.contact      IS NOT NEW.contact
    OR OLD.lat          IS NOT NEW.lat
    OR OLD.lng          IS NOT NEW.lng
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'customers',
           (SELECT rowid FROM customers WHERE code = NEW.code),
           'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'name'         AS field, OLD.name         AS old_v, NEW.name         AS new_v WHERE OLD.name         IS NOT NEW.name
        UNION ALL SELECT 'salesperson',           OLD.salesperson,          NEW.salesperson          WHERE OLD.salesperson  IS NOT NEW.salesperson
        UNION ALL SELECT 'zone',                  OLD.zone,                 NEW.zone                 WHERE OLD.zone         IS NOT NEW.zone
        UNION ALL SELECT 'region_id',             OLD.region_id,            NEW.region_id            WHERE OLD.region_id    IS NOT NEW.region_id
        UNION ALL SELECT 'address',               OLD.address,              NEW.address              WHERE OLD.address      IS NOT NEW.address
        UNION ALL SELECT 'phone',                 OLD.phone,                NEW.phone                WHERE OLD.phone        IS NOT NEW.phone
        UNION ALL SELECT 'fax',                   OLD.fax,                  NEW.fax                  WHERE OLD.fax          IS NOT NEW.fax
        UNION ALL SELECT 'nickname',              OLD.nickname,             NEW.nickname             WHERE OLD.nickname     IS NOT NEW.nickname
        UNION ALL SELECT 'contact_note',          OLD.contact_note,         NEW.contact_note         WHERE OLD.contact_note IS NOT NEW.contact_note
        UNION ALL SELECT 'tax_id',                OLD.tax_id,               NEW.tax_id               WHERE OLD.tax_id       IS NOT NEW.tax_id
        UNION ALL SELECT 'credit_days',           OLD.credit_days,          NEW.credit_days          WHERE OLD.credit_days  IS NOT NEW.credit_days
        UNION ALL SELECT 'contact',               OLD.contact,              NEW.contact              WHERE OLD.contact      IS NOT NEW.contact
        UNION ALL SELECT 'lat',                   OLD.lat,                  NEW.lat                  WHERE OLD.lat          IS NOT NEW.lat
        UNION ALL SELECT 'lng',                   OLD.lng,                  NEW.lng                  WHERE OLD.lng          IS NOT NEW.lng
    );
END;

CREATE TRIGGER audit_employee_salary_history_delete
BEFORE DELETE ON employee_salary_history
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employee_salary_history', OLD.id, 'DELETE',
        json_object(
            'employee_id',     OLD.employee_id,
            'effective_date',  OLD.effective_date,
            'monthly_salary',  OLD.monthly_salary,
            'reason',          OLD.reason
        )
    );
END;

CREATE TRIGGER audit_employee_salary_history_insert
AFTER INSERT ON employee_salary_history
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employee_salary_history', NEW.id, 'INSERT',
        json_object(
            'employee_id',     NEW.employee_id,
            'effective_date',  NEW.effective_date,
            'monthly_salary',  NEW.monthly_salary,
            'reason',          NEW.reason,
            'note',            NEW.note
        )
    );
END;

CREATE TRIGGER audit_employee_salary_history_update
AFTER UPDATE ON employee_salary_history
WHEN (
       OLD.monthly_salary  IS NOT NEW.monthly_salary
    OR OLD.effective_date  IS NOT NEW.effective_date
    OR OLD.reason          IS NOT NEW.reason
    OR OLD.note            IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'employee_salary_history', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'monthly_salary'  AS field, OLD.monthly_salary  AS old_v, NEW.monthly_salary  AS new_v WHERE OLD.monthly_salary  IS NOT NEW.monthly_salary
        UNION ALL SELECT 'effective_date',  OLD.effective_date,  NEW.effective_date  WHERE OLD.effective_date  IS NOT NEW.effective_date
        UNION ALL SELECT 'reason',          OLD.reason,          NEW.reason          WHERE OLD.reason          IS NOT NEW.reason
        UNION ALL SELECT 'note',            OLD.note,            NEW.note            WHERE OLD.note            IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_employees_delete
BEFORE DELETE ON employees
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employees', OLD.id, 'DELETE',
        json_object('emp_code', OLD.emp_code, 'full_name', OLD.full_name, 'is_active', OLD.is_active)
    );
END;

CREATE TRIGGER audit_employees_insert
AFTER INSERT ON employees
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employees', NEW.id, 'INSERT',
        json_object(
            'emp_code',            NEW.emp_code,
            'full_name',           NEW.full_name,
            'nickname',            NEW.nickname,
            'company_id',          NEW.company_id,
            'employment_type',     NEW.employment_type,
            'start_date',          NEW.start_date,
            'end_date',            NEW.end_date,
            'sso_enrolled',        NEW.sso_enrolled,
            'diligence_allowance', NEW.diligence_allowance,
            'is_active',           NEW.is_active,
            'salesperson_code',    NEW.salesperson_code
        )
    );
END;

CREATE TRIGGER audit_employees_update
AFTER UPDATE ON employees
WHEN (
       OLD.full_name             IS NOT NEW.full_name
    OR OLD.nickname               IS NOT NEW.nickname
    OR OLD.national_id            IS NOT NEW.national_id
    OR OLD.phone                  IS NOT NEW.phone
    OR OLD.address                IS NOT NEW.address
    OR OLD.position               IS NOT NEW.position
    OR OLD.company_id             IS NOT NEW.company_id
    OR OLD.employment_type        IS NOT NEW.employment_type
    OR OLD.start_date             IS NOT NEW.start_date
    OR OLD.end_date               IS NOT NEW.end_date
    OR OLD.probation_end_date     IS NOT NEW.probation_end_date
    OR OLD.sso_enrolled           IS NOT NEW.sso_enrolled
    OR OLD.diligence_allowance    IS NOT NEW.diligence_allowance
    OR OLD.bank_name              IS NOT NEW.bank_name
    OR OLD.bank_branch            IS NOT NEW.bank_branch
    OR OLD.bank_account_no        IS NOT NEW.bank_account_no
    OR OLD.bank_account_name      IS NOT NEW.bank_account_name
    OR OLD.salesperson_code       IS NOT NEW.salesperson_code
    OR OLD.user_id                IS NOT NEW.user_id
    OR OLD.is_active              IS NOT NEW.is_active
    OR OLD.note                   IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'employees', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'full_name'             AS field, OLD.full_name             AS old_v, NEW.full_name             AS new_v WHERE OLD.full_name             IS NOT NEW.full_name
        UNION ALL SELECT 'nickname',              OLD.nickname,              NEW.nickname              WHERE OLD.nickname               IS NOT NEW.nickname
        UNION ALL SELECT 'national_id',           OLD.national_id,           NEW.national_id           WHERE OLD.national_id            IS NOT NEW.national_id
        UNION ALL SELECT 'phone',                 OLD.phone,                 NEW.phone                 WHERE OLD.phone                  IS NOT NEW.phone
        UNION ALL SELECT 'address',               OLD.address,               NEW.address               WHERE OLD.address                IS NOT NEW.address
        UNION ALL SELECT 'position',              OLD.position,              NEW.position              WHERE OLD.position               IS NOT NEW.position
        UNION ALL SELECT 'company_id',            OLD.company_id,            NEW.company_id            WHERE OLD.company_id             IS NOT NEW.company_id
        UNION ALL SELECT 'employment_type',       OLD.employment_type,       NEW.employment_type       WHERE OLD.employment_type        IS NOT NEW.employment_type
        UNION ALL SELECT 'start_date',            OLD.start_date,            NEW.start_date            WHERE OLD.start_date             IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',              OLD.end_date,              NEW.end_date              WHERE OLD.end_date               IS NOT NEW.end_date
        UNION ALL SELECT 'probation_end_date',    OLD.probation_end_date,    NEW.probation_end_date    WHERE OLD.probation_end_date     IS NOT NEW.probation_end_date
        UNION ALL SELECT 'sso_enrolled',          OLD.sso_enrolled,          NEW.sso_enrolled          WHERE OLD.sso_enrolled           IS NOT NEW.sso_enrolled
        UNION ALL SELECT 'diligence_allowance',   OLD.diligence_allowance,   NEW.diligence_allowance   WHERE OLD.diligence_allowance    IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'bank_name',             OLD.bank_name,             NEW.bank_name             WHERE OLD.bank_name              IS NOT NEW.bank_name
        UNION ALL SELECT 'bank_branch',           OLD.bank_branch,           NEW.bank_branch           WHERE OLD.bank_branch            IS NOT NEW.bank_branch
        UNION ALL SELECT 'bank_account_no',       OLD.bank_account_no,       NEW.bank_account_no       WHERE OLD.bank_account_no        IS NOT NEW.bank_account_no
        UNION ALL SELECT 'bank_account_name',     OLD.bank_account_name,     NEW.bank_account_name     WHERE OLD.bank_account_name      IS NOT NEW.bank_account_name
        UNION ALL SELECT 'salesperson_code',      OLD.salesperson_code,      NEW.salesperson_code      WHERE OLD.salesperson_code       IS NOT NEW.salesperson_code
        UNION ALL SELECT 'user_id',               OLD.user_id,               NEW.user_id               WHERE OLD.user_id                IS NOT NEW.user_id
        UNION ALL SELECT 'is_active',             OLD.is_active,             NEW.is_active             WHERE OLD.is_active              IS NOT NEW.is_active
        UNION ALL SELECT 'note',                  OLD.note,                  NEW.note                  WHERE OLD.note                   IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_expense_categories_delete
BEFORE DELETE ON expense_categories
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_categories', OLD.id, 'DELETE',
        json_object('code', OLD.code, 'name_th', OLD.name_th)
    );
END;

CREATE TRIGGER audit_expense_categories_insert
AFTER INSERT ON expense_categories
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_categories', NEW.id, 'INSERT',
        json_object(
            'code', NEW.code, 'name_th', NEW.name_th,
            'sort_order', NEW.sort_order, 'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_expense_categories_update
AFTER UPDATE ON expense_categories
WHEN (
       OLD.code       IS NOT NEW.code
    OR OLD.name_th    IS NOT NEW.name_th
    OR OLD.sort_order IS NOT NEW.sort_order
    OR OLD.is_active  IS NOT NEW.is_active
    OR OLD.note       IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'expense_categories', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'       AS field, OLD.code       AS old_v, NEW.code       AS new_v WHERE OLD.code       IS NOT NEW.code
        UNION ALL SELECT 'name_th',             OLD.name_th,            NEW.name_th            WHERE OLD.name_th    IS NOT NEW.name_th
        UNION ALL SELECT 'sort_order',          OLD.sort_order,         NEW.sort_order         WHERE OLD.sort_order IS NOT NEW.sort_order
        UNION ALL SELECT 'is_active',           OLD.is_active,          NEW.is_active          WHERE OLD.is_active  IS NOT NEW.is_active
        UNION ALL SELECT 'note',                OLD.note,               NEW.note               WHERE OLD.note       IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_expense_log_delete
BEFORE DELETE ON expense_log
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_log', OLD.id, 'DELETE',
        json_object(
            'date_iso', OLD.date_iso, 'company_id', OLD.company_id,
            'category_id', OLD.category_id, 'amount_pre_vat', OLD.amount_pre_vat,
            'vat_amount', OLD.vat_amount, 'doc_no', OLD.doc_no
        )
    );
END;

CREATE TRIGGER audit_expense_log_insert
AFTER INSERT ON expense_log
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_log', NEW.id, 'INSERT',
        json_object(
            'date_iso', NEW.date_iso, 'company_id', NEW.company_id,
            'category_id', NEW.category_id, 'amount_pre_vat', NEW.amount_pre_vat,
            'vat_amount', NEW.vat_amount, 'description', NEW.description,
            'doc_no', NEW.doc_no, 'created_by', NEW.created_by
        )
    );
END;

CREATE TRIGGER audit_expense_log_update
AFTER UPDATE ON expense_log
WHEN (
       OLD.date_iso       IS NOT NEW.date_iso
    OR OLD.company_id     IS NOT NEW.company_id
    OR OLD.category_id    IS NOT NEW.category_id
    OR OLD.amount_pre_vat IS NOT NEW.amount_pre_vat
    OR OLD.vat_amount     IS NOT NEW.vat_amount
    OR OLD.description    IS NOT NEW.description
    OR OLD.doc_no         IS NOT NEW.doc_no
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'expense_log', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'date_iso'       AS field, OLD.date_iso       AS old_v, NEW.date_iso       AS new_v WHERE OLD.date_iso       IS NOT NEW.date_iso
        UNION ALL SELECT 'company_id',              OLD.company_id,              NEW.company_id              WHERE OLD.company_id     IS NOT NEW.company_id
        UNION ALL SELECT 'category_id',             OLD.category_id,             NEW.category_id             WHERE OLD.category_id    IS NOT NEW.category_id
        UNION ALL SELECT 'amount_pre_vat',          OLD.amount_pre_vat,          NEW.amount_pre_vat          WHERE OLD.amount_pre_vat IS NOT NEW.amount_pre_vat
        UNION ALL SELECT 'vat_amount',              OLD.vat_amount,              NEW.vat_amount              WHERE OLD.vat_amount     IS NOT NEW.vat_amount
        UNION ALL SELECT 'description',             OLD.description,             NEW.description             WHERE OLD.description    IS NOT NEW.description
        UNION ALL SELECT 'doc_no',                  OLD.doc_no,                  NEW.doc_no                  WHERE OLD.doc_no         IS NOT NEW.doc_no
    );
END;

CREATE TRIGGER audit_leave_requests_delete
BEFORE DELETE ON leave_requests
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'leave_requests', OLD.id, 'DELETE',
        json_object('employee_id', OLD.employee_id, 'start_date', OLD.start_date, 'end_date', OLD.end_date, 'status', OLD.status)
    );
END;

CREATE TRIGGER audit_leave_requests_insert
AFTER INSERT ON leave_requests
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'leave_requests', NEW.id, 'INSERT',
        json_object(
            'employee_id',   NEW.employee_id,
            'leave_type_id', NEW.leave_type_id,
            'start_date',    NEW.start_date,
            'end_date',      NEW.end_date,
            'days',          NEW.days,
            'status',        NEW.status
        )
    );
END;

CREATE TRIGGER audit_leave_requests_update
AFTER UPDATE ON leave_requests
WHEN (
       OLD.status            IS NOT NEW.status
    OR OLD.start_date        IS NOT NEW.start_date
    OR OLD.end_date          IS NOT NEW.end_date
    OR OLD.days              IS NOT NEW.days
    OR OLD.leave_type_id     IS NOT NEW.leave_type_id
    OR OLD.reason            IS NOT NEW.reason
    OR OLD.has_medical_cert  IS NOT NEW.has_medical_cert
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'leave_requests', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'status'            AS field, OLD.status            AS old_v, NEW.status            AS new_v WHERE OLD.status            IS NOT NEW.status
        UNION ALL SELECT 'start_date',        OLD.start_date,        NEW.start_date        WHERE OLD.start_date        IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',          OLD.end_date,          NEW.end_date          WHERE OLD.end_date          IS NOT NEW.end_date
        UNION ALL SELECT 'days',              OLD.days,              NEW.days              WHERE OLD.days              IS NOT NEW.days
        UNION ALL SELECT 'leave_type_id',     OLD.leave_type_id,     NEW.leave_type_id     WHERE OLD.leave_type_id     IS NOT NEW.leave_type_id
        UNION ALL SELECT 'reason',            OLD.reason,            NEW.reason            WHERE OLD.reason            IS NOT NEW.reason
        UNION ALL SELECT 'has_medical_cert',  OLD.has_medical_cert,  NEW.has_medical_cert  WHERE OLD.has_medical_cert  IS NOT NEW.has_medical_cert
    );
END;

CREATE TRIGGER audit_listing_bundles_delete
BEFORE DELETE ON listing_bundles
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('listing_bundles', OLD.id, 'DELETE',
        json_object(
            'listing_id',           OLD.listing_id,
            'component_product_id', OLD.component_product_id,
            'qty_per_sale',         OLD.qty_per_sale,
            'note',                 OLD.note
        ));
END;

CREATE TRIGGER audit_listing_bundles_insert
AFTER INSERT ON listing_bundles
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('listing_bundles', NEW.id, 'INSERT',
        json_object(
            'listing_id',           NEW.listing_id,
            'component_product_id', NEW.component_product_id,
            'qty_per_sale',         NEW.qty_per_sale,
            'note',                 NEW.note
        ));
END;

CREATE TRIGGER audit_listing_bundles_update
AFTER UPDATE ON listing_bundles
WHEN (
       OLD.listing_id           IS NOT NEW.listing_id
    OR OLD.component_product_id IS NOT NEW.component_product_id
    OR OLD.qty_per_sale         IS NOT NEW.qty_per_sale
    OR OLD.note                 IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'listing_bundles', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'listing_id'           AS field, OLD.listing_id           AS old_v, NEW.listing_id           AS new_v WHERE OLD.listing_id           IS NOT NEW.listing_id
        UNION ALL SELECT 'component_product_id',         OLD.component_product_id,         NEW.component_product_id         WHERE OLD.component_product_id IS NOT NEW.component_product_id
        UNION ALL SELECT 'qty_per_sale',                 OLD.qty_per_sale,                 NEW.qty_per_sale                 WHERE OLD.qty_per_sale         IS NOT NEW.qty_per_sale
        UNION ALL SELECT 'note',                         OLD.note,                         NEW.note                         WHERE OLD.note                 IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_paid_invoices_delete
BEFORE DELETE ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', OLD.id, 'DELETE',
        json_object(
            're_id',    OLD.re_id,
            'doc_no',   OLD.doc_no,
            'doc_kind', OLD.doc_kind,
            'amount',   OLD.amount
        )
    );
END;

CREATE TRIGGER audit_paid_invoices_insert
AFTER INSERT ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', NEW.id, 'INSERT',
        json_object(
            're_id',    NEW.re_id,
            'doc_no',   NEW.doc_no,
            'doc_kind', NEW.doc_kind,
            'amount',   NEW.amount
        )
    );
END;

CREATE TRIGGER audit_paid_invoices_update
AFTER UPDATE ON paid_invoices
WHEN (
       OLD.re_id    IS NOT NEW.re_id
    OR OLD.doc_no   IS NOT NEW.doc_no
    OR OLD.doc_kind IS NOT NEW.doc_kind
    OR OLD.amount   IS NOT NEW.amount
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'paid_invoices', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 're_id'    AS field, OLD.re_id    AS old_v, NEW.re_id    AS new_v WHERE OLD.re_id    IS NOT NEW.re_id
        UNION ALL SELECT 'doc_no',   OLD.doc_no,   NEW.doc_no   WHERE OLD.doc_no   IS NOT NEW.doc_no
        UNION ALL SELECT 'doc_kind', OLD.doc_kind, NEW.doc_kind WHERE OLD.doc_kind IS NOT NEW.doc_kind
        UNION ALL SELECT 'amount',   OLD.amount,   NEW.amount   WHERE OLD.amount   IS NOT NEW.amount
    );
END;

CREATE TRIGGER audit_payroll_items_delete
BEFORE DELETE ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', OLD.id, 'DELETE',
        json_object(
            'run_id',                    OLD.run_id,
            'employee_id',               OLD.employee_id,
            'salary_rate',               OLD.salary_rate,
            'base_amount',               OLD.base_amount,
            'unpaid_leave_days',         OLD.unpaid_leave_days,
            'unpaid_leave_deduction',    OLD.unpaid_leave_deduction,
            'diligence_allowance',       OLD.diligence_allowance,
            'diligence_forfeited',       OLD.diligence_forfeited,
            'diligence_forfeit_reason',  OLD.diligence_forfeit_reason,
            'bonus',                     OLD.bonus,
            'other_additions',           OLD.other_additions,
            'other_additions_note',      OLD.other_additions_note,
            'other_deductions',          OLD.other_deductions,
            'other_deductions_note',     OLD.other_deductions_note,
            'sso_employee',              OLD.sso_employee,
            'sso_employer',              OLD.sso_employer,
            'commission_amount',         OLD.commission_amount,
            'salary_advance_deduction',  OLD.salary_advance_deduction,
            'gross',                     OLD.gross,
            'net_pay',                   OLD.net_pay,
            'note',                      OLD.note
        )
    );
END;

CREATE TRIGGER audit_payroll_items_insert
AFTER INSERT ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', NEW.id, 'INSERT',
        json_object(
            'run_id',                    NEW.run_id,
            'employee_id',               NEW.employee_id,
            'salary_rate',               NEW.salary_rate,
            'base_amount',               NEW.base_amount,
            'unpaid_leave_days',         NEW.unpaid_leave_days,
            'unpaid_leave_deduction',    NEW.unpaid_leave_deduction,
            'diligence_allowance',       NEW.diligence_allowance,
            'diligence_forfeited',       NEW.diligence_forfeited,
            'diligence_forfeit_reason',  NEW.diligence_forfeit_reason,
            'bonus',                     NEW.bonus,
            'other_additions',           NEW.other_additions,
            'other_additions_note',      NEW.other_additions_note,
            'other_deductions',          NEW.other_deductions,
            'other_deductions_note',     NEW.other_deductions_note,
            'sso_employee',              NEW.sso_employee,
            'sso_employer',              NEW.sso_employer,
            'commission_amount',         NEW.commission_amount,
            'salary_advance_deduction',  NEW.salary_advance_deduction,
            'gross',                     NEW.gross,
            'net_pay',                   NEW.net_pay,
            'note',                      NEW.note
        )
    );
END;

CREATE TRIGGER audit_payroll_items_update
AFTER UPDATE ON payroll_items
WHEN (
       OLD.bonus                       IS NOT NEW.bonus
    OR OLD.other_additions             IS NOT NEW.other_additions
    OR OLD.other_deductions            IS NOT NEW.other_deductions
    OR OLD.diligence_allowance         IS NOT NEW.diligence_allowance
    OR OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
    OR OLD.sso_employee                IS NOT NEW.sso_employee
    OR OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
    OR OLD.gross                       IS NOT NEW.gross
    OR OLD.net_pay                     IS NOT NEW.net_pay
    OR OLD.note                        IS NOT NEW.note
    OR OLD.other_additions_note        IS NOT NEW.other_additions_note
    OR OLD.other_deductions_note       IS NOT NEW.other_deductions_note
    OR OLD.diligence_forfeit_reason    IS NOT NEW.diligence_forfeit_reason
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_items', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'bonus'                       AS field, OLD.bonus                       AS old_v, NEW.bonus                       AS new_v WHERE OLD.bonus                       IS NOT NEW.bonus
        UNION ALL SELECT 'other_additions',             OLD.other_additions,             NEW.other_additions             WHERE OLD.other_additions             IS NOT NEW.other_additions
        UNION ALL SELECT 'other_deductions',            OLD.other_deductions,            NEW.other_deductions            WHERE OLD.other_deductions            IS NOT NEW.other_deductions
        UNION ALL SELECT 'diligence_allowance',         OLD.diligence_allowance,         NEW.diligence_allowance         WHERE OLD.diligence_allowance         IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'diligence_forfeited',         OLD.diligence_forfeited,         NEW.diligence_forfeited         WHERE OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
        UNION ALL SELECT 'sso_employee',                OLD.sso_employee,                NEW.sso_employee                WHERE OLD.sso_employee                IS NOT NEW.sso_employee
        UNION ALL SELECT 'salary_advance_deduction',    OLD.salary_advance_deduction,    NEW.salary_advance_deduction    WHERE OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
        UNION ALL SELECT 'gross',                       OLD.gross,                       NEW.gross                       WHERE OLD.gross                       IS NOT NEW.gross
        UNION ALL SELECT 'net_pay',                     OLD.net_pay,                     NEW.net_pay                     WHERE OLD.net_pay                     IS NOT NEW.net_pay
        UNION ALL SELECT 'note',                        OLD.note,                        NEW.note                        WHERE OLD.note                        IS NOT NEW.note
        UNION ALL SELECT 'other_additions_note',        OLD.other_additions_note,        NEW.other_additions_note        WHERE OLD.other_additions_note        IS NOT NEW.other_additions_note
        UNION ALL SELECT 'other_deductions_note',       OLD.other_deductions_note,       NEW.other_deductions_note       WHERE OLD.other_deductions_note       IS NOT NEW.other_deductions_note
        UNION ALL SELECT 'diligence_forfeit_reason',    OLD.diligence_forfeit_reason,    NEW.diligence_forfeit_reason    WHERE OLD.diligence_forfeit_reason    IS NOT NEW.diligence_forfeit_reason
    );
END;

CREATE TRIGGER audit_payroll_runs_delete
BEFORE DELETE ON payroll_runs
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_runs', OLD.id, 'DELETE',
        json_object('year_month', OLD.year_month, 'company_id', OLD.company_id, 'status', OLD.status)
    );
END;

CREATE TRIGGER audit_payroll_runs_insert
AFTER INSERT ON payroll_runs
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_runs', NEW.id, 'INSERT',
        json_object(
            'year_month', NEW.year_month,
            'company_id', NEW.company_id,
            'status',     NEW.status,
            'run_date',   NEW.run_date,
            'created_by', NEW.created_by
        )
    );
END;

CREATE TRIGGER audit_payroll_runs_update
AFTER UPDATE ON payroll_runs
WHEN (OLD.status IS NOT NEW.status OR OLD.finalized_at IS NOT NEW.finalized_at)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_runs', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'status'       AS field, OLD.status       AS old_v, NEW.status       AS new_v WHERE OLD.status       IS NOT NEW.status
        UNION ALL SELECT 'finalized_at', OLD.finalized_at, NEW.finalized_at WHERE OLD.finalized_at IS NOT NEW.finalized_at
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

CREATE TRIGGER audit_product_families_delete
BEFORE DELETE ON product_families
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_families', OLD.id, 'DELETE',
        json_object(
            'family_code',  OLD.family_code,
            'display_name', OLD.display_name,
            'brand_id',     OLD.brand_id
        ));
END;

CREATE TRIGGER audit_product_families_insert
AFTER INSERT ON product_families
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_families', NEW.id, 'INSERT',
        json_object(
            'family_code',  NEW.family_code,
            'display_name', NEW.display_name,
            'brand_id',     NEW.brand_id,
            'sort_order',   NEW.sort_order,
            'note',         NEW.note
        ));
END;

CREATE TRIGGER audit_product_families_update
AFTER UPDATE ON product_families
WHEN (
       OLD.family_code  IS NOT NEW.family_code
    OR OLD.display_name IS NOT NEW.display_name
    OR OLD.brand_id     IS NOT NEW.brand_id
    OR OLD.sort_order   IS NOT NEW.sort_order
    OR OLD.note         IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'product_families', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'family_code'  AS field, OLD.family_code  AS old_v, NEW.family_code  AS new_v WHERE OLD.family_code  IS NOT NEW.family_code
        UNION ALL SELECT 'display_name',          OLD.display_name,          NEW.display_name          WHERE OLD.display_name IS NOT NEW.display_name
        UNION ALL SELECT 'brand_id',              OLD.brand_id,              NEW.brand_id              WHERE OLD.brand_id     IS NOT NEW.brand_id
        UNION ALL SELECT 'sort_order',            OLD.sort_order,            NEW.sort_order            WHERE OLD.sort_order   IS NOT NEW.sort_order
        UNION ALL SELECT 'note',                  OLD.note,                  NEW.note                  WHERE OLD.note         IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_product_images_delete
BEFORE DELETE ON product_images
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_images', OLD.id, 'DELETE',
        json_object(
            'family_id',  OLD.family_id,
            'sku_id',     OLD.sku_id,
            'image_path', OLD.image_path
        ));
END;

CREATE TRIGGER audit_product_images_insert
AFTER INSERT ON product_images
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_images', NEW.id, 'INSERT',
        json_object(
            'family_id',        NEW.family_id,
            'sku_id',           NEW.sku_id,
            'image_path',       NEW.image_path,
            'presentation_tag', NEW.presentation_tag,
            'sort_order',       NEW.sort_order
        ));
END;

CREATE TRIGGER audit_product_images_update
AFTER UPDATE ON product_images
WHEN (
       OLD.family_id        IS NOT NEW.family_id
    OR OLD.sku_id           IS NOT NEW.sku_id
    OR OLD.image_path       IS NOT NEW.image_path
    OR OLD.presentation_tag IS NOT NEW.presentation_tag
    OR OLD.sort_order       IS NOT NEW.sort_order
    OR OLD.note             IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'product_images', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'family_id'        AS field, OLD.family_id        AS old_v, NEW.family_id        AS new_v WHERE OLD.family_id        IS NOT NEW.family_id
        UNION ALL SELECT 'sku_id',                    OLD.sku_id,                    NEW.sku_id                    WHERE OLD.sku_id           IS NOT NEW.sku_id
        UNION ALL SELECT 'image_path',                OLD.image_path,                NEW.image_path                WHERE OLD.image_path       IS NOT NEW.image_path
        UNION ALL SELECT 'presentation_tag',          OLD.presentation_tag,          NEW.presentation_tag          WHERE OLD.presentation_tag IS NOT NEW.presentation_tag
        UNION ALL SELECT 'sort_order',                OLD.sort_order,                NEW.sort_order                WHERE OLD.sort_order       IS NOT NEW.sort_order
        UNION ALL SELECT 'note',                      OLD.note,                      NEW.note                      WHERE OLD.note             IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_product_price_tiers_delete
BEFORE DELETE ON product_price_tiers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_price_tiers', OLD.id, 'DELETE',
        json_object(
            'product_id', OLD.product_id,
            'qty_label',  OLD.qty_label,
            'price',      OLD.price
        ));
END;

CREATE TRIGGER audit_product_price_tiers_insert
AFTER INSERT ON product_price_tiers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_price_tiers', NEW.id, 'INSERT',
        json_object(
            'product_id', NEW.product_id,
            'qty_label',  NEW.qty_label,
            'price',      NEW.price,
            'sort_order', NEW.sort_order,
            'note',       NEW.note
        ));
END;

CREATE TRIGGER audit_product_price_tiers_update
AFTER UPDATE ON product_price_tiers
WHEN (
       OLD.product_id IS NOT NEW.product_id
    OR OLD.qty_label  IS NOT NEW.qty_label
    OR OLD.price      IS NOT NEW.price
    OR OLD.sort_order IS NOT NEW.sort_order
    OR OLD.note       IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'product_price_tiers', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'product_id' AS field, OLD.product_id AS old_v, NEW.product_id AS new_v WHERE OLD.product_id IS NOT NEW.product_id
        UNION ALL SELECT 'qty_label',           OLD.qty_label,           NEW.qty_label           WHERE OLD.qty_label  IS NOT NEW.qty_label
        UNION ALL SELECT 'price',               OLD.price,               NEW.price               WHERE OLD.price      IS NOT NEW.price
        UNION ALL SELECT 'sort_order',          OLD.sort_order,          NEW.sort_order          WHERE OLD.sort_order IS NOT NEW.sort_order
        UNION ALL SELECT 'note',                OLD.note,                NEW.note                WHERE OLD.note       IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_products_delete
BEFORE DELETE ON products
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'products', OLD.id, 'DELETE',
        json_object(
            'product_name', OLD.product_name,
            'unit_type', OLD.unit_type,
            'cost_price', OLD.cost_price,
            'base_sell_price', OLD.base_sell_price,
            'is_active', OLD.is_active
        )
    );
END;

CREATE TRIGGER audit_products_insert
AFTER INSERT ON products
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'products', NEW.id, 'INSERT',
        json_object(
            'product_name', NEW.product_name,
            'unit_type', NEW.unit_type,
            'cost_price', NEW.cost_price,
            'base_sell_price', NEW.base_sell_price,
            'low_stock_threshold', NEW.low_stock_threshold,
            'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_products_update
AFTER UPDATE ON products
WHEN (
       OLD.product_name        IS NOT NEW.product_name
    OR OLD.unit_type           IS NOT NEW.unit_type
    OR OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.units_per_carton    IS NOT NEW.units_per_carton
    OR OLD.units_per_box       IS NOT NEW.units_per_box
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    OR OLD.hard_to_sell        IS NOT NEW.hard_to_sell
    OR OLD.is_active           IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'products', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'product_name'        AS field, OLD.product_name        AS old_v, NEW.product_name        AS new_v WHERE OLD.product_name        IS NOT NEW.product_name
        UNION ALL SELECT 'unit_type',           OLD.unit_type,           NEW.unit_type           WHERE OLD.unit_type           IS NOT NEW.unit_type
        UNION ALL SELECT 'cost_price',          OLD.cost_price,          NEW.cost_price          WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',     OLD.base_sell_price,     NEW.base_sell_price     WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'units_per_carton',    OLD.units_per_carton,    NEW.units_per_carton    WHERE OLD.units_per_carton    IS NOT NEW.units_per_carton
        UNION ALL SELECT 'units_per_box',       OLD.units_per_box,       NEW.units_per_box       WHERE OLD.units_per_box       IS NOT NEW.units_per_box
        UNION ALL SELECT 'low_stock_threshold', OLD.low_stock_threshold, NEW.low_stock_threshold WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
        UNION ALL SELECT 'hard_to_sell',        OLD.hard_to_sell,        NEW.hard_to_sell        WHERE OLD.hard_to_sell        IS NOT NEW.hard_to_sell
        UNION ALL SELECT 'is_active',           OLD.is_active,           NEW.is_active           WHERE OLD.is_active           IS NOT NEW.is_active
    );
END;

CREATE TRIGGER audit_promotions_delete
BEFORE DELETE ON promotions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'promotions', OLD.id, 'DELETE',
        json_object(
            'product_id',        OLD.product_id,
            'promo_name',        OLD.promo_name,
            'promo_type',        OLD.promo_type,
            'discount_value',    OLD.discount_value,
            'bundle_buy',        OLD.bundle_buy,
            'bundle_free',       OLD.bundle_free,
            'bundle_unit',       OLD.bundle_unit,
            'bundle_condition',  OLD.bundle_condition,
            'bundle_tiers_json', OLD.bundle_tiers_json,
            'gift_desc',         OLD.gift_desc,
            'gift_qty',          OLD.gift_qty,
            'is_active',         OLD.is_active
        )
    );
END;

CREATE TRIGGER audit_promotions_insert
AFTER INSERT ON promotions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'promotions', NEW.id, 'INSERT',
        json_object(
            'product_id',        NEW.product_id,
            'promo_name',        NEW.promo_name,
            'promo_type',        NEW.promo_type,
            'discount_value',    NEW.discount_value,
            'bundle_buy',        NEW.bundle_buy,
            'bundle_free',       NEW.bundle_free,
            'bundle_unit',       NEW.bundle_unit,
            'bundle_condition',  NEW.bundle_condition,
            'bundle_tiers_json', NEW.bundle_tiers_json,
            'gift_desc',         NEW.gift_desc,
            'gift_qty',          NEW.gift_qty,
            'date_start',        NEW.date_start,
            'date_end',          NEW.date_end,
            'is_active',         NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_promotions_update
AFTER UPDATE ON promotions
WHEN (
       OLD.product_id        IS NOT NEW.product_id
    OR OLD.promo_name        IS NOT NEW.promo_name
    OR OLD.promo_type        IS NOT NEW.promo_type
    OR OLD.discount_value    IS NOT NEW.discount_value
    OR OLD.bundle_buy        IS NOT NEW.bundle_buy
    OR OLD.bundle_free       IS NOT NEW.bundle_free
    OR OLD.bundle_unit       IS NOT NEW.bundle_unit
    OR OLD.bundle_condition  IS NOT NEW.bundle_condition
    OR OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json
    OR OLD.gift_desc         IS NOT NEW.gift_desc
    OR OLD.gift_qty          IS NOT NEW.gift_qty
    OR OLD.date_start        IS NOT NEW.date_start
    OR OLD.date_end          IS NOT NEW.date_end
    OR OLD.is_active         IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'promotions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'product_id'        AS field, OLD.product_id        AS old_v, NEW.product_id        AS new_v WHERE OLD.product_id        IS NOT NEW.product_id
        UNION ALL SELECT 'promo_name',                OLD.promo_name,                NEW.promo_name                WHERE OLD.promo_name        IS NOT NEW.promo_name
        UNION ALL SELECT 'promo_type',                OLD.promo_type,                NEW.promo_type                WHERE OLD.promo_type        IS NOT NEW.promo_type
        UNION ALL SELECT 'discount_value',            OLD.discount_value,            NEW.discount_value            WHERE OLD.discount_value    IS NOT NEW.discount_value
        UNION ALL SELECT 'bundle_buy',                OLD.bundle_buy,                NEW.bundle_buy                WHERE OLD.bundle_buy        IS NOT NEW.bundle_buy
        UNION ALL SELECT 'bundle_free',               OLD.bundle_free,               NEW.bundle_free               WHERE OLD.bundle_free       IS NOT NEW.bundle_free
        UNION ALL SELECT 'bundle_unit',               OLD.bundle_unit,               NEW.bundle_unit               WHERE OLD.bundle_unit       IS NOT NEW.bundle_unit
        UNION ALL SELECT 'bundle_condition',          OLD.bundle_condition,          NEW.bundle_condition          WHERE OLD.bundle_condition  IS NOT NEW.bundle_condition
        UNION ALL SELECT 'bundle_tiers_json',         OLD.bundle_tiers_json,         NEW.bundle_tiers_json         WHERE OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json
        UNION ALL SELECT 'gift_desc',                 OLD.gift_desc,                 NEW.gift_desc                 WHERE OLD.gift_desc         IS NOT NEW.gift_desc
        UNION ALL SELECT 'gift_qty',                  OLD.gift_qty,                  NEW.gift_qty                  WHERE OLD.gift_qty          IS NOT NEW.gift_qty
        UNION ALL SELECT 'date_start',                OLD.date_start,                NEW.date_start                WHERE OLD.date_start        IS NOT NEW.date_start
        UNION ALL SELECT 'date_end',                  OLD.date_end,                  NEW.date_end                  WHERE OLD.date_end          IS NOT NEW.date_end
        UNION ALL SELECT 'is_active',                 OLD.is_active,                 NEW.is_active                 WHERE OLD.is_active         IS NOT NEW.is_active
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

CREATE TRIGGER audit_regions_delete
BEFORE DELETE ON regions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'regions', OLD.id, 'DELETE',
        json_object('code', OLD.code, 'name_th', OLD.name_th, 'parent_id', OLD.parent_id)
    );
END;

CREATE TRIGGER audit_regions_insert
AFTER INSERT ON regions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'regions', NEW.id, 'INSERT',
        json_object(
            'code', NEW.code, 'name_th', NEW.name_th,
            'parent_id', NEW.parent_id, 'sort_order', NEW.sort_order
        )
    );
END;

CREATE TRIGGER audit_regions_update
AFTER UPDATE ON regions
WHEN (
       OLD.code       IS NOT NEW.code
    OR OLD.name_th    IS NOT NEW.name_th
    OR OLD.parent_id  IS NOT NEW.parent_id
    OR OLD.sort_order IS NOT NEW.sort_order
    OR OLD.note       IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'regions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'       AS field, OLD.code       AS old_v, NEW.code       AS new_v WHERE OLD.code       IS NOT NEW.code
        UNION ALL SELECT 'name_th',             OLD.name_th,            NEW.name_th             WHERE OLD.name_th    IS NOT NEW.name_th
        UNION ALL SELECT 'parent_id',           OLD.parent_id,          NEW.parent_id           WHERE OLD.parent_id  IS NOT NEW.parent_id
        UNION ALL SELECT 'sort_order',          OLD.sort_order,         NEW.sort_order          WHERE OLD.sort_order IS NOT NEW.sort_order
        UNION ALL SELECT 'note',                OLD.note,               NEW.note                WHERE OLD.note       IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_salary_advances_delete
BEFORE DELETE ON salary_advances
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salary_advances', OLD.id, 'DELETE',
        json_object('employee_id', OLD.employee_id, 'advance_date', OLD.advance_date, 'amount', OLD.amount)
    );
END;

CREATE TRIGGER audit_salary_advances_insert
AFTER INSERT ON salary_advances
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salary_advances', NEW.id, 'INSERT',
        json_object(
            'employee_id',     NEW.employee_id,
            'advance_date',    NEW.advance_date,
            'amount',          NEW.amount,
            'raw_name',        NEW.raw_name,
            'note',            NEW.note,
            'source_file',     NEW.source_file,
            'import_batch_id', NEW.import_batch_id
        )
    );
END;

CREATE TRIGGER audit_salary_advances_update
AFTER UPDATE ON salary_advances
WHEN (
       OLD.amount             IS NOT NEW.amount
    OR OLD.advance_date       IS NOT NEW.advance_date
    OR OLD.deducted_in_run_id IS NOT NEW.deducted_in_run_id
    OR OLD.note               IS NOT NEW.note
    OR OLD.employee_id        IS NOT NEW.employee_id
    OR OLD.raw_name           IS NOT NEW.raw_name
    OR OLD.import_batch_id    IS NOT NEW.import_batch_id
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'salary_advances', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'amount'             AS field, OLD.amount             AS old_v, NEW.amount             AS new_v WHERE OLD.amount             IS NOT NEW.amount
        UNION ALL SELECT 'advance_date',       OLD.advance_date,       NEW.advance_date       WHERE OLD.advance_date       IS NOT NEW.advance_date
        UNION ALL SELECT 'deducted_in_run_id', OLD.deducted_in_run_id, NEW.deducted_in_run_id WHERE OLD.deducted_in_run_id IS NOT NEW.deducted_in_run_id
        UNION ALL SELECT 'note',               OLD.note,               NEW.note               WHERE OLD.note               IS NOT NEW.note
        UNION ALL SELECT 'employee_id',        OLD.employee_id,        NEW.employee_id        WHERE OLD.employee_id        IS NOT NEW.employee_id
        UNION ALL SELECT 'raw_name',           OLD.raw_name,           NEW.raw_name           WHERE OLD.raw_name           IS NOT NEW.raw_name
        UNION ALL SELECT 'import_batch_id',    OLD.import_batch_id,    NEW.import_batch_id    WHERE OLD.import_batch_id    IS NOT NEW.import_batch_id
    );
END;

CREATE TRIGGER audit_salespersons_delete
BEFORE DELETE ON salespersons
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salespersons', OLD.rowid, 'DELETE',
        json_object('code', OLD.code, 'name', OLD.name)
    );
END;

CREATE TRIGGER audit_salespersons_insert
AFTER INSERT ON salespersons
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salespersons', NEW.rowid, 'INSERT',
        json_object('code', NEW.code, 'name', NEW.name, 'is_active', NEW.is_active)
    );
END;

CREATE TRIGGER audit_salespersons_update
AFTER UPDATE ON salespersons
WHEN (
       OLD.code      IS NOT NEW.code
    OR OLD.name      IS NOT NEW.name
    OR OLD.is_active IS NOT NEW.is_active
    OR OLD.note      IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'salespersons', NEW.rowid, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'      AS field, OLD.code      AS old_v, NEW.code      AS new_v WHERE OLD.code      IS NOT NEW.code
        UNION ALL SELECT 'name',               OLD.name,               NEW.name               WHERE OLD.name      IS NOT NEW.name
        UNION ALL SELECT 'is_active',          OLD.is_active,          NEW.is_active          WHERE OLD.is_active IS NOT NEW.is_active
        UNION ALL SELECT 'note',               OLD.note,               NEW.note               WHERE OLD.note      IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_suppliers_delete
BEFORE DELETE ON suppliers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'suppliers', OLD.id, 'DELETE',
        json_object(
            'name', OLD.name, 'display_name', OLD.display_name,
            'contact_info', OLD.contact_info
        )
    );
END;

CREATE TRIGGER audit_suppliers_insert
AFTER INSERT ON suppliers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'suppliers', NEW.id, 'INSERT',
        json_object(
            'name', NEW.name, 'display_name', NEW.display_name,
            'contact_info', NEW.contact_info, 'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_suppliers_update
AFTER UPDATE ON suppliers
WHEN (
       OLD.name         IS NOT NEW.name
    OR OLD.display_name IS NOT NEW.display_name
    OR OLD.contact_info IS NOT NEW.contact_info
    OR OLD.note         IS NOT NEW.note
    OR OLD.is_active    IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'suppliers', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'name'         AS field, OLD.name         AS old_v, NEW.name         AS new_v WHERE OLD.name         IS NOT NEW.name
        UNION ALL SELECT 'display_name',          OLD.display_name,         NEW.display_name         WHERE OLD.display_name IS NOT NEW.display_name
        UNION ALL SELECT 'contact_info',          OLD.contact_info,         NEW.contact_info         WHERE OLD.contact_info IS NOT NEW.contact_info
        UNION ALL SELECT 'note',                  OLD.note,                 NEW.note                 WHERE OLD.note         IS NOT NEW.note
        UNION ALL SELECT 'is_active',             OLD.is_active,            NEW.is_active            WHERE OLD.is_active    IS NOT NEW.is_active
    );
END;

CREATE TRIGGER audit_transactions_delete
BEFORE DELETE ON transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'transactions', OLD.id, 'DELETE',
        json_object(
            'product_id',      OLD.product_id,
            'txn_type',        OLD.txn_type,
            'quantity_change', OLD.quantity_change,
            'unit_mode',       OLD.unit_mode,
            'reference_no',    OLD.reference_no,
            'note',            OLD.note,
            'created_at',      OLD.created_at
        )
    );
END;

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

CREATE TRIGGER audit_transactions_update
AFTER UPDATE ON transactions
WHEN (
       OLD.product_id      IS NOT NEW.product_id
    OR OLD.txn_type        IS NOT NEW.txn_type
    OR OLD.quantity_change IS NOT NEW.quantity_change
    OR OLD.unit_mode       IS NOT NEW.unit_mode
    OR OLD.reference_no    IS NOT NEW.reference_no
    OR OLD.note            IS NOT NEW.note
    OR OLD.created_at      IS NOT NEW.created_at
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'transactions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'product_id'      AS field, OLD.product_id      AS old_v, NEW.product_id      AS new_v WHERE OLD.product_id      IS NOT NEW.product_id
        UNION ALL SELECT 'txn_type',        OLD.txn_type,        NEW.txn_type        WHERE OLD.txn_type        IS NOT NEW.txn_type
        UNION ALL SELECT 'quantity_change', OLD.quantity_change, NEW.quantity_change WHERE OLD.quantity_change IS NOT NEW.quantity_change
        UNION ALL SELECT 'unit_mode',       OLD.unit_mode,       NEW.unit_mode       WHERE OLD.unit_mode       IS NOT NEW.unit_mode
        UNION ALL SELECT 'reference_no',    OLD.reference_no,    NEW.reference_no    WHERE OLD.reference_no    IS NOT NEW.reference_no
        UNION ALL SELECT 'note',            OLD.note,            NEW.note            WHERE OLD.note            IS NOT NEW.note
        UNION ALL SELECT 'created_at',      OLD.created_at,      NEW.created_at      WHERE OLD.created_at      IS NOT NEW.created_at
    );
END;

CREATE TRIGGER product_families_display_format_check_insert
    BEFORE INSERT ON product_families
    WHEN NEW.display_format IS NOT NULL
         AND NEW.display_format NOT IN
             ('single', 'pack_variants', 'size_table', 'color_swatch', 'matrix')
    BEGIN
        SELECT RAISE(ABORT,
            'display_format must be NULL or one of: single, pack_variants, size_table, color_swatch, matrix');
    END;

CREATE TRIGGER product_families_display_format_check_update
    BEFORE UPDATE ON product_families
    WHEN NEW.display_format IS NOT NULL
         AND NEW.display_format NOT IN
             ('single', 'pack_variants', 'size_table', 'color_swatch', 'matrix')
    BEGIN
        SELECT RAISE(ABORT,
            'display_format must be NULL or one of: single, pack_variants, size_table, color_swatch, matrix');
    END;

CREATE TRIGGER product_price_history_update
AFTER UPDATE ON products
WHEN (
       OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
)
BEGIN
    INSERT INTO product_price_history (product_id, field_name, old_value, new_value)
    SELECT NEW.id, field, old_v, new_v
    FROM (
                  SELECT 'cost_price'          AS field, OLD.cost_price          AS old_v, NEW.cost_price          AS new_v WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',             OLD.base_sell_price,             NEW.base_sell_price             WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'low_stock_threshold',         OLD.low_stock_threshold,         NEW.low_stock_threshold         WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    );
END;

CREATE TRIGGER products_packaging_short_check_insert
    BEFORE INSERT ON products
    WHEN NEW.packaging_short IS NOT NULL
         AND NEW.packaging_short NOT IN (
             'UN', 'PN', 'BG', 'SC', 'PK', 'DZ', 'HP', 'PP', 'TB', 'SP', 'C60'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_short must be NULL or one of: UN, PN, BG, SC, PK, DZ, HP, PP, TB, SP, C60');
    END;

CREATE TRIGGER products_packaging_short_check_update
    BEFORE UPDATE ON products
    WHEN NEW.packaging_short IS NOT NULL
         AND NEW.packaging_short NOT IN (
             'UN', 'PN', 'BG', 'SC', 'PK', 'DZ', 'HP', 'PP', 'TB', 'SP', 'C60'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_short must be NULL or one of: UN, PN, BG, SC, PK, DZ, HP, PP, TB, SP, C60');
    END;

CREATE TRIGGER products_packaging_th_check_insert
    BEFORE INSERT ON products
    WHEN NEW.packaging_th IS NOT NULL
         AND NEW.packaging_th NOT IN (
             'แผง', 'ตัว', 'ถุง', 'แพ็คหัว', 'แพ็คถุง',
             'ซอง', 'อัดแผง', 'แพ็ค', 'แบบหลอด', 'โหล', '1กลมี60ใบ'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_th must be NULL or one of: แผง, ตัว, ถุง, แพ็คหัว, แพ็คถุง, ซอง, อัดแผง, แพ็ค, แบบหลอด, โหล, 1กลมี60ใบ');
    END;

CREATE TRIGGER products_packaging_th_check_update
    BEFORE UPDATE ON products
    WHEN NEW.packaging_th IS NOT NULL
         AND NEW.packaging_th NOT IN (
             'แผง', 'ตัว', 'ถุง', 'แพ็คหัว', 'แพ็คถุง',
             'ซอง', 'อัดแผง', 'แพ็ค', 'แบบหลอด', 'โหล', '1กลมี60ใบ'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_th must be NULL or one of: แผง, ตัว, ถุง, แพ็คหัว, แพ็คถุง, ซอง, อัดแผง, แพ็ค, แบบหลอด, โหล, 1กลมี60ใบ');
    END;

CREATE TRIGGER update_color_finish_codes_timestamp
    AFTER UPDATE ON color_finish_codes
    BEGIN
        UPDATE color_finish_codes SET updated_at = datetime('now','localtime')
        WHERE code = NEW.code;
    END;

CREATE TRIGGER update_product_families_timestamp
    AFTER UPDATE ON product_families
    BEGIN
        UPDATE product_families SET updated_at = datetime('now','localtime')
        WHERE id = NEW.id;
    END;

CREATE TRIGGER update_product_images_timestamp
    AFTER UPDATE ON product_images
    BEGIN
        UPDATE product_images SET updated_at = datetime('now','localtime')
        WHERE id = NEW.id;
    END;

CREATE TRIGGER update_product_price_tiers_timestamp
    AFTER UPDATE ON product_price_tiers
    BEGIN
        UPDATE product_price_tiers SET updated_at = datetime('now','localtime')
        WHERE id = NEW.id;
    END;

CREATE TRIGGER update_product_timestamp
    AFTER UPDATE ON products
    BEGIN
        UPDATE products SET updated_at = datetime('now','localtime') WHERE id = NEW.id;
    END;

CREATE VIEW products_full AS
SELECT
    p.id, p.product_name,
    c.name_th        AS category,
    p.series,
    b.name           AS brand,
    b.short_code     AS brand_short_code,
    b.is_own_brand   AS is_own_brand,
    p.model, p.size,
    cf.name_th       AS color_th,
    p.color_code, p.packaging_th, p.packaging_short, p.condition, p.pack_variant,
    p.family_id, p.unit_type, p.units_per_carton, p.units_per_box,
    p.cost_price, p.base_sell_price, p.hard_to_sell, p.is_active,
    COALESCE(s.quantity, 0) AS stock,
    p.shopee_stock, p.lazada_stock,
    p.created_at, p.updated_at
FROM products p
LEFT JOIN brands b              ON b.id   = p.brand_id
LEFT JOIN categories c          ON c.id   = p.category_id
LEFT JOIN color_finish_codes cf ON cf.code = p.color_code
LEFT JOIN stock_levels s        ON s.product_id = p.id;

COMMIT;
PRAGMA foreign_keys = ON;
