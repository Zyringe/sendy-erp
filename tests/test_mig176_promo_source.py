"""Migration 176 — promotions.source + date_start stamp for the 2026-06-01
catalog batch.

Built on `tmp_db_conn` (clones the LIVE dev DB with data) rather than a
purpose-made SQLite file: the point of this migration is stamping the REAL
566-row batch, and a synthetic table would not exercise the actual
`promo_name LIKE 'catalog 2026-06-01%'` population or the real audit
triggers.

Drop-first fixture: the live DB this is copied from may already have 176
applied (init_db() applies any migration file present the moment any test
imports `app`), so the `db` fixture rolls back to a known pre-176 state
before each test runs its own forward migration explicitly. This is the same
shape as tests/test_mig171_backfill_packaging_th.py.
"""
import sqlite3
from pathlib import Path

import pytest

MIG = Path(__file__).resolve().parents[1] / "data/migrations/176_promo_source_and_dates.sql"
ROLLBACK = Path(__file__).resolve().parents[1] / "data/migrations/176_promo_source_and_dates.rollback.sql"

BATCH_PATTERN = "catalog 2026-06-01%"
AUDIT_TRIGGERS = ("audit_promotions_insert", "audit_promotions_update", "audit_promotions_delete")


def _trigger_bodies(conn):
    return {row[0]: row[1] for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)",
        AUDIT_TRIGGERS,
    )}


def _n_batch(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE promo_name LIKE ?", (BATCH_PATTERN,)
    ).fetchone()[0]


def _insert_control_row(conn):
    """A promo that must NOT match the batch pattern -- proves the stamp is scoped."""
    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, is_active)"
        " VALUES (?, 'manual test', 'percent', 10, 1)",
        (pid,),
    )
    conn.commit()


@pytest.fixture
def db(tmp_db_conn):
    conn = tmp_db_conn
    cols = {row[1] for row in conn.execute("PRAGMA table_info(promotions)")}
    if "source" in cols:
        conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
        conn.commit()
    return conn


def test_stamps_only_the_catalog_batch_control_row_stays_unstamped(db):
    conn = db
    n_batch = _n_batch(conn)
    # CONTROL: a fixture that lost the batch would make everything below vacuous.
    assert n_batch > 0, "fixture lost the 2026-06-01 catalog batch"

    _insert_control_row(conn)

    conn.executescript(MIG.read_text(encoding="utf-8"))

    n_stamped = conn.execute(
        "SELECT COUNT(*) FROM promotions"
        " WHERE promo_name LIKE ? AND source = 'catalog-import' AND date_start = '2026-06-01'",
        (BATCH_PATTERN,),
    ).fetchone()[0]
    assert n_stamped == n_batch

    control = conn.execute(
        "SELECT source, date_start FROM promotions WHERE promo_name = 'manual test'"
    ).fetchone()
    assert control == (None, None), "the stamp must not touch a promo outside the batch"


def test_check_rejects_an_unlisted_source(db):
    conn = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value,"
            " is_active, source) VALUES (?, 'shopee test', 'percent', 10, 1, 'shopee')",
            (pid,),
        )


def test_audit_triggers_carry_source_after_the_migration(db):
    conn = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    bodies = _trigger_bodies(conn)
    assert set(bodies) == set(AUDIT_TRIGGERS)
    for name, sql in bodies.items():
        assert "NEW.source" in sql or "OLD.source" in sql, f"{name} does not audit source"


def test_rollback_drops_column_unstamps_only_migrated_rows_restores_trigger_bodies(db):
    conn = db
    before_triggers = _trigger_bodies(conn)
    assert before_triggers, "control: the 3 audit triggers must exist pre-migration"

    n_batch = _n_batch(conn)
    assert n_batch > 0
    _insert_control_row(conn)

    conn.executescript(MIG.read_text(encoding="utf-8"))
    # CONTROL: it really was applied.
    assert conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE source = 'catalog-import'"
    ).fetchone()[0] == n_batch

    conn.executescript(ROLLBACK.read_text(encoding="utf-8"))

    cols = {row[1] for row in conn.execute("PRAGMA table_info(promotions)")}
    assert "source" not in cols

    dates = [r[0] for r in conn.execute(
        "SELECT date_start FROM promotions WHERE promo_name LIKE ?", (BATCH_PATTERN,)
    ).fetchall()]
    assert all(d is None for d in dates), "the batch's date_start must roll back to NULL"

    control = conn.execute(
        "SELECT promo_name FROM promotions WHERE promo_name = 'manual test'"
    ).fetchone()
    assert control is not None, "the control row must survive the rollback"

    after_triggers = _trigger_bodies(conn)
    assert after_triggers == before_triggers, "trigger bodies must come back byte-identical"


def test_rollback_leaves_a_date_start_an_operator_set_afterwards(db):
    """Keying the un-stamp on (source='catalog-import' AND date_start='2026-06-01') rather
    than on promo_name alone means a date someone deliberately edited after the migration
    survives a later rollback. Mirrors the equivalent case in test_mig171."""
    conn = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    row = conn.execute(
        "SELECT id FROM promotions WHERE promo_name LIKE ? LIMIT 1", (BATCH_PATTERN,)
    ).fetchone()
    pid = row[0]
    conn.execute("UPDATE promotions SET date_start = '2026-07-15' WHERE id = ?", (pid,))
    conn.commit()

    conn.executescript(ROLLBACK.read_text(encoding="utf-8"))

    kept = conn.execute("SELECT date_start FROM promotions WHERE id = ?", (pid,)).fetchone()[0]
    assert kept == '2026-07-15', "rollback destroyed a date an operator set after the migration"
