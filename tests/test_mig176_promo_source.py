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

⚠ The rollback-trigger-byte-identity test compares against
EXPECTED_ORIGINAL_TRIGGER_SQL below, a LITERAL snapshot -- never against
`before_triggers` captured from the `db` fixture. The fixture's own
drop-first branch (above) runs ROLLBACK.sql to reach its pre-state, so a
`before` snapshot captured from it is already the rollback file's OWN
output; comparing it to an `after` snapshot produced by running the same
rollback file again just compares that file to itself and cannot fail no
matter what the file says (Codex review round 1, task-0). The literal was
extracted from `git show 6da54e0:data/schema.sql` (origin/main as of this
branch's base, before 176 ever existed) and independently verified
byte-for-byte against a real `ROLLBACK.sql` run on a snapshot -- see
task-0-report.md's fix section for the verification transcript.
"""
import sqlite3
from pathlib import Path

import pytest

MIG = Path(__file__).resolve().parents[1] / "data/migrations/176_promo_source_and_dates.sql"
ROLLBACK = Path(__file__).resolve().parents[1] / "data/migrations/176_promo_source_and_dates.rollback.sql"

BATCH_PATTERN = "catalog 2026-06-01%"
AUDIT_TRIGGERS = ("audit_promotions_insert", "audit_promotions_update", "audit_promotions_delete")

# Literal snapshot of the pre-176 trigger bodies, exactly as `sqlite_master.sql`
# stores them (no trailing `;` -- SQLite strips it on storage). Extracted from
# `git show 6da54e0:data/schema.sql` (the immutable commit this branch is based
# on), NOT from data/migrations/176_promo_source_and_dates.rollback.sql -- the
# whole point is an expectation the rollback file cannot corrupt and still pass.
EXPECTED_ORIGINAL_TRIGGER_SQL = {
    'audit_promotions_insert': "CREATE TRIGGER audit_promotions_insert\nAFTER INSERT ON promotions\nBEGIN\n    INSERT INTO audit_log (table_name, row_id, action, changed_fields)\n    VALUES (\n        'promotions', NEW.id, 'INSERT',\n        json_object(\n            'product_id',        NEW.product_id,\n            'promo_name',        NEW.promo_name,\n            'promo_type',        NEW.promo_type,\n            'discount_value',    NEW.discount_value,\n            'bundle_buy',        NEW.bundle_buy,\n            'bundle_free',       NEW.bundle_free,\n            'bundle_unit',       NEW.bundle_unit,\n            'bundle_condition',  NEW.bundle_condition,\n            'bundle_tiers_json', NEW.bundle_tiers_json,\n            'gift_desc',         NEW.gift_desc,\n            'gift_qty',          NEW.gift_qty,\n            'date_start',        NEW.date_start,\n            'date_end',          NEW.date_end,\n            'is_active',         NEW.is_active\n        )\n    );\nEND",
    'audit_promotions_update': "CREATE TRIGGER audit_promotions_update\nAFTER UPDATE ON promotions\nWHEN (\n       OLD.product_id        IS NOT NEW.product_id\n    OR OLD.promo_name        IS NOT NEW.promo_name\n    OR OLD.promo_type        IS NOT NEW.promo_type\n    OR OLD.discount_value    IS NOT NEW.discount_value\n    OR OLD.bundle_buy        IS NOT NEW.bundle_buy\n    OR OLD.bundle_free       IS NOT NEW.bundle_free\n    OR OLD.bundle_unit       IS NOT NEW.bundle_unit\n    OR OLD.bundle_condition  IS NOT NEW.bundle_condition\n    OR OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json\n    OR OLD.gift_desc         IS NOT NEW.gift_desc\n    OR OLD.gift_qty          IS NOT NEW.gift_qty\n    OR OLD.date_start        IS NOT NEW.date_start\n    OR OLD.date_end          IS NOT NEW.date_end\n    OR OLD.is_active         IS NOT NEW.is_active\n)\nBEGIN\n    INSERT INTO audit_log (table_name, row_id, action, changed_fields)\n    SELECT 'promotions', NEW.id, 'UPDATE',\n           json_group_object(field, json_array(old_v, new_v))\n    FROM (\n                  SELECT 'product_id'        AS field, OLD.product_id        AS old_v, NEW.product_id        AS new_v WHERE OLD.product_id        IS NOT NEW.product_id\n        UNION ALL SELECT 'promo_name',                OLD.promo_name,                NEW.promo_name                WHERE OLD.promo_name        IS NOT NEW.promo_name\n        UNION ALL SELECT 'promo_type',                OLD.promo_type,                NEW.promo_type                WHERE OLD.promo_type        IS NOT NEW.promo_type\n        UNION ALL SELECT 'discount_value',            OLD.discount_value,            NEW.discount_value            WHERE OLD.discount_value    IS NOT NEW.discount_value\n        UNION ALL SELECT 'bundle_buy',                OLD.bundle_buy,                NEW.bundle_buy                WHERE OLD.bundle_buy        IS NOT NEW.bundle_buy\n        UNION ALL SELECT 'bundle_free',               OLD.bundle_free,               NEW.bundle_free               WHERE OLD.bundle_free       IS NOT NEW.bundle_free\n        UNION ALL SELECT 'bundle_unit',               OLD.bundle_unit,               NEW.bundle_unit               WHERE OLD.bundle_unit       IS NOT NEW.bundle_unit\n        UNION ALL SELECT 'bundle_condition',          OLD.bundle_condition,          NEW.bundle_condition          WHERE OLD.bundle_condition  IS NOT NEW.bundle_condition\n        UNION ALL SELECT 'bundle_tiers_json',         OLD.bundle_tiers_json,         NEW.bundle_tiers_json         WHERE OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json\n        UNION ALL SELECT 'gift_desc',                 OLD.gift_desc,                 NEW.gift_desc                 WHERE OLD.gift_desc         IS NOT NEW.gift_desc\n        UNION ALL SELECT 'gift_qty',                  OLD.gift_qty,                  NEW.gift_qty                  WHERE OLD.gift_qty          IS NOT NEW.gift_qty\n        UNION ALL SELECT 'date_start',                OLD.date_start,                NEW.date_start                WHERE OLD.date_start        IS NOT NEW.date_start\n        UNION ALL SELECT 'date_end',                  OLD.date_end,                  NEW.date_end                  WHERE OLD.date_end          IS NOT NEW.date_end\n        UNION ALL SELECT 'is_active',                 OLD.is_active,                 NEW.is_active                 WHERE OLD.is_active         IS NOT NEW.is_active\n    );\nEND",
    'audit_promotions_delete': "CREATE TRIGGER audit_promotions_delete\nBEFORE DELETE ON promotions\nBEGIN\n    INSERT INTO audit_log (table_name, row_id, action, changed_fields)\n    VALUES (\n        'promotions', OLD.id, 'DELETE',\n        json_object(\n            'product_id',        OLD.product_id,\n            'promo_name',        OLD.promo_name,\n            'promo_type',        OLD.promo_type,\n            'discount_value',    OLD.discount_value,\n            'bundle_buy',        OLD.bundle_buy,\n            'bundle_free',       OLD.bundle_free,\n            'bundle_unit',       OLD.bundle_unit,\n            'bundle_condition',  OLD.bundle_condition,\n            'bundle_tiers_json', OLD.bundle_tiers_json,\n            'gift_desc',         OLD.gift_desc,\n            'gift_qty',          OLD.gift_qty,\n            'is_active',         OLD.is_active\n        )\n    );\nEND",
}


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
    assert tuple(control) == (None, None), "the stamp must not touch a promo outside the batch"


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
    # Sanity-only: confirms the fixture reached a working pre-state (3 triggers
    # present). NOT the expected value for the byte-identity assertion below --
    # this snapshot is itself produced by running ROLLBACK.sql (see the `db`
    # fixture), so comparing against it would be circular. See module docstring.
    assert _trigger_bodies(conn), "control: the 3 audit triggers must exist pre-migration"

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
    assert after_triggers == EXPECTED_ORIGINAL_TRIGGER_SQL, (
        "trigger bodies must come back byte-identical to the pre-176 originals"
        " (commit 6da54e0, origin/main:data/schema.sql) -- NOT merely to whatever"
        " ROLLBACK.sql happened to produce"
    )


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
