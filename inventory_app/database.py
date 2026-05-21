"""Sendy ERP — schema bootstrap + migration runner.

Owns:
- `SCHEMA` constant: the base SQL for a fresh DB (used only on first boot
  if no tables exist; otherwise the migration runner takes over)
- `get_connection()`: returns a sqlite3.Connection with `row_factory=Row`,
  WAL journal mode, and `foreign_keys = ON`
- `init_db()`: idempotent bootstrap — runs `SCHEMA` against empty DBs,
  then applies any pending migrations from `data/migrations/NNN_*.sql`
- `_apply_pending_migrations()`: the migration runner — filename-keyed,
  uses `INSERT OR IGNORE` into `applied_migrations` (legacy migs 025-052
  self-insert; the runner's bookkeeping is idempotent either way)

Migration runner contract:
  - Files MUST be named `NNN_descriptive_name.sql` + `.rollback.sql`
  - Forward script wraps work in `BEGIN; ... COMMIT;`
  - For table-rebuilds with FK references, use `PRAGMA foreign_keys=OFF`
    BEFORE `BEGIN;` (no-op inside a transaction) — see mig 069 for the
    working pattern
  - Filename is the immutable key; runner does NOT re-check sha256, so
    in-place edits to an already-applied migration are technically
    possible BUT silently break prod/dev parity. Use a new higher-NNN
    migration to fix bugs (see sendy_erp/CLAUDE.md for the rare-exception
    rules)
  - On failure: the script's own BEGIN/COMMIT is rolled back; boot fails
    loudly (safe default)
"""
import sqlite3
import os
import time
import hashlib
import glob
from config import DATABASE_PATH
from werkzeug.security import generate_password_hash

MIGRATIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'data', 'migrations')
)

SCHEMA = """
PRAGMA encoding = 'UTF-8';
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sku                 INTEGER UNIQUE NOT NULL,
    product_name        TEXT    NOT NULL,
    units_per_carton    INTEGER,
    units_per_box       INTEGER,
    unit_type           TEXT    NOT NULL DEFAULT 'ตัว',
    hard_to_sell        INTEGER NOT NULL DEFAULT 0,
    cost_price          REAL    NOT NULL DEFAULT 0.0,
    base_sell_price     REAL    NOT NULL DEFAULT 0.0,
    low_stock_threshold INTEGER NOT NULL DEFAULT 10,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS stock_levels (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id),
    quantity    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    txn_type        TEXT    NOT NULL CHECK(txn_type IN ('IN','OUT','ADJUST')),
    quantity_change INTEGER NOT NULL,
    unit_mode       TEXT    NOT NULL CHECK(unit_mode IN ('unit','box','carton')),
    reference_no    TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS promotions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    promo_name      TEXT    NOT NULL,
    promo_type      TEXT    NOT NULL CHECK(promo_type IN ('percent','fixed')),
    discount_value  REAL    NOT NULL,
    date_start      TEXT,
    date_end        TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS import_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT    NOT NULL,
    rows_imported   INTEGER NOT NULL,
    rows_skipped    INTEGER NOT NULL,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    notes           TEXT
);

-- BSN system product code → internal product mapping
CREATE TABLE IF NOT EXISTS product_code_mapping (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bsn_code    TEXT UNIQUE NOT NULL,
    bsn_name    TEXT NOT NULL,
    product_id  INTEGER REFERENCES products(id),
    is_ignored  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Sales transactions (from ขาย files)
CREATE TABLE IF NOT EXISTS sales_transactions (
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
    ref_invoice         TEXT,                                 -- only set on SR rows: original IV being credited
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Purchase transactions (from ซื้อ files)
CREATE TABLE IF NOT EXISTS purchase_transactions (
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
);

CREATE TABLE IF NOT EXISTS unit_conversions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    bsn_unit    TEXT    NOT NULL,
    ratio       REAL    NOT NULL DEFAULT 1.0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(product_id, bsn_unit)
);

CREATE TABLE IF NOT EXISTS product_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    floor_no    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Audit log for tracking row-level changes (rollout per-table via triggers)
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT    NOT NULL,
    row_id          INTEGER NOT NULL,
    action          TEXT    NOT NULL CHECK(action IN ('INSERT','UPDATE','DELETE')),
    changed_fields  TEXT,
    user            TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_audit_table_row ON audit_log(table_name, row_id);

-- Tracks which numbered SQL migrations from data/migrations/ have been applied.
-- run_pending_migrations() reads this table on every boot.
CREATE TABLE IF NOT EXISTS applied_migrations (
    filename     TEXT    PRIMARY KEY,
    applied_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    applied_by   TEXT,
    sha256       TEXT,
    duration_ms  INTEGER
);

CREATE TABLE IF NOT EXISTS received_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    re_no        TEXT    NOT NULL UNIQUE,
    date_iso     TEXT    NOT NULL,
    customer     TEXT    NOT NULL,
    salesperson  TEXT,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    imported_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- iv_no holds BOTH 'IV…' settlement refs AND 'SR…' credit-note netting refs
-- (the latter with negative amount). payments_alloc.py:216-279 re-attributes
-- SR rows to the original invoice; do NOT delete or filter SR rows in audits.
CREATE TABLE IF NOT EXISTS paid_invoices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    re_id      INTEGER NOT NULL REFERENCES received_payments(id),
    iv_no      TEXT    NOT NULL,
    UNIQUE(re_id, iv_no)
);

-- E-commerce platform SKUs (Shopee / Lazada)
-- Users & roles
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    display_name  TEXT,
    role          TEXT    NOT NULL DEFAULT 'staff'
                          CHECK(role IN ('admin','manager','staff')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS platform_skus (
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
    imported_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, variation_id)
);

-- Product conversion formulas (สูตรแปลงสินค้า)
CREATE TABLE IF NOT EXISTS conversion_formulas (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    output_product_id INTEGER NOT NULL REFERENCES products(id),
    output_qty        INTEGER NOT NULL DEFAULT 1,
    note              TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS product_barcodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    barcode     TEXT    NOT NULL UNIQUE,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_product_barcodes_product ON product_barcodes(product_id);

CREATE TABLE IF NOT EXISTS conversion_formula_inputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    formula_id  INTEGER NOT NULL REFERENCES conversion_formulas(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL
);

-- Customer master (imported from BSN customer info CSV)
CREATE TABLE IF NOT EXISTS customers (
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
);

CREATE TRIGGER IF NOT EXISTS update_product_timestamp
    AFTER UPDATE ON products
    BEGIN
        UPDATE products SET updated_at = datetime('now','localtime') WHERE id = NEW.id;
    END;

CREATE TRIGGER IF NOT EXISTS after_transaction_insert
    AFTER INSERT ON transactions
    BEGIN
        INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0)
            ON CONFLICT(product_id) DO NOTHING;
        UPDATE stock_levels
           SET quantity = quantity + NEW.quantity_change
         WHERE product_id = NEW.product_id;
    END;
"""


def get_connection():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _list_migration_files():
    """Return numbered .sql files in data/migrations/ sorted by name.
    Excludes .rollback.sql files."""
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    files = []
    for path in glob.glob(os.path.join(MIGRATIONS_DIR, '*.sql')):
        name = os.path.basename(path)
        if name.endswith('.rollback.sql'):
            continue
        files.append(name)
    return sorted(files)


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)
    ).fetchone()
    return row is not None


def run_pending_migrations(conn, verbose=True):
    """Apply any numbered .sql migration in data/migrations/ that isn't
    yet recorded in applied_migrations.

    Bootstrap: on a database that pre-dates this runner (i.e. brands
    table exists from migration 004 but applied_migrations is empty),
    backfill ALL existing migration files as already-applied. This
    avoids re-running migrations that were applied manually before this
    code shipped.

    On a fresh DB (no brands table), every migration runs in order."""
    files = _list_migration_files()
    if not files:
        return []

    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations"
    ).fetchall()}

    # Bootstrap path: existing DB with manual migrations already applied
    if not applied and _table_exists(conn, 'brands'):
        for filename in files:
            path = os.path.join(MIGRATIONS_DIR, filename)
            conn.execute(
                """INSERT OR IGNORE INTO applied_migrations
                       (filename, applied_by, sha256)
                   VALUES (?, ?, ?)""",
                (filename, 'bootstrap-backfill', _file_sha256(path))
            )
        conn.commit()
        if verbose:
            print(f"[migration] bootstrap: backfilled {len(files)} migrations as applied")
        return []

    pending = [f for f in files if f not in applied]
    if not pending:
        return []

    ran = []
    for filename in pending:
        path = os.path.join(MIGRATIONS_DIR, filename)
        with open(path, 'r', encoding='utf-8') as f:
            sql = f.read()
        t0 = time.time()
        try:
            conn.executescript(sql)
        except Exception as e:
            # Migration files wrap their work in BEGIN/COMMIT; on failure
            # SQLite will have rolled back the transaction. Surface the
            # error loudly — boot will fail, which is the safe default.
            print(f"[migration] FAILED {filename}: {e}")
            raise
        duration_ms = int((time.time() - t0) * 1000)
        # INSERT OR IGNORE (matches the bootstrap path): legacy migration
        # files 025–052 self-insert their own applied_migrations row inside
        # the script. On the pending path executescript runs that self-insert,
        # so a plain INSERT here would hit the filename PRIMARY KEY and crash
        # boot. OR IGNORE makes the runner's bookkeeping idempotent whether or
        # not the migration self-recorded.
        conn.execute(
            """INSERT OR IGNORE INTO applied_migrations
                   (filename, applied_by, sha256, duration_ms)
               VALUES (?, 'auto', ?, ?)""",
            (filename, _file_sha256(path), duration_ms)
        )
        conn.commit()
        ran.append(filename)
        if verbose:
            print(f"[migration] applied {filename} in {duration_ms}ms")
    return ran


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
    # Migration: add synced_to_stock column to BSN transaction tables if missing
    for tbl in ('sales_transactions', 'purchase_transactions'):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if 'synced_to_stock' not in cols:
            conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN synced_to_stock INTEGER NOT NULL DEFAULT 0"
            )
    # Migration: add shopee_stock and lazada_stock if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
    if 'shopee_stock' not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN shopee_stock INTEGER NOT NULL DEFAULT 0")
    if 'lazada_stock' not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN lazada_stock INTEGER NOT NULL DEFAULT 0")
    # Migration: add doc_base column + indexes for payment status performance
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sales_transactions)").fetchall()]
    if 'doc_base' not in cols:
        conn.execute("ALTER TABLE sales_transactions ADD COLUMN doc_base TEXT")
        conn.execute("""
            UPDATE sales_transactions
            SET doc_base = SUBSTR(doc_no, 1, INSTR(doc_no || '-', '-') - 1)
        """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_st_doc_base ON sales_transactions(doc_base)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pi_iv_no ON paid_invoices(iv_no)")
    # Migration: ref_invoice column on sales_transactions (only populated on SR/credit-note rows)
    if 'ref_invoice' not in cols:
        conn.execute("ALTER TABLE sales_transactions ADD COLUMN ref_invoice TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_st_ref_invoice ON sales_transactions(ref_invoice)")
    # Migration: create conversion tables if missing
    existing_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'conversion_formulas' not in existing_tables:
        conn.executescript("""
            CREATE TABLE conversion_formulas (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT    NOT NULL,
                output_product_id INTEGER NOT NULL REFERENCES products(id),
                output_qty        INTEGER NOT NULL DEFAULT 1,
                note              TEXT,
                is_active         INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE conversion_formula_inputs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                formula_id  INTEGER NOT NULL REFERENCES conversion_formulas(id) ON DELETE CASCADE,
                product_id  INTEGER NOT NULL REFERENCES products(id),
                quantity    INTEGER NOT NULL
            );
        """)
    # Migration: create customers table if missing
    if 'customers' not in existing_tables:
        conn.executescript("""
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
            );
        """)
    # Migration: create product_cost_ledger and conversion_cost_log if missing
    if 'product_cost_ledger' not in existing_tables:
        conn.executescript("""
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
            CREATE INDEX idx_pcl_product ON product_cost_ledger(product_id, event_date, id);
        """)
    if 'conversion_cost_log' not in existing_tables:
        conn.executescript("""
            CREATE TABLE conversion_cost_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                output_product_id INTEGER NOT NULL REFERENCES products(id),
                reference_no      TEXT,
                event_date        TEXT    NOT NULL,
                output_qty        REAL    NOT NULL,
                total_input_cost  REAL    NOT NULL,
                unit_cost         REAL    NOT NULL,
                created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
        """)
    # Migration: create ecommerce_listings table if missing
    if 'ecommerce_listings' not in existing_tables:
        conn.executescript("""
            CREATE TABLE ecommerce_listings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                platform     TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
                item_name    TEXT    NOT NULL,
                variation    TEXT,
                seller_sku   TEXT,
                listing_key  TEXT    NOT NULL UNIQUE,
                sample_price REAL,
                product_id   INTEGER REFERENCES products(id),
                qty_per_sale REAL    NOT NULL DEFAULT 1,
                is_ignored   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX idx_el_platform ON ecommerce_listings(platform, product_id);
        """)
    else:
        # Migration: add qty_per_sale if table exists but column missing
        el_cols = [r[1] for r in conn.execute("PRAGMA table_info(ecommerce_listings)").fetchall()]
        if 'qty_per_sale' not in el_cols:
            conn.execute("ALTER TABLE ecommerce_listings ADD COLUMN qty_per_sale REAL NOT NULL DEFAULT 1")
    # Migration: create default admin user if users table is empty
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        import config as _cfg
        conn.execute(
            "INSERT INTO users(username, password_hash, display_name, role) VALUES (?,?,?,?)",
            ('admin', generate_password_hash(_cfg.ADMIN_PASSWORD, method='pbkdf2:sha256'), 'Administrator', 'admin')
        )
    conn.commit()
    # Apply any pending numbered migrations from data/migrations/.
    run_pending_migrations(conn)
    conn.close()
