"""Migration 176 — promotions.source + date_start stamp for the 2026-06-01
catalog batch.

Built on `tmp_db_conn` (clones the LIVE dev DB with data) so the real audit
triggers and migration 177's one-per-slot triggers are in force. The batch
itself is NOT inherited (#507): every test that needs one seeds it on
throwaway products (`_seed_batch`) after deleting whatever the clone holds
under `BATCH_PATTERN`. The clone cannot be trusted to hold it: the dev DB
carries 566 such rows, but on prod all 566 were renamed to
`catalog 2024-01-01 (...)` on 2026-08-28 (audit_log), so on any prod-derived
DB the pattern matches 0 rows. `BATCH_PATTERN` still mirrors the migration's
own hard-coded literal, which is historical and is what the forward stamp
matches.

The non-batch rows are not scenery: each sits on the far side of a WHERE
clause this file pins (the stamp's name pattern: the 'manual test' control;
the un-stamp's `date_start` clause: the operator-edited row; its `source`
clause: the manual promo dated 2026-06-01). Without them, deleting that clause
would change no row (verification-discipline.md, "a test that cannot fail",
shape #8).

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
import re
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


def _date_start(conn, promo_id):
    return conn.execute(
        "SELECT date_start FROM promotions WHERE id = ?", (promo_id,)
    ).fetchone()[0]


_pid = [960000]

# How many rows `_seed_batch` writes. A literal on purpose, not len() of the
# seed: a seed that shrinks to nothing must turn the count controls red.
N_BATCH_SEEDED = 2


def _mk_product(conn, name):
    _pid[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, 'ตัว', 100, 60, 1)", (f'{name} #{_pid[0]}',))
    conn.commit()
    return cur.lastrowid


def _seed_batch(conn):
    """The 2026-06-01 batch in its pre-176 shape: no `source` column yet (the
    `db` fixture rolled 176 back), date_start and date_end NULL, is_active = 1.
    One row per promo_name the catalogue import writes
    (scripts/import_catalog_pricing.py: `(special_price)` is always 'fixed',
    `(promo)` takes the row's own type).

    Whatever the clone holds under BATCH_PATTERN is deleted first, so the
    population the migration stamps is exactly these rows on the dev DB (566
    inherited) and on a prod-derived one (0) alike. No table holds a foreign
    key to promotions.id; the delete only writes audit_log rows in the clone.
    Each row gets its own fresh product: both are price-shaped, and migration
    177 allows one current price-shaped promo per product.
    """
    conn.execute("DELETE FROM promotions WHERE promo_name LIKE ?", (BATCH_PATTERN,))
    ids = []
    for promo_name, promo_type, value in (
        ("catalog 2026-06-01 (promo)", "percent", 10),
        ("catalog 2026-06-01 (special_price)", "fixed", 90),
    ):
        ids.append(conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value,"
            " date_start, date_end, is_active) VALUES (?, ?, ?, ?, NULL, NULL, 1)",
            (_mk_product(conn, "mig176 batch"), promo_name, promo_type, value),
        ).lastrowid)
    conn.commit()
    return ids


def _pick_products_without_active_promos(conn, n=1):
    """`n` distinct products holding ZERO active promotions.

    `tmp_db_conn` clones the live dev DB with its data, including the
    566-row 2026-06-01 catalog batch, so `SELECT id FROM products LIMIT 1`
    can land on a product that already carries an active price-slot promo
    from that batch. Migration 177's one-per-slot trigger then refuses a
    second price-shaped INSERT (a fresh 'percent' control/test row) for an
    unrelated reason. Different products (rule #1) sidesteps this so only
    the constraint each test actually exercises -- the `source` CHECK, or
    "the stamp doesn't touch a row outside the batch" -- can still make the
    insert fail.
    """
    rows = conn.execute(
        "SELECT id FROM products WHERE id NOT IN "
        "(SELECT product_id FROM promotions WHERE is_active = 1) "
        "ORDER BY id LIMIT ?", (n,)
    ).fetchall()
    assert len(rows) == n, f"needed {n} promo-free products, found {len(rows)}"
    return [r[0] for r in rows]


def _insert_control_row(conn):
    """A promo that must NOT match the batch pattern -- proves the stamp is scoped."""
    pid = _pick_products_without_active_promos(conn, 1)[0]
    cur = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, is_active)"
        " VALUES (?, 'manual test', 'percent', 10, 1)",
        (pid,),
    )
    conn.commit()
    return cur.lastrowid


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
    _seed_batch(conn)
    n_batch = _n_batch(conn)
    # CONTROL: exactly the rows seeded above. A seed that lands nothing would make
    # everything below vacuous; an inherited row would put rows the test does not
    # control into the population.
    assert n_batch == N_BATCH_SEEDED, "the batch must be exactly the rows this test seeded"

    control_id = _insert_control_row(conn)

    # The migration's header states "date_end and is_active are NOT touched".
    # Nothing tested it, so a stamp that also closed or deactivated the batch
    # would have shipped green.
    before_untouched = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, date_end, is_active FROM promotions WHERE promo_name LIKE ?",
        (BATCH_PATTERN,),
    )}
    assert len(before_untouched) == n_batch  # control: we captured the whole batch

    conn.executescript(MIG.read_text(encoding="utf-8"))

    after_untouched = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, date_end, is_active FROM promotions WHERE promo_name LIKE ?",
        (BATCH_PATTERN,),
    )}
    assert after_untouched == before_untouched, (
        "the stamp must leave date_end and is_active exactly as they were")

    n_stamped = conn.execute(
        "SELECT COUNT(*) FROM promotions"
        " WHERE promo_name LIKE ? AND source = 'catalog-import' AND date_start = '2026-06-01'",
        (BATCH_PATTERN,),
    ).fetchone()[0]
    assert n_stamped == n_batch

    control = conn.execute(
        "SELECT source, date_start FROM promotions WHERE id = ?", (control_id,)
    ).fetchone()
    assert tuple(control) == (None, None), "the stamp must not touch a promo outside the batch"


def test_check_rejects_an_unlisted_source(db):
    conn = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    # Each INSERT below is its own fresh 'percent' (price-slot) promo; sharing
    # ONE product across all four would make each collide with the previous
    # one under migration 177 regardless of `source`. A distinct promo-free
    # product per insert (rule #1) means only the `source` CHECK is on trial.
    pids = _pick_products_without_active_promos(conn, 4)

    # CONTROL, and it has to come first: every value the CHECK is meant to
    # ACCEPT must insert cleanly. Observing only a refusal cannot tell a
    # correct CHECK from one that rejects everything -- or from a table that
    # lost the column and now errors on any insert naming it.
    for pid, accepted in zip(pids, ('catalog-import', 'manual', None)):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value,"
            " is_active, source) VALUES (?, 'accepted control', 'percent', 10, 1, ?)",
            (pid, accepted),
        )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value,"
            " is_active, source) VALUES (?, 'shopee test', 'percent', 10, 1, 'shopee')",
            (pids[3],),
        )


def test_audit_triggers_carry_source_after_the_migration(db):
    conn = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    bodies = _trigger_bodies(conn)
    assert set(bodies) == set(AUDIT_TRIGGERS)
    for name, sql in bodies.items():
        # `NEW.source` ANYWHERE is too weak: a trigger naming the column only
        # in a WHEN guard -- and never writing it into the JSON it records --
        # satisfies that while auditing nothing. Pin the payload key itself,
        # which is how all three triggers spell a recorded column.
        assert "'source'," in sql, f"{name} does not record source in its JSON payload"

    # The UPDATE trigger additionally has to FIRE on a source-only edit;
    # its payload could name the column while the WHEN clause never wakes it.
    assert re.search(r"OLD\.source\s+IS NOT NEW\.source",
                     bodies['audit_promotions_update']), (
        "audit_promotions_update would not fire when only `source` changed")


def test_rollback_drops_column_unstamps_only_migrated_rows_restores_trigger_bodies(db):
    conn = db
    # Sanity-only: confirms the fixture reached a working pre-state (3 triggers
    # present). NOT the expected value for the byte-identity assertion below --
    # this snapshot is itself produced by running ROLLBACK.sql (see the `db`
    # fixture), so comparing against it would be circular. See module docstring.
    assert len(_trigger_bodies(conn)) == len(AUDIT_TRIGGERS), (
        "control: all 3 audit triggers must exist pre-migration -- a truthiness\n"
        "        check here passes on a dict holding only ONE of them")

    _seed_batch(conn)
    n_batch = _n_batch(conn)
    assert n_batch == N_BATCH_SEEDED, "the batch must be exactly the rows this test seeded"
    control_id = _insert_control_row(conn)
    # FAR SIDE of the un-stamp's `source = 'catalog-import'` clause: a promo the
    # stamp never owned that carries the stamp's own date. Its own fresh product,
    # since it is price-shaped (migration 177).
    far_side_id = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value,"
        " date_start, is_active) VALUES (?, 'manual dated 2026-06-01', 'percent', 10,"
        " '2026-06-01', 1)",
        (_mk_product(conn, "mig176 far side"),),
    ).lastrowid
    conn.commit()

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
    assert len(dates) == N_BATCH_SEEDED  # count first: all() over no rows is True
    assert all(d is None for d in dates), "the batch's date_start must roll back to NULL"
    assert _date_start(conn, far_side_id) == '2026-06-01', (
        "the rollback un-stamped a promo the migration never stamped")

    control = conn.execute(
        "SELECT promo_name FROM promotions WHERE id = ?", (control_id,)
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
    ids = _seed_batch(conn)
    # CONTROL, before any row is picked: with no batch this used to die on a None row.
    assert _n_batch(conn) == N_BATCH_SEEDED, "the batch must be exactly the rows this test seeded"
    edited, untouched = ids

    conn.executescript(MIG.read_text(encoding="utf-8"))
    # CONTROL: the stamp reached both rows, so the edit below replaces a stamped date
    # and the un-edited row has a stamp for the rollback to remove.
    assert (_date_start(conn, edited), _date_start(conn, untouched)) == ('2026-06-01', '2026-06-01')
    conn.execute("UPDATE promotions SET date_start = '2026-07-15' WHERE id = ?", (edited,))
    conn.commit()

    conn.executescript(ROLLBACK.read_text(encoding="utf-8"))

    # CONTROL: the rollback did un-stamp the row nobody edited, so the edited row
    # below survives the `date_start` clause, not a rollback that un-stamped nothing.
    assert _date_start(conn, untouched) is None, "control: the un-edited batch row must roll back to NULL"
    assert _date_start(conn, edited) == '2026-07-15', (
        "rollback destroyed a date an operator set after the migration")
