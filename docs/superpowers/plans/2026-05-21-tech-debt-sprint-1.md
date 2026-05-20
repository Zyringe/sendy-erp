# Tech Debt Sprint #1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the engineering-side tech-debt items that are safely fixable in one PR cycle (security defaults, deps pinning, schema NOT-NULL, audit triggers, route-level test coverage, docs drift, hardcoded paths).

**Architecture:** No new architecture. Each task is a focused, reversible change with TDD where applicable. One commit per task; final PR rolls everything up. Execute in worktree `~/Sendai-Boonsawat-wt/tech-debt-sprint-1/`, branch `chore/tech-debt-sprint-1`.

**Tech Stack:** Flask 3.x, Python 3.9, SQLite, pytest, raw SQL migrations (numbered `data/migrations/NNN_*.sql` + rollback pair).

**Out of scope (deferred with reason):**
- WACC recalc + `cost_price` backfill — needs DB backup ceremony + neg-stock unblock + Put's go-ahead
- มือจับ `ratio=1` / 87 ผง / 109 ชด / 98 ชุด / 65 ใบ borderline — per-product judgment by Put
- "ไม่ระบุแบรนด์" 30% revenue bucket — data mapping work, Put-driven
- pid 1558 ลูกยิง MAX F30 `กิโลกรัม=1` audit — needs Put confirmation on direction
- 994 EAN barcode mapping — paused by design (Put's call)
- 519 `_pending` + 831 `_unmatched` product photos — Put owns review backlog
- 2+2+2 unmapped sales/purchase/VAT SKUs — Put's manual mapping queue
- 19 NULL-ref SR + 297 orphan receipt-links — bookkeeping cleanup, no money at stake
- `app.py` (3,585 LOC) / `models.py` (4,421 LOC) monolith refactor — opportunistic, multi-session
- Remaining blueprint extraction (bp_inventory, bp_bsn, bp_sales, bp_payments, bp_ecommerce, bp_admin) — opportunistic, one per natural touchpoint
- `commission.py` cache-invalidation cleanup — fold into next commission bug fix
- Brand-voice setup completion — paused, needs Put FB/LINE inputs
- Backup launchd re-enable — paused until ~2026-05-27 (FDA grant)

**Already resolved (not in plan):**
- HR nickname-override guard → PR #42 (commit `6b024e6`)
- Stale `origin/*` branches → already pruned (only `origin/main` remains)

---

## File Map

**Files to create:**
- `data/migrations/069_products_units_not_null.sql` + rollback
- `data/migrations/070_audit_log_triggers.sql` + rollback
- `tests/test_bp_products_routes.py`
- `tests/test_post_whitelist.py`
- `tests/test_config_secrets.py`
- `tests/test_audit_log_triggers.py`
- `sendy_erp/.env.example`
- `sendy_erp/inventory_app/README.md`
- `sendy_erp/requirements.lock`

**Files to modify:**
- `sendy_erp/inventory_app/config.py` (drop fallback defaults)
- `sendy_erp/requirements.txt` (pin to current versions)
- `sendy_erp/requirements-dev.txt` (pin to current versions)
- `sendy_erp/CLAUDE.md` (mig table 064 → 068; add cache contract section)
- `sendy_erp/scripts/*.py` (10 files — replace hardcoded `/Users/putty/` paths with `config.DATABASE_PATH` import)
- `sendy_erp/scripts/RESTORE.md` (add DEPRECATED note pointing to date-named one-offs)

---

## Task 1: Remove insecure `config.py` fallback defaults

**Why:** `SECRET_KEY='sendai-boonsawat-erp-secret'` and `ADMIN_PASSWORD='sendai12345'` are committed defaults. Anyone cloning the repo gets a working admin login if Railway env isn't loaded. Force startup to fail loudly when secrets are missing.

**Files:**
- Modify: `sendy_erp/inventory_app/config.py:16-17`
- Create: `sendy_erp/.env.example`
- Create: `sendy_erp/tests/test_config_secrets.py`

- [ ] **Step 1.1: Write the failing test**

Create `sendy_erp/tests/test_config_secrets.py`:

```python
"""Tests for config.py secret-required behavior.

The app must refuse to import config when SECRET_KEY or ADMIN_PASSWORD
is unset — no committed fallback defaults.
"""
import importlib
import os
import sys

import pytest


def _reload_config(monkeypatch, env):
    monkeypatch.setattr(os, 'environ', env)
    sys.modules.pop('config', None)
    return importlib.import_module('config')


def test_config_loads_when_secrets_present(monkeypatch):
    env = {'SECRET_KEY': 'test-key', 'ADMIN_PASSWORD': 'test-pwd'}
    cfg = _reload_config(monkeypatch, env)
    assert cfg.SECRET_KEY == 'test-key'
    assert cfg.ADMIN_PASSWORD == 'test-pwd'


def test_config_raises_when_secret_key_missing(monkeypatch):
    env = {'ADMIN_PASSWORD': 'test-pwd'}
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        _reload_config(monkeypatch, env)


def test_config_raises_when_admin_password_missing(monkeypatch):
    env = {'SECRET_KEY': 'test-key'}
    with pytest.raises(RuntimeError, match='ADMIN_PASSWORD'):
        _reload_config(monkeypatch, env)
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_config_secrets.py -v
```

Expected: 2 tests FAIL (the missing-env tests). The first passes because env-set works regardless.

- [ ] **Step 1.3: Modify `config.py` to require env vars**

Replace lines 15-17 of `sendy_erp/inventory_app/config.py`:

```python
# Override via Railway environment variables.
# No committed fallbacks — fail loudly so a stray local run can't
# silently boot with publicly-known secrets.
def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} environment variable is required. "
            f"For local dev, copy .env.example to .env and set values."
        )
    return value


SECRET_KEY     = _require_env('SECRET_KEY')
ADMIN_PASSWORD = _require_env('ADMIN_PASSWORD')
```

- [ ] **Step 1.4: Run test to verify it passes**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_config_secrets.py -v
```

Expected: 3 PASS.

- [ ] **Step 1.5: Create `.env.example`**

Create `sendy_erp/.env.example`:

```bash
# Copy to .env and fill in. Never commit .env (already gitignored).
# These mirror the Railway env vars.

SECRET_KEY=replace-me-with-a-long-random-string
ADMIN_PASSWORD=replace-me-with-a-strong-password

# Optional — defaults to instance/ for dev, /data for Railway prod.
# DATA_DIR=/data

# Optional — set to '1' on HTTPS deployments to enforce secure cookie.
# SESSION_COOKIE_SECURE=1
```

- [ ] **Step 1.6: Verify `.env` is gitignored**

```bash
cd sendy_erp && grep -q '^\.env$' .gitignore || echo ".env" >> .gitignore
git check-ignore -v .env
```

Expected: `.env` ignored.

- [ ] **Step 1.7: Verify dev server still starts (with env)**

```bash
cd sendy_erp/inventory_app && SECRET_KEY=dev-only ADMIN_PASSWORD=dev-only ~/.virtualenvs/erp/bin/python -c "import config; print('OK', config.SECRET_KEY[:8])"
```

Expected: `OK dev-only`

- [ ] **Step 1.8: Run full suite to ensure no regressions**

Most test fixtures don't import `config` standalone, but `conftest.py` may. Confirm:

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest -x --tb=short
```

Expected: All pre-existing tests still pass. If `conftest` needs env, add it to `pytest.ini`:

```ini
# Append to existing pytest.ini under [pytest] section
env =
    SECRET_KEY=test-only-secret
    ADMIN_PASSWORD=test-only-admin
```

Note: only add if needed (i.e. if step shows failures from missing env in non-secret tests). Use `pytest-env` plugin if not already available (add to `requirements-dev.txt` in Task 2).

- [ ] **Step 1.9: Commit**

```bash
cd sendy_erp && git add inventory_app/config.py .env.example .gitignore tests/test_config_secrets.py pytest.ini
git commit -m "$(cat <<'EOF'
security: require SECRET_KEY / ADMIN_PASSWORD env vars

Drop committed fallback defaults ('sendai-boonsawat-erp-secret' /
'sendai12345'). Config raises RuntimeError on import if either env
var is unset. Adds .env.example for local dev and tests covering
the fail-loud behavior.

Railway prod already sets these via env, so deploy is unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Pin `requirements.txt` + `requirements-dev.txt`

**Why:** All deps currently use `>=` with no upper bound. A Railway redeploy could silently pull a breaking minor (Flask 3.2, pandas 3.0). Pin to current working versions; add upper-bound on majors.

**Files:**
- Modify: `sendy_erp/requirements.txt`
- Modify: `sendy_erp/requirements-dev.txt`

- [ ] **Step 2.1: Capture current installed versions from working venv**

```bash
~/.virtualenvs/erp/bin/pip freeze | grep -iE '^(flask|werkzeug|gunicorn|pandas|openpyxl|rapidfuzz|pytest)' > /tmp/sendy_pins.txt
cat /tmp/sendy_pins.txt
```

Record the output. Expected example (your actual numbers may differ — use what the venv reports):

```
Flask==3.1.0
Werkzeug==3.1.3
gunicorn==23.0.0
openpyxl==3.1.5
pandas==2.2.3
rapidfuzz==3.10.1
pytest==8.3.3
```

- [ ] **Step 2.2: Rewrite `requirements.txt` with pinned + capped versions**

Replace `sendy_erp/requirements.txt` (use the actual versions from Step 2.1; this is the template):

```
# Pinned to currently-working versions. Cap on major to avoid silent
# breaking-change pulls on Railway redeploy. Bump deliberately with
# a tested upgrade PR.
flask>=3.1,<4
werkzeug>=3.1,<4
gunicorn>=23.0,<24
pandas>=2.2,<3
openpyxl>=3.1,<4
rapidfuzz>=3.10,<4
```

- [ ] **Step 2.3: Rewrite `requirements-dev.txt`**

Replace `sendy_erp/requirements-dev.txt`:

```
-r requirements.txt
pytest>=8.3,<9
pytest-env>=1.1,<2
```

(`pytest-env` only if Step 1.8 needed it.)

- [ ] **Step 2.4: Sanity check install resolves**

```bash
~/.virtualenvs/erp/bin/pip install --dry-run -r sendy_erp/requirements-dev.txt
```

Expected: no conflicts; all pins satisfiable.

- [ ] **Step 2.5: Run full suite**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest -x --tb=short
```

Expected: all pass.

- [ ] **Step 2.6: Commit**

```bash
cd sendy_erp && git add requirements.txt requirements-dev.txt
git commit -m "$(cat <<'EOF'
deps: pin to current versions with major-version upper bound

Replace bare >= with `>=current,<next_major` for flask, werkzeug,
gunicorn, pandas, openpyxl, rapidfuzz, pytest. Prevents Railway
redeploy from silently picking up a breaking Flask/pandas minor.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Migration 069 — `products.units_per_carton/units_per_box` NOT NULL

**Why:** Both columns are nullable and code defaults them to 1 in scattered places (`COALESCE`, ternary). Tightening the schema removes that drift surface and is the natural complement to the known `ratio=1` borderline issues.

**Files:**
- Create: `sendy_erp/data/migrations/069_products_units_not_null.sql`
- Create: `sendy_erp/data/migrations/069_products_units_not_null.rollback.sql`
- Create: `sendy_erp/tests/test_mig069_units_not_null.py`

- [ ] **Step 3.1: Audit current NULL count**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/python -c "
import sqlite3
c = sqlite3.connect('inventory_app/instance/inventory.db')
print('units_per_carton NULL:', c.execute('SELECT COUNT(*) FROM products WHERE units_per_carton IS NULL').fetchone()[0])
print('units_per_box NULL:',    c.execute('SELECT COUNT(*) FROM products WHERE units_per_box IS NULL').fetchone()[0])
"
```

Record the counts. If both are 0 the migration is purely schema-tightening; if non-zero, backfill is needed.

- [ ] **Step 3.2: Write the failing test**

Create `sendy_erp/tests/test_mig069_units_not_null.py`:

```python
"""Tests for migration 069: products.units_per_carton/units_per_box NOT NULL.

The migration must:
- Backfill any existing NULL rows to 1.
- Enforce NOT NULL going forward.
- Default to 1 for new inserts that omit the columns.
"""
import sqlite3

import pytest


def _connect(tmp_db):
    return sqlite3.connect(tmp_db)


def test_units_columns_are_not_null_after_migration(tmp_db):
    """After init_db runs migration 069, columns must be NOT NULL."""
    conn = _connect(tmp_db)
    info = {row[1]: row for row in conn.execute("PRAGMA table_info(products)")}
    # PRAGMA table_info row: (cid, name, type, notnull, default_value, pk)
    assert info['units_per_carton'][3] == 1, "units_per_carton should be NOT NULL"
    assert info['units_per_box'][3] == 1, "units_per_box should be NOT NULL"
    conn.close()


def test_existing_null_rows_backfilled_to_one(tmp_db):
    """No products may have NULL units_per_carton or units_per_box."""
    conn = _connect(tmp_db)
    null_count = conn.execute(
        "SELECT COUNT(*) FROM products "
        "WHERE units_per_carton IS NULL OR units_per_box IS NULL"
    ).fetchone()[0]
    assert null_count == 0
    conn.close()


def test_insert_without_units_defaults_to_one(tmp_db):
    """A new product inserted without units_per_carton/box must default to 1."""
    conn = _connect(tmp_db)
    conn.execute(
        "INSERT INTO products (sku, product_name) VALUES (999999, 'test_mig069')"
    )
    row = conn.execute(
        "SELECT units_per_carton, units_per_box FROM products WHERE sku=999999"
    ).fetchone()
    assert row == (1, 1)
    conn.execute("DELETE FROM products WHERE sku=999999")
    conn.commit()
    conn.close()
```

- [ ] **Step 3.3: Run test to verify it fails**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest tests/test_mig069_units_not_null.py -v
```

Expected: 3 tests FAIL (columns are still nullable, no default).

- [ ] **Step 3.4: Write migration 069**

Create `sendy_erp/data/migrations/069_products_units_not_null.sql`:

```sql
-- mig 069: products.units_per_carton / units_per_box NOT NULL DEFAULT 1
--
-- These columns are conceptually "how many base units per packaging
-- level". The code paths already treat NULL as 1 via COALESCE; this
-- migration removes that drift surface.
--
-- SQLite can't ALTER COLUMN to add NOT NULL — we do the standard
-- rebuild-table dance (CREATE _new, INSERT...SELECT, DROP, RENAME).

BEGIN;

-- 1) Backfill any existing NULLs to 1.
UPDATE products SET units_per_carton = 1 WHERE units_per_carton IS NULL;
UPDATE products SET units_per_box    = 1 WHERE units_per_box    IS NULL;

-- 2) Rebuild products with the tighter constraint.
-- Mirror current columns from sendy_erp/CLAUDE.md schema (post-mig 068).
CREATE TABLE products_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sku                 INTEGER UNIQUE,
    product_name        TEXT,
    units_per_carton    INTEGER NOT NULL DEFAULT 1,
    units_per_box       INTEGER NOT NULL DEFAULT 1,
    unit_type           TEXT DEFAULT 'ตัว',
    hard_to_sell        INTEGER DEFAULT 0,
    cost_price          REAL,
    base_sell_price     REAL,
    low_stock_threshold INTEGER DEFAULT 10,
    is_active           INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    shopee_stock        INTEGER DEFAULT 0,
    lazada_stock        INTEGER DEFAULT 0,
    brand_id            INTEGER REFERENCES brands(id),
    category_id         INTEGER REFERENCES categories(id),
    family_id           INTEGER REFERENCES product_families(id),
    series              TEXT,
    model               TEXT,
    size                TEXT,
    color_code          TEXT,
    packaging           TEXT,
    condition           TEXT,
    pack_variant        TEXT,
    material            TEXT
);

INSERT INTO products_new
SELECT id, sku, product_name,
       units_per_carton, units_per_box, unit_type, hard_to_sell,
       cost_price, base_sell_price, low_stock_threshold, is_active,
       created_at, updated_at, shopee_stock, lazada_stock,
       brand_id, category_id, family_id,
       series, model, size, color_code, packaging, condition,
       pack_variant, material
FROM products;

DROP TABLE products;
ALTER TABLE products_new RENAME TO products;

-- 3) Recreate the products_full VIEW (was dropped with the table).
-- IMPORTANT: this MUST match the live definition. Pull current
-- DDL from the source DB before applying — if the VIEW has drifted,
-- update this section accordingly.
DROP VIEW IF EXISTS products_full;

-- The actual CREATE VIEW statement is captured in this file from
-- `SELECT sql FROM sqlite_master WHERE name='products_full'` before
-- migration runs. The runner copies it back from the DDL snapshot.
-- See companion .rollback.sql for the saved-off definition.

COMMIT;
```

⚠️ **Important:** SQLite VIEWs and triggers referencing `products` are dropped when the table is dropped. Before writing this migration's final SQL, capture the current `products_full` VIEW definition with:

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/python -c "
import sqlite3
c = sqlite3.connect('inventory_app/instance/inventory.db')
for kind in ('view', 'trigger'):
    for row in c.execute(f\"SELECT name, sql FROM sqlite_master WHERE type='{kind}' AND sql LIKE '%products%'\"):
        print(f'-- === {kind.upper()}: {row[0]} ===')
        print(row[1] + ';')
        print()
" > /tmp/products_dependents.sql
cat /tmp/products_dependents.sql
```

Paste the captured `CREATE VIEW products_full AS ...` (and any triggers referencing products) at the **end of section 3** of the migration, after the `DROP VIEW IF EXISTS products_full;` line. This is the durable record per the rebuild-table pattern in `sendy_erp/CLAUDE.md` (mig runner is filename-keyed; in-place edit is safe pre-merge).

- [ ] **Step 3.5: Write the rollback**

Create `sendy_erp/data/migrations/069_products_units_not_null.rollback.sql`:

```sql
-- Rollback for mig 069. Recreates products with nullable units columns.
-- Run manually + DELETE applied_migrations row for '069_products_units_not_null'.
BEGIN;

CREATE TABLE products_old AS SELECT * FROM products;
DROP TABLE products;

CREATE TABLE products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sku                 INTEGER UNIQUE,
    product_name        TEXT,
    units_per_carton    INTEGER,
    units_per_box       INTEGER,
    unit_type           TEXT DEFAULT 'ตัว',
    hard_to_sell        INTEGER DEFAULT 0,
    cost_price          REAL,
    base_sell_price     REAL,
    low_stock_threshold INTEGER DEFAULT 10,
    is_active           INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    shopee_stock        INTEGER DEFAULT 0,
    lazada_stock        INTEGER DEFAULT 0,
    brand_id            INTEGER REFERENCES brands(id),
    category_id         INTEGER REFERENCES categories(id),
    family_id           INTEGER REFERENCES product_families(id),
    series              TEXT,
    model               TEXT,
    size                TEXT,
    color_code          TEXT,
    packaging           TEXT,
    condition           TEXT,
    pack_variant        TEXT,
    material            TEXT
);

INSERT INTO products SELECT * FROM products_old;
DROP TABLE products_old;

-- Recreate dependents (paste the same DDL snapshot used in the forward mig).

COMMIT;
```

- [ ] **Step 3.6: Take a DB backup before applying**

```bash
cd sendy_erp && ./scripts/backup_db.sh
ls -la data/exports/backups/ | tail -3
```

Expected: a fresh `inventory.db.<timestamp>` exists.

- [ ] **Step 3.7: Restart server to apply migration**

```bash
sendy-down
sendy-up
sendy-log | head -30
```

Expected: log contains `Applied migration 069_products_units_not_null` and no rollback/error.

- [ ] **Step 3.8: Run test to verify it passes**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest tests/test_mig069_units_not_null.py -v
```

Expected: 3 PASS.

- [ ] **Step 3.9: Smoke-test a product CRUD flow**

```bash
curl -s http://localhost:5001/healthz
```

Expected: `ok` or 200. Then in browser, hit `/products` and confirm the listing renders.

- [ ] **Step 3.10: Commit**

```bash
cd sendy_erp && git add data/migrations/069_*.sql tests/test_mig069_units_not_null.py
git commit -m "$(cat <<'EOF'
schema: products.units_per_carton/units_per_box NOT NULL DEFAULT 1 (mig 069)

Both columns were nullable; code paths already treated NULL as 1
via COALESCE. Tighten the schema to remove that drift surface.

Backfill any existing NULLs to 1, then rebuild products with
NOT NULL + DEFAULT 1. VIEW products_full recreated from captured
snapshot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Migration 070 — `audit_log` triggers on products / transactions / received_payments

**Why:** Table `audit_log` shipped with mig 023 but never got triggers. No audit trail for product edits, ledger inserts, or payment changes. This is the unblocking step for any future "who changed this and when" investigation.

**Files:**
- Create: `sendy_erp/data/migrations/070_audit_log_triggers.sql`
- Create: `sendy_erp/data/migrations/070_audit_log_triggers.rollback.sql`
- Create: `sendy_erp/tests/test_audit_log_triggers.py`

- [ ] **Step 4.1: Inspect existing `audit_log` schema**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/python -c "
import sqlite3
c = sqlite3.connect('inventory_app/instance/inventory.db')
print(c.execute(\"SELECT sql FROM sqlite_master WHERE name='audit_log'\").fetchone()[0])
"
```

Record the column list. Expected approximately: `id, table_name, row_pk, action, changed_at, changed_by, old_json, new_json`. Adjust trigger payloads to match the actual columns.

- [ ] **Step 4.2: Write the failing test**

Create `sendy_erp/tests/test_audit_log_triggers.py`:

```python
"""Tests for migration 070: audit_log triggers on products, transactions,
received_payments.

Each table must emit one audit_log row on INSERT/UPDATE/DELETE with the
correct action, table_name, and row identifier.
"""
import sqlite3

import pytest


def _connect(tmp_db):
    return sqlite3.connect(tmp_db)


def test_product_insert_logged(tmp_db):
    conn = _connect(tmp_db)
    conn.execute(
        "INSERT INTO products (sku, product_name) VALUES (888888, 'audit_test')"
    )
    row = conn.execute(
        "SELECT table_name, action FROM audit_log "
        "WHERE table_name='products' AND row_pk = "
        "(SELECT id FROM products WHERE sku=888888) "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row == ('products', 'INSERT')
    conn.execute("DELETE FROM products WHERE sku=888888")
    conn.commit()
    conn.close()


def test_product_update_logged(tmp_db):
    conn = _connect(tmp_db)
    conn.execute("INSERT INTO products (sku, product_name) VALUES (888889, 'a')")
    pid = conn.execute("SELECT id FROM products WHERE sku=888889").fetchone()[0]
    conn.execute("UPDATE products SET product_name='b' WHERE id=?", (pid,))
    row = conn.execute(
        "SELECT action FROM audit_log "
        "WHERE table_name='products' AND row_pk=? "
        "ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
    assert row[0] == 'UPDATE'
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()


def test_transactions_insert_logged(tmp_db):
    conn = _connect(tmp_db)
    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO transactions "
        "(product_id, txn_type, quantity_change, reference_no, note) "
        "VALUES (?, 'ADJUST', 0, 'AUDIT_TEST', 'mig070 test')",
        (pid,),
    )
    row = conn.execute(
        "SELECT table_name, action FROM audit_log "
        "WHERE table_name='transactions' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row == ('transactions', 'INSERT')
    # Cleanup: the IN/OUT stock-levels trigger will have applied; reverse it.
    conn.execute(
        "DELETE FROM transactions WHERE reference_no='AUDIT_TEST'"
    )
    conn.commit()
    conn.close()


def test_received_payments_insert_logged(tmp_db):
    conn = _connect(tmp_db)
    # Schema may vary — adapt minimal-required insert to the live schema.
    # See `SELECT sql FROM sqlite_master WHERE name='received_payments'`.
    pytest.skip("Adapt this test to received_payments live schema; see Step 4.1 audit_log payload spec")
```

Notes:
- The `received_payments` test is a placeholder — fill it in after Step 4.1 reveals the column list. Use the minimal INSERT plus a DELETE cleanup matching the test pattern of the first three.

- [ ] **Step 4.3: Run test to verify it fails**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest tests/test_audit_log_triggers.py -v
```

Expected: 3 FAIL (no triggers yet), 1 SKIP.

- [ ] **Step 4.4: Write migration 070**

Create `sendy_erp/data/migrations/070_audit_log_triggers.sql`:

```sql
-- mig 070: audit_log triggers on products, transactions, received_payments
--
-- Captures INSERT / UPDATE / DELETE on each table with old/new JSON
-- snapshots. Adapt JSON column lists to match the actual table
-- columns at the time this migration is written (see Step 4.1 audit).
--
-- changed_by is best-effort: SQLite has no app-session context, so
-- triggers cannot read 'current user'. Application-layer audit writes
-- (where user identity matters) should continue to insert audit_log
-- rows directly. Triggers are a safety net for ANY mutation, including
-- direct sqlite3 CLI edits.

BEGIN;

-- ── products ───────────────────────────────────────────────────────────────
CREATE TRIGGER audit_products_ins AFTER INSERT ON products
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, changed_at, new_json)
    VALUES ('products', NEW.id, 'INSERT', CURRENT_TIMESTAMP,
            json_object(
                'sku', NEW.sku, 'product_name', NEW.product_name,
                'cost_price', NEW.cost_price, 'base_sell_price', NEW.base_sell_price,
                'is_active', NEW.is_active
            ));
END;

CREATE TRIGGER audit_products_upd AFTER UPDATE ON products
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, changed_at, old_json, new_json)
    VALUES ('products', NEW.id, 'UPDATE', CURRENT_TIMESTAMP,
            json_object(
                'product_name', OLD.product_name,
                'cost_price', OLD.cost_price, 'base_sell_price', OLD.base_sell_price,
                'is_active', OLD.is_active
            ),
            json_object(
                'product_name', NEW.product_name,
                'cost_price', NEW.cost_price, 'base_sell_price', NEW.base_sell_price,
                'is_active', NEW.is_active
            ));
END;

CREATE TRIGGER audit_products_del AFTER DELETE ON products
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, changed_at, old_json)
    VALUES ('products', OLD.id, 'DELETE', CURRENT_TIMESTAMP,
            json_object(
                'sku', OLD.sku, 'product_name', OLD.product_name,
                'cost_price', OLD.cost_price
            ));
END;

-- ── transactions ───────────────────────────────────────────────────────────
-- INSERT only — ledger is append-only by convention (no UPDATE/DELETE
-- triggers; if a row needs reversing, the app inserts a counter-row).
CREATE TRIGGER audit_transactions_ins AFTER INSERT ON transactions
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, changed_at, new_json)
    VALUES ('transactions', NEW.id, 'INSERT', CURRENT_TIMESTAMP,
            json_object(
                'product_id', NEW.product_id, 'txn_type', NEW.txn_type,
                'quantity_change', NEW.quantity_change,
                'reference_no', NEW.reference_no
            ));
END;

-- ── received_payments ──────────────────────────────────────────────────────
-- Adapt column list after Step 4.1.
CREATE TRIGGER audit_received_payments_ins AFTER INSERT ON received_payments
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, changed_at, new_json)
    VALUES ('received_payments', NEW.id, 'INSERT', CURRENT_TIMESTAMP,
            json_object('payload', 'see live schema — fill at write time'));
END;

CREATE TRIGGER audit_received_payments_upd AFTER UPDATE ON received_payments
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, changed_at, old_json, new_json)
    VALUES ('received_payments', NEW.id, 'UPDATE', CURRENT_TIMESTAMP,
            json_object('payload', 'see live schema'),
            json_object('payload', 'see live schema'));
END;

CREATE TRIGGER audit_received_payments_del AFTER DELETE ON received_payments
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, changed_at, old_json)
    VALUES ('received_payments', OLD.id, 'DELETE', CURRENT_TIMESTAMP,
            json_object('payload', 'see live schema'));
END;

COMMIT;
```

⚠️ **At write time:** open `inventory.db` with sqlite3 CLI, dump `received_payments` schema, replace the placeholder json_object payloads with the real column list (mirror the `products` trigger style).

- [ ] **Step 4.5: Write the rollback**

Create `sendy_erp/data/migrations/070_audit_log_triggers.rollback.sql`:

```sql
BEGIN;
DROP TRIGGER IF EXISTS audit_products_ins;
DROP TRIGGER IF EXISTS audit_products_upd;
DROP TRIGGER IF EXISTS audit_products_del;
DROP TRIGGER IF EXISTS audit_transactions_ins;
DROP TRIGGER IF EXISTS audit_received_payments_ins;
DROP TRIGGER IF EXISTS audit_received_payments_upd;
DROP TRIGGER IF EXISTS audit_received_payments_del;
COMMIT;
```

- [ ] **Step 4.6: Apply migration via server restart**

```bash
sendy-down && sendy-up && sleep 2 && sendy-log | head -30
```

Expected: log shows `Applied migration 070_audit_log_triggers`. No errors.

- [ ] **Step 4.7: Run tests to verify they pass**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest tests/test_audit_log_triggers.py -v
```

Expected: 3 PASS, 1 SKIP (or 4 PASS if `received_payments` test was filled in).

- [ ] **Step 4.8: Confirm full suite still passes**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest -x --tb=short
```

Expected: all pass. **Watch for slowdowns** — every transaction insert now writes 1 extra row; check if any existing test takes noticeably longer.

- [ ] **Step 4.9: Commit**

```bash
cd sendy_erp && git add data/migrations/070_*.sql tests/test_audit_log_triggers.py
git commit -m "$(cat <<'EOF'
audit: triggers on products / transactions / received_payments (mig 070)

audit_log table shipped in mig 023 but had no triggers. Add INSERT/
UPDATE/DELETE triggers on the three highest-value tables. JSON
payloads cover the columns that matter for forensics
(name/price/status on products; ledger fields on transactions;
amounts on received_payments).

Trigger-based audit is a safety net — app-layer code that knows
the user identity should continue to write to audit_log directly
so changed_by is populated.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `bp_products` route-level integration tests

**Why:** `blueprints/products.py` is 561 LOC with 20+ CRUD routes — 0 route-level tests. Highest-leverage gap since product edits touch money (cost_price, base_sell_price).

**Files:**
- Create: `sendy_erp/tests/test_bp_products_routes.py`

- [ ] **Step 5.1: Write 5 happy-path tests**

Create `sendy_erp/tests/test_bp_products_routes.py`:

```python
"""Route-level integration tests for bp_products.

Picks 5 highest-value endpoints. Uses the tmp_db fixture so tests
never touch the live DB.
"""
import pytest
from flask import url_for

from app import app as flask_app


@pytest.fixture
def client(tmp_db, monkeypatch):
    """Flask test client backed by tmp_db.

    Logs in as admin via session manipulation to skip the login flow.
    """
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        with c.session_transaction() as sess:
            sess['user_id'] = 1            # admin user seeded in fixture
            sess['username'] = 'admin'
            sess['role'] = 'admin'
        yield c


def test_products_index_renders(client):
    resp = client.get('/products')
    assert resp.status_code == 200
    assert 'products' in resp.get_data(as_text=True).lower()


def test_product_detail_renders_first_product(client, tmp_db):
    import sqlite3
    pid = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1"
    ).fetchone()[0]
    resp = client.get(f'/products/{pid}')
    assert resp.status_code == 200


def test_api_products_search_returns_json(client):
    resp = client.get('/api/products/search?q=ค้อน')
    assert resp.status_code == 200
    assert resp.is_json


def test_product_pricing_endpoint_renders(client, tmp_db):
    import sqlite3
    pid = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1"
    ).fetchone()[0]
    resp = client.get(f'/products/{pid}/pricing')
    assert resp.status_code == 200


def test_product_404_for_unknown_id(client):
    resp = client.get('/products/99999999')
    assert resp.status_code in (404, 302)  # 302 if redirected to listing
```

- [ ] **Step 5.2: Run tests**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest tests/test_bp_products_routes.py -v
```

Expected: all 5 PASS. Adjust route assertions if any return different statuses than expected (e.g. if `/products/<id>/pricing` is admin-only and the fixture user differs).

- [ ] **Step 5.3: Commit**

```bash
cd sendy_erp && git add tests/test_bp_products_routes.py
git commit -m "$(cat <<'EOF'
test: route-level integration tests for bp_products (5 endpoints)

Covers products index, detail, search API, pricing, and 404 path.
bp_products had 0 route-level coverage; these protect the
highest-value endpoints (touches cost_price and base_sell_price).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: POST whitelist permission tests

**Why:** `app.py:117-138` defines `_STAFF_POST_OK` and `_MANAGER_POST_OK` frozensets that gate every POST. No test asserts the gate works. A typo in `_STAFF_POST_OK` (e.g. someone adds a route to the list "just because") could silently grant write access.

**Files:**
- Create: `sendy_erp/tests/test_post_whitelist.py`

- [ ] **Step 6.1: Write tests**

Create `sendy_erp/tests/test_post_whitelist.py`:

```python
"""Tests for the POST permission gate in app.py.

Verifies that:
- staff cannot POST to endpoints outside _STAFF_POST_OK
- manager cannot POST to endpoints outside _MANAGER_POST_OK
- admin can POST anywhere
- /hr/* and /cashbook/* GET are also blocked for staff
"""
import pytest

from app import app as flask_app, _STAFF_POST_OK, _MANAGER_POST_OK


@pytest.fixture
def client(tmp_db):
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


def _login_as(client, role):
    with client.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = role
        sess['role']     = role


def test_staff_blocked_from_admin_post(client):
    _login_as(client, 'staff')
    resp = client.post('/users', data={})  # admin-only
    assert resp.status_code in (302, 403)  # 302 = redirect to dashboard


def test_manager_blocked_from_admin_post(client):
    _login_as(client, 'manager')
    resp = client.post('/users', data={})
    assert resp.status_code in (302, 403)


def test_manager_allowed_for_manager_endpoint(client):
    _login_as(client, 'manager')
    # /import-payments is in _MANAGER_POST_OK; bare POST returns 400/200
    # but not the permission redirect.
    resp = client.post('/import-payments', data={}, follow_redirects=False)
    assert resp.status_code != 302 or '/dashboard' not in (resp.headers.get('Location') or '')


def test_staff_blocked_from_hr_get(client):
    _login_as(client, 'staff')
    resp = client.get('/hr/', follow_redirects=False)
    # Redirected to dashboard with permission flash
    assert resp.status_code == 302
    assert '/dashboard' in (resp.headers.get('Location') or '/')


def test_staff_blocked_from_cashbook_get(client):
    _login_as(client, 'staff')
    resp = client.get('/cashbook/', follow_redirects=False)
    assert resp.status_code == 302
    assert '/dashboard' in (resp.headers.get('Location') or '/')


def test_staff_whitelist_subset_of_manager(client):
    """_STAFF_POST_OK must be a subset of _MANAGER_POST_OK."""
    assert _STAFF_POST_OK.issubset(_MANAGER_POST_OK)


def test_whitelists_contain_no_typos():
    """Every endpoint in either whitelist must exist in the Flask URL map."""
    endpoints = {rule.endpoint for rule in flask_app.url_map.iter_rules()}
    missing_staff = _STAFF_POST_OK - endpoints
    missing_mgr   = _MANAGER_POST_OK - endpoints
    assert not missing_staff, f"_STAFF_POST_OK references unknown endpoints: {missing_staff}"
    assert not missing_mgr,   f"_MANAGER_POST_OK references unknown endpoints: {missing_mgr}"
```

- [ ] **Step 6.2: Run tests**

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest tests/test_post_whitelist.py -v
```

Expected: 7 PASS. If `test_whitelists_contain_no_typos` fails, it caught a real bug — fix `app.py:117-138` (likely a stale endpoint name from a removed/renamed route).

- [ ] **Step 6.3: Commit**

```bash
cd sendy_erp && git add tests/test_post_whitelist.py
git commit -m "$(cat <<'EOF'
test: POST permission whitelist enforcement

Covers staff blocked from admin POST, manager blocked from admin
POST, manager allowed at manager endpoints, staff blocked from
hr/cashbook GET, and assertion that every endpoint in the
whitelists actually exists in the URL map (catches typos).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Replace hardcoded `/Users/putty/` paths in scripts

**Why:** `grep -l "/Users/putty/" scripts/*.py` returns 10 files. Most are one-offs but still in the tree. If a friend or coworker runs any of them, they break or — worse — write to a wrong DB. Centralize on `config.DATABASE_PATH`.

**Files:**
- Modify: `sendy_erp/scripts/apply_decision_ratios.py`
- Modify: `sendy_erp/scripts/apply_platform_overview_mapping.py`
- Modify: `sendy_erp/scripts/apply_decision_remaps.py`
- Modify: `sendy_erp/scripts/apply_stock_and_mapping_csv.py`
- Modify: `sendy_erp/scripts/apply_unit_type_change.py`
- Modify: `sendy_erp/scripts/auto_pay_pre_feb_2026.py`
- Modify: `sendy_erp/scripts/build_catalog_candidates.py`
- Modify: `sendy_erp/scripts/commission_check.py`
- Modify: `sendy_erp/scripts/fix_k11_1155_unit_conversion.py`
- Modify: `sendy_erp/scripts/import_listing_mapping_csv.py`

- [ ] **Step 7.1: Inspect the canonical pattern**

```bash
cd sendy_erp && grep -n "/Users/putty/" scripts/commission_check.py
```

Expected output shows the hardcoded `DB_PATH` line (around line 55).

- [ ] **Step 7.2: Apply the fix to each file**

For **each** script in the list above, replace the hardcoded path with config-driven resolution. Use this canonical patch:

```python
# OLD (example from commission_check.py)
DB_PATH = '/Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db'

# NEW
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'inventory_app'))
from config import DATABASE_PATH as DB_PATH
```

For scripts that import other things from `inventory_app/` already, just add:

```python
from config import DATABASE_PATH as DB_PATH
```

and drop the hardcoded string.

For scripts where the path is to a directory (e.g. `/Users/putty/Downloads/`), accept an env var:

```python
INPUT_DIR = os.environ.get('SENDY_INPUT_DIR', os.path.expanduser('~/Downloads'))
```

- [ ] **Step 7.3: Verify no hardcoded paths remain**

```bash
cd sendy_erp && grep -l "/Users/putty/" scripts/*.py
```

Expected: no output.

- [ ] **Step 7.4: Smoke-run one read-only script to confirm it still works**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/python scripts/audit_data_gaps.py 2>&1 | head -10
```

Expected: runs without ImportError. (If `audit_data_gaps.py` wasn't in the modified list, swap with any read-only script that WAS modified, e.g. `commission_check.py`.)

- [ ] **Step 7.5: Commit**

```bash
cd sendy_erp && git add scripts/*.py
git commit -m "$(cat <<'EOF'
scripts: replace hardcoded /Users/putty/ paths with config.DATABASE_PATH

10 scripts had a literal '/Users/putty/Sendai-Boonsawat/...' DB
path or Downloads/ directory. Centralize on
config.DATABASE_PATH (DB) and SENDY_INPUT_DIR env var (file
inputs) so coworkers / CI can run them.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Mark one-off `scripts/` as DEPRECATED

**Why:** `scripts/` has ~60 files; many are date-named or SKU-named one-offs from past reimport sessions. New contributors can't tell which are live. Add a `# DEPRECATED: ...` comment at the top of every clearly-one-off file.

**Files:**
- Modify: `sendy_erp/scripts/RESTORE.md` (add an inventory section)
- Modify: each one-off script (add header comment)

- [ ] **Step 8.1: Identify one-offs**

One-off signals: date in filename (e.g. `auto_pay_pre_feb_2026.py`), SKU in filename (e.g. `fix_k11_1155_unit_conversion.py`), or "reimport_2026_*" subfolders.

```bash
cd sendy_erp && ls scripts/ | grep -E '(2026|pre_|fix_k|fix_|reimport_)' > /tmp/sendy_oneoffs.txt
cat /tmp/sendy_oneoffs.txt
```

- [ ] **Step 8.2: Add DEPRECATED header to each**

For each file in `/tmp/sendy_oneoffs.txt`, prepend:

```python
"""DEPRECATED: one-off from 2026-MM-DD reimport. Kept for audit trail.

Do not run in normal operation. If a similar fix is needed, write
a new dated script — don't re-execute this one.
"""
```

If the file already has a module docstring, append the DEPRECATED line as the first line of the existing docstring.

- [ ] **Step 8.3: Update `scripts/RESTORE.md`**

Append to `sendy_erp/scripts/RESTORE.md`:

```markdown

## One-off scripts (DEPRECATED)

Scripts in `scripts/` with dates in their names (e.g. `auto_pay_pre_feb_2026.py`)
or specific SKU references (e.g. `fix_k11_1155_unit_conversion.py`) are
one-off fixes that were run once and kept for audit trail. They are
marked with a `DEPRECATED:` docstring at the top.

**Do not re-run them.** If a similar fix is needed, write a new dated
script. If you're cleaning up, you can safely delete any script marked
DEPRECATED after confirming with git log that it ran successfully in
its target session.

Active utility scripts (no date / SKU in name): `backup_db.sh`,
`audit_data_gaps.py`, `bsn_completeness_report.py`,
`commission_check.py`, `parse_*` family, `build_*` family.
```

- [ ] **Step 8.4: Commit**

```bash
cd sendy_erp && git add scripts/*.py scripts/RESTORE.md
git commit -m "$(cat <<'EOF'
scripts: mark one-off reimport scripts as DEPRECATED

Date-named and SKU-named scripts get a DEPRECATED docstring
header. scripts/RESTORE.md gains a guide to active vs one-off.
No functional change — purely documentation hygiene.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Update `sendy_erp/CLAUDE.md` schema/migrations section

**Why:** The file currently says "ปัจจุบัน — เวอร์ชัน schema migration 037" and the migrations table stops at 064. Latest applied is 068. Future Claude sessions read this file as authoritative — drift here causes wrong assumptions.

**Files:**
- Modify: `sendy_erp/CLAUDE.md`

- [ ] **Step 9.1: Update the schema-version line**

In `sendy_erp/CLAUDE.md`, find the line:

```
## Schema ตาราง (ปัจจุบัน — เวอร์ชัน schema migration 037, 2026-05-07)
```

Replace with:

```
## Schema ตาราง (ปัจจุบัน — เวอร์ชัน schema migration 070, 2026-05-21)
```

- [ ] **Step 9.2: Update the migrations table**

In the migrations table (after the row for `064`), append:

```markdown
| **065** | 2026-05-20 | **ar_followup_log (mig 065) + outreach workspace** |
| 066 | 2026-05-20 | data-quality cleanup (โหล/กล่อง normalize, 33 fixes) |
| **067** | 2026-05-20 | **drop cashbook_vat_flag (NoVat-only by design)** |
| 068 | 2026-05-21 | drop express_sales.brand_kind (write-only cache removal) |
| **069** | 2026-05-21 | **products.units_per_carton/box NOT NULL DEFAULT 1** |
| **070** | 2026-05-21 | **audit_log triggers on products/transactions/received_payments** |
```

Also update the latest-migration callout earlier in the file:

```
data/migrations/         ← 0NN_name.sql + .rollback.sql (latest: **070**)
```

And the heading "Migrations (latest: 064...":

```markdown
## Migrations (latest: 070 — see `data/migrations/` for canonical files)
```

- [ ] **Step 9.3: Add `payment_amounts` cache contract section**

Append a new section after "Recent business modules":

```markdown
## Denormalized cache contracts (read before touching payment math)

Two tables hold cached/derived values that the app must keep in sync
with their source ledger. Drift here causes silent finance bugs.

### `payment_amounts` (mig 058)
- **Source of truth:** `received_payments` per-IV allocations.
- **Invariant:** for every `received_payments` row with allocations,
  there is exactly one `payment_amounts` row per (payment_id, doc_no)
  with `amount_applied` summing to the payment.
- **Drift signal:** `SUM(payment_amounts.amount_applied) ≠
  received_payments.amount` for the same payment_id.
- **Recovery:** rerun `payments_alloc.allocate_fifo()` for the
  affected customer; it is idempotent and recomputes from scratch.

### `credit_note_amounts` (mig 062)
- **Source of truth:** ใบลดหนี้ master CSV (NOT `sales_transactions`
  SR rows — those are pre-VAT line totals, not the customer-facing
  CN amount).
- **Invariant:** every CN doc_no has exactly one row in
  `credit_note_amounts` with the master-CSV amount.
- **Drift signal:** missing doc_no, or `amount` differs from the CSV.
- **Recovery:** rerun `/payment-status` CN import (PR #36/#37 UI).
```

- [ ] **Step 9.4: Commit**

```bash
cd sendy_erp && git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(CLAUDE.md): bump migration table 064 -> 070, add cache contracts

Schema-version line and migration table were stale (last 064 listed,
heading said 037). Bring both current through mig 070 (this PR).
Adds a new "Denormalized cache contracts" section documenting the
payment_amounts and credit_note_amounts invariants — drift here
caused the phantom-credit and phantom-overpaid bugs (PR #27,
mig 062).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Add `inventory_app/README.md`

**Why:** `inventory_app/` is the heart of the app and has no README. Onboarding a new contributor (Put's friend, Codex, fresh Claude) means rummaging through CLAUDE.md and CLAUDE-MD scattered files. One concise README pointing at the right docs is enough.

**Files:**
- Create: `sendy_erp/inventory_app/README.md`

- [ ] **Step 10.1: Write the README**

Create `sendy_erp/inventory_app/README.md`:

```markdown
# inventory_app — Sendy ERP Flask application

Flask 3 / Python 3.9 / SQLite. No ORM. Single-process gunicorn on Railway.

## Quick start

```bash
# From sendy_erp/ root
~/.virtualenvs/erp/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill SECRET_KEY + ADMIN_PASSWORD
~/.virtualenvs/erp/bin/python inventory_app/app.py
# or: sendy-up / sendy-down / sendy-log
```

App listens on `:5001`. DB at `instance/inventory.db`.

## Layout

| File | Responsibility |
|------|----------------|
| `app.py` | Flask app + most routes + POST whitelist (`_STAFF_POST_OK`, `_MANAGER_POST_OK`) |
| `models.py` | Business logic + raw-SQL queries |
| `database.py` | Schema bootstrap + migration runner (filename-keyed) |
| `config.py` | `DATABASE_PATH`, `SECRET_KEY`, `ADMIN_PASSWORD` (env-required) |
| `parse_weekly.py` | BSN cp874 weekly file parser |
| `parse_cashbook.py` | NoVat cashbook Excel parser |
| `parse_platform.py` | Shopee / Lazada / TikTok parser |
| `bsn_suggest.py` | Smart-mapping suggester |
| `bsn_units.py` | BSN unit alias normalizer (mig 064) |
| `commission.py` | Commission engine (unit-aware, mig 063/064) |
| `payments_alloc.py` | FIFO payment-to-invoice allocator |
| `cashflow.py` | AR aging + cash-flow dashboard helpers |
| `revenue.py` | Revenue dashboard (Phase 3, by brand) |
| `ar_followup.py` | AR follow-up workspace + outreach log (mig 065) |
| `hr.py` / `hr_queries.py` | HR module reads/writes |
| `import_cashbook.py` | Cashbook Excel import (round-trip) |
| `import_credit_notes.py` | CN preview/confirm two-step (PR #36/#37) |
| `sku_code_utils.py` | SKU-code builder (post-mig 033 naming rule) |
| `blueprints/` | `bp_products`, `bp_cashbook`, `bp_hr`, `bp_supplier_catalogue`, `bp_mobile` |
| `imports/` | Express AR/AP parsers (Sendai Trading) |
| `templates/`, `static/` | Jinja + CSS |
| `instance/inventory.db` | SQLite live DB (NOT in git) |

## Authoritative references (do not duplicate)

- **Schema, routes, env, deploy:** `../CLAUDE.md` (app-level)
- **Workspace conventions, brand routing:** `../../CLAUDE.md` (workspace-level)
- **Permission model + POST whitelist:** `/erp-permissions` skill or `app.py:117-138`
- **Recent migrations 054-070:** `../CLAUDE.md` Migrations table

## Testing

```bash
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only \
  ~/.virtualenvs/erp/bin/pytest                          # full suite
~/.virtualenvs/erp/bin/pytest tests/test_cashflow.py     # one file
~/.virtualenvs/erp/bin/pytest -k vat                     # by keyword
```

`tests/conftest.py` provides `tmp_db` (copies live DB to a tmp_path
and monkeypatches `config.DATABASE_PATH`) so tests never touch the
live DB.

## Adding a migration

1. Drop `data/migrations/NNN_name.sql` + `NNN_name.rollback.sql`
2. Restart the server (`sendy-down && sendy-up`)
3. Runner auto-applies on `init_db()`; sha256 + duration_ms recorded
   in `applied_migrations`
4. **Applied migrations are immutable** — fix bugs with a NEW
   higher-NNN migration, not by editing the old one (see
   `../CLAUDE.md` for the rare-exception rules)

## Adding a route

1. Decide blueprint vs `app.py`. New domain-coherent routes go in a
   blueprint under `blueprints/<name>.py` (see `bp_products` for the
   pattern).
2. If the route accepts POST, add its endpoint name to
   `_STAFF_POST_OK` or `_MANAGER_POST_OK` in `app.py`.
3. Restart the server (auto-reloader does NOT pick up new URL map
   entries reliably).
4. Add a test in `tests/test_<area>.py` (route-level or unit, per
   the existing style).
```

- [ ] **Step 10.2: Commit**

```bash
cd sendy_erp && git add inventory_app/README.md
git commit -m "$(cat <<'EOF'
docs: add inventory_app/README.md as onboarding entry point

One concise README pointing at CLAUDE.md, conftest patterns,
migration runner contract, and POST whitelist. Removes the
"where do I even start" friction for new contributors.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Open PR + verification gate

**Files:** none (orchestration)

- [ ] **Step 11.1: Run full suite from a clean shell**

```bash
sendy-down
cd sendy_erp && SECRET_KEY=test-only ADMIN_PASSWORD=test-only ~/.virtualenvs/erp/bin/pytest -v --tb=short 2>&1 | tail -50
```

Expected: all tests pass (count > 47, since we added ~20 new tests). Record total in PR body.

- [ ] **Step 11.2: Start dev server, hit `/healthz`**

```bash
sendy-up
sleep 3
curl -s http://localhost:5001/healthz
sendy-log | tail -20
```

Expected: `ok` (or 200), no migration errors in log.

- [ ] **Step 11.3: Browser smoke**

Open `http://localhost:5001/products` and confirm the listing renders. Open `/cashflow` and confirm AR aging renders. Open `/hr/` (as admin) and confirm employee list renders.

- [ ] **Step 11.4: Push the branch + open PR**

```bash
cd sendy_erp && git push -u origin chore/tech-debt-sprint-1
gh pr create --title "chore: tech debt sprint #1 — security, deps, schema, audit, tests, docs" --body "$(cat <<'EOF'
## Summary

Closes 7 of the 30 tech-debt items audited 2026-05-21. The rest are deferred with explicit reason in `docs/superpowers/plans/2026-05-21-tech-debt-sprint-1.md`.

### Shipped
1. **security:** `SECRET_KEY` / `ADMIN_PASSWORD` env vars required — no committed fallbacks (`.env.example` added)
2. **deps:** pin `requirements.txt` + `requirements-dev.txt` with major-version upper bounds
3. **schema:** mig 069 — `products.units_per_carton/box NOT NULL DEFAULT 1`
4. **audit:** mig 070 — `audit_log` triggers on products / transactions / received_payments
5. **test coverage:** `bp_products` route-level integration tests (5 endpoints)
6. **test coverage:** POST whitelist permission tests (7 assertions inc. typo guard)
7. **scripts:** centralize 10 scripts on `config.DATABASE_PATH`; mark one-offs DEPRECATED
8. **docs:** bump `sendy_erp/CLAUDE.md` migration table 064 → 070; add cache-contract section
9. **docs:** new `inventory_app/README.md`

### Deferred (with reason)

See plan doc for the full list. Headline reasons: WACC + cost_price backfill (needs backup ceremony + Put go-ahead), data-quality items requiring per-product judgment, paused-by-design items (barcode, photos, brand voice), opportunistic refactors (app.py / models.py split).

## Test plan

- [x] `pytest -v` — all tests pass
- [x] `sendy-up` → `/healthz` returns 200, log shows migrations 069 + 070 applied
- [x] Browser smoke: `/products`, `/cashflow`, `/hr/` render
- [x] Hardcoded-path grep returns empty (`grep -l "/Users/putty/" scripts/*.py`)
- [x] Permission gate covered (`pytest tests/test_post_whitelist.py`)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL returned by `gh`.

---

## Self-review checklist

After all 11 tasks land:

1. **Spec coverage:** every fixable item from the audit either has a task above, is in the "Out of scope" list with reason, or is in the "Already resolved" list.
2. **Placeholder scan:** no `TBD`, no `# implement later`, no `# similar to above`. The one explicit "fill in at write time" is for the `received_payments` audit trigger payload and the dependent-VIEW DDL capture in mig 069 — both are documented in their respective steps with the exact command to run.
3. **Type consistency:** test fixtures use `tmp_db` (defined in existing `conftest.py`); migration numbers ascend 069 → 070 with no gap; CLAUDE.md mig table mentions 065-068 (already shipped) + 069/070 (this PR).

---

## Execution recommendation

Run via `superpowers:subagent-driven-development` — each task is independent enough to dispatch a fresh subagent. Two-stage review between tasks catches surprises in the migrations / route tests early.

After the PR is up: `/scrutinize` for an outsider read of the plan vs the actual diff, then `codex:rescue` for an independent second-pass review.
