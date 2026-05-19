"""Migration 063 — brand_kind trigger becomes unit-aware + one-time repair.

Locks the Codex adversarial-review findings (2026-05-20):

- [high]  The 061-recreated refresh_brand_kind_on_product_brand_change
          trigger matched express_sales BY product_code only. On a split
          code (unit A → product A, unit B → product B) updating product
          A's brand corrupted brand_kind for product B's rows too. 063's
          trigger must resolve (product_code, unit) the same way the import
          resolver does and only touch rows that resolve to NEW.id.
- [high]  063 one-time backfill must repair already-corrupted rows.
- [medium] 061's rollback rebuilt product_code_mapping without dropping the
          trigger first → same SQLite trigger-revalidation crash. 061's
          rollback must now run cleanly with trigger 021 present.
- [high]  Application path: models.set_product_brand() must NOT re-corrupt
          split-code rows with a by-code refresh (the redundant manual
          UPDATE was removed; it now relies on the 063 trigger).

tmp_db is a copy of the live DB (carries trigger 021 / 061-shape mapping),
the exact precondition that mattered on Railway.
"""
import os
import sqlite3

import pytest

import database
import models

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG61 = os.path.join(REPO, "data", "migrations", "061_mapping_unit_aware.sql")
RB61 = os.path.join(
    REPO, "data", "migrations", "061_mapping_unit_aware.rollback.sql")
MIG63 = os.path.join(
    REPO, "data", "migrations", "063_brand_kind_unit_aware_trigger.sql")
RB63 = os.path.join(
    REPO, "data", "migrations",
    "063_brand_kind_unit_aware_trigger.rollback.sql")

TRIGGER = "refresh_brand_kind_on_product_brand_change"


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _reset_pre061(conn):
    """Force product_code_mapping back to the exact PRE-061 shape so the
    test is deterministic whether or not live already has 061/063."""
    conn.executescript("""
        DROP TABLE IF EXISTS product_code_mapping;
        CREATE TABLE product_code_mapping (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code    TEXT UNIQUE NOT NULL,
            bsn_name    TEXT NOT NULL,
            product_id  INTEGER REFERENCES products(id),
            is_ignored  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        ALTER TABLE product_code_mapping ADD COLUMN ignore_reason TEXT;
    """)
    conn.commit()


def _trigger_sql(conn):
    r = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (TRIGGER,)).fetchone()
    return r[0] if r else None


def _two_products(conn):
    """Two distinct active product ids."""
    rows = conn.execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 2").fetchall()
    return rows[0][0], rows[1][0]


def _brand(conn, own):
    return conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=? LIMIT 1",
        (1 if own else 0,)).fetchone()[0]


def _ins_es(conn, code, unit, kind, doc):
    conn.execute(
        "INSERT INTO express_sales "
        "(batch_id,doc_no,line_no,doc_type,date_iso,company_id,"
        " product_code,unit,brand_kind) "
        "VALUES (1,?,1,'IV','2026-05-20',1,?,?,?)",
        (doc, code, unit, kind))


# ── [high] trigger must be unit-aware ────────────────────────────────────────
def test_063_trigger_isolates_split_code(tmp_db):
    """Split code: unit 'กล่อง' → product A, catch-all → product B.
    Changing A's brand must touch ONLY A's express_sales row; B's row
    (resolved via catch-all) must stay untouched. The pre-063 by-code
    trigger would corrupt B's row."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG61)
    _apply(conn, MIG63)

    A, B = _two_products(conn)
    own, third = _brand(conn, True), _brand(conn, False)
    # A starts third_party so setting brand→own is a real OLD!=NEW change
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (third, A))
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (own, B))

    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZSPL63','x',?, 'กล่อง')", (A,))
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZSPL63','x',?, '')", (B,))
    _ins_es(conn, "ZSPL63", "กล่อง", "SENTINEL", "ZA1")   # → A (exact unit)
    _ins_es(conn, "ZSPL63", "ชิ้น", "SENTINEL", "ZB1")    # → B (catch-all)
    conn.commit()

    # fire trigger: change A's brand third → own
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (own, A))
    conn.commit()

    a_kind = conn.execute(
        "SELECT brand_kind FROM express_sales WHERE doc_no='ZA1'"
    ).fetchone()[0]
    b_kind = conn.execute(
        "SELECT brand_kind FROM express_sales WHERE doc_no='ZB1'"
    ).fetchone()[0]
    assert a_kind == "own", "A's row must follow A's new brand"
    assert b_kind == "SENTINEL", (
        "B's row resolves to product B via catch-all — the unit-aware "
        "trigger must NOT touch it when A's brand changes")
    conn.close()


# ── [high] one-time backfill repairs corruption ──────────────────────────────
def test_063_backfill_repairs_corrupted_rows(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG61)

    P, _ = _two_products(conn)
    own = _brand(conn, True)
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (own, P))
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZBF63','x',?, '')", (P,))
    _ins_es(conn, "ZBF63", "ชิ้น", "WRONG", "ZF1")   # corrupted value
    conn.commit()

    _apply(conn, MIG63)   # backfill runs here

    assert conn.execute(
        "SELECT brand_kind FROM express_sales WHERE doc_no='ZF1'"
    ).fetchone()[0] == "own"
    conn.close()


def test_063_backfill_preserves_unresolved_rows(tmp_db):
    """A row whose code resolves to nothing must keep its brand_kind
    (EXISTS guard — never nulled)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG61)
    _ins_es(conn, "ZNOMAP63", "ชิ้น", "KEEP", "ZN1")
    conn.commit()

    _apply(conn, MIG63)

    assert conn.execute(
        "SELECT brand_kind FROM express_sales WHERE doc_no='ZN1'"
    ).fetchone()[0] == "KEEP"
    conn.close()


def test_063_recreates_trigger_pointing_at_mapping(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG61)
    _apply(conn, MIG63)
    sql = _trigger_sql(conn)
    assert sql is not None and "product_code_mapping" in sql
    assert "bsn_unit" in sql, "trigger must be unit-aware"
    conn.close()


# ── [medium] 061 rollback no longer crashes with trigger present ─────────────
def test_061_rollback_runs_with_trigger_present(tmp_db):
    """tmp_db carries trigger 021. Apply 061, then run the fixed 061
    rollback — it must NOT raise the trigger-revalidation crash and must
    restore a working by-code trigger."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    assert _trigger_sql(conn) is not None, (
        "precondition: live DB must carry trigger 021")
    _reset_pre061(conn)
    _apply(conn, MIG61)
    _apply(conn, RB61)          # must NOT raise (the medium finding)

    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(product_code_mapping)")}
    assert "bsn_unit" not in cols, "rollback should restore pre-061 shape"
    assert _trigger_sql(conn) is not None, (
        "rollback must recreate the pre-061 trigger")
    conn.close()


def test_063_rollback_restores_by_code_trigger(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG61)
    _apply(conn, MIG63)
    _apply(conn, RB63)
    sql = _trigger_sql(conn)
    assert sql is not None
    assert "bsn_unit" not in sql, "063 rollback restores by-code trigger"
    conn.close()


# ── [high] application path: set_product_brand() must not re-corrupt ─────────
def test_set_product_brand_is_unit_aware_via_trigger(tmp_db):
    """The real UI path (blueprints/products.py → models.set_product_brand)
    must only refresh brand_kind for rows that resolve to the changed
    product. Before the fix a redundant by-code UPDATE in
    set_product_brand() overwrote the 063 trigger's narrower result and
    re-corrupted the OTHER product's split-code rows."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG61)
    _apply(conn, MIG63)

    A, B = _two_products(conn)
    own, third = _brand(conn, True), _brand(conn, False)
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (third, A))
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (own, B))
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZAPP63','x',?, 'กล่อง')", (A,))
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZAPP63','x',?, '')", (B,))
    _ins_es(conn, "ZAPP63", "กล่อง", "SENTINEL", "ZAPPA")   # → A
    _ins_es(conn, "ZAPP63", "ชิ้น", "SENTINEL", "ZAPPB")    # → B (catch-all)
    conn.commit()
    conn.close()

    # real application path (opens its own monkeypatched connection)
    models.set_product_brand(A, own)

    conn = sqlite3.connect(tmp_db)
    a_kind = conn.execute(
        "SELECT brand_kind FROM express_sales WHERE doc_no='ZAPPA'"
    ).fetchone()[0]
    b_kind = conn.execute(
        "SELECT brand_kind FROM express_sales WHERE doc_no='ZAPPB'"
    ).fetchone()[0]
    assert a_kind == "own", "A's row must follow A's new brand"
    assert b_kind == "SENTINEL", (
        "set_product_brand() must not touch B's row (resolves to product "
        "B via catch-all) — no redundant by-code refresh")
    conn.close()


# ── runner records 063 exactly once (no self-insert) ─────────────────────────
def test_runner_records_063_exactly_once(tmp_db, tmp_path, monkeypatch):
    """tmp_db's applied_migrations is populated (live copy) so the runner
    takes the normal PENDING path. 063 is brand-new → must run once and be
    recorded once, with no self-insert duplicate."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "DELETE FROM applied_migrations WHERE filename="
        "'063_brand_kind_unit_aware_trigger.sql'")
    conn.commit()

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    import shutil
    shutil.copy(MIG63, mig_dir / "063_brand_kind_unit_aware_trigger.sql")
    monkeypatch.setattr(database, "MIGRATIONS_DIR", str(mig_dir))

    ran = database.run_pending_migrations(conn, verbose=False)

    assert ran == ["063_brand_kind_unit_aware_trigger.sql"]
    assert conn.execute(
        "SELECT COUNT(*) FROM applied_migrations WHERE "
        "filename='063_brand_kind_unit_aware_trigger.sql'"
    ).fetchone()[0] == 1
    conn.close()
