"""Issue #389 — the conversion-role alert must survive a caller that is
already holding the write lock.

`cross_unit_hazard` reports a malformed multi-input [แพ็ค] formula by filing a
durable `system_alerts` row. That row used to be written through a FRESH
connection, which is correct for the read/list callers (they are mid-SELECT
loop and must not open a write transaction there) but wrong for the callers
that have already written on their own connection: SQLite hands the fresh
connection SQLITE_BUSY, it waits out the 10s busy timeout, and the helper's
best-effort `except` swallows it. The operator then gets the block with no
durable explanation — precisely what system_alerts exists to prevent.

Which callers are actually exposed was measured on `main` @ 399fd10
(2026-09-07); the issue's own "Where" list is wrong in both directions:

  save_unit_conversions        exposed from loop iteration 2 (first INSERT
                               takes the lock, held to the single commit)
  approve_pending_suggestion   holds the lock (three writes precede it) but the
                               branch is UNREACHABLE — see its test below
  upsert_unit_conversion       NOT exposed — hazard check precedes its writes
  update_unit_conversion_ratio NOT exposed — same shape
  the two read/list callers    NOT exposed — they never write

The seam these tests inject at is the alert helper's own `get_connection`, so
"locked out" is fast and certain instead of a 10-second wait. Each test asserts
the ALERT ROW, not the absence of an exception: the fix removes the contention
entirely, so a test that only checked "no raise" would pass for the wrong
reason once the seam it patches is no longer reached.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import models
from models import products, system_alerts


PACK, LOOSE, CARD, CLEAN = 961101, 961102, 961103, 961104
KIND = 'conversion_role_error'


def _seed_product(conn, pid, name, unit="ตัว"):
    conn.execute("INSERT INTO products (id, product_name, unit_type) VALUES (?, ?, ?)",
                 (pid, name, unit))


def _seed_malformed_pack_formula(conn):
    """An active [แพ็ค] with 2 inputs and NO roles — the shape
    conversion_roles.component_product_id refuses to guess at."""
    _seed_product(conn, PACK, "hammer pack", "แผง")
    _seed_product(conn, LOOSE, "hammer loose", "อัน")
    _seed_product(conn, CARD, "blister card", "แผง")
    fid = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty) VALUES (?,?,?)",
        ("[แพ็ค] hammer pack ⟵ 1 อัน + blister card", PACK, 1)).lastrowid
    for pid in (LOOSE, CARD):
        conn.execute(
            "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
            " VALUES (?,?,1,NULL)", (fid, pid))
    conn.commit()
    return fid


def _open_alerts(conn):
    return conn.execute(
        "SELECT id, dedupe_key FROM system_alerts WHERE kind=? AND resolved_at IS NULL",
        (KIND,)).fetchall()


@pytest.fixture
def impatient_alert_connection(monkeypatch):
    """Make the alert helper's fresh connection give up on a lock in 0.2s.

    Without this the pre-fix behaviour is identical but takes the full 10s
    busy timeout. Patching the name inside system_alerts (it does
    `from database import get_connection`) leaves every other caller alone.
    """
    import database

    def _quick():
        conn = sqlite3.connect(database.DATABASE_PATH, timeout=0.2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr(system_alerts, 'get_connection', _quick)


def test_alert_lands_when_save_unit_conversions_already_holds_the_lock(
        empty_db_conn, impatient_alert_connection):
    c = empty_db_conn
    fid = _seed_malformed_pack_formula(c)
    _seed_product(c, CLEAN, "clean product", "ตัว")
    c.commit()
    assert _open_alerts(c) == []          # control: this query can come back empty

    result = models.save_unit_conversions([
        {'product_id': CLEAN, 'bsn_unit': 'กล่อง', 'ratio': 3},   # takes the write lock
        {'product_id': PACK,  'bsn_unit': 'อัน',   'ratio': 1},   # malformed → must alert
    ])

    # Behaviour that must NOT change: the clean row saves, the malformed one blocks.
    assert result['saved'] == 1
    assert [b['kind'] for b in result['blocked']] == ['configuration_error']

    alerts = _open_alerts(c)
    assert len(alerts) == 1, "the malformed formula left no durable alert"
    assert alerts[0]['dedupe_key'] == str(fid)


def test_approve_pending_suggestion_cannot_reach_the_alert_path(empty_db_conn):
    """The other write caller that holds a lock at the call — and why it still
    needs no fix.

    approve_pending_suggestion does write three times before reaching
    cross_unit_hazard, so it genuinely holds the lock. But the product id it
    passes comes from create_structured_product, which is a plain INSERT
    returning lastrowid: a brand-new id cannot appear in conversion_formulas or
    conversion_formula_inputs, so neither of cross_unit_hazard's two loops can
    find a formula at all, malformed or otherwise. The configuration_error
    branch is unreachable there.

    This test is the guard on that reasoning rather than on today's behaviour:
    if the approval flow is ever changed to reuse an EXISTING product, the id
    stops being fresh, the branch becomes reachable, and this goes red — which
    is the moment that call site would need the same treatment as
    save_unit_conversions.
    """
    c = empty_db_conn
    _seed_malformed_pack_formula(c)
    c.commit()

    max_before = c.execute("SELECT MAX(id) FROM products").fetchone()[0]
    new_pid = products.create_structured_product(
        {'product_name': 'brand new sku', 'unit_type': 'แผง'},
        created_via='smart_mapping')

    assert new_pid > max_before, "the approval flow no longer creates a FRESH product"
    # ...and therefore no formula can reference it, in either direction.
    referenced = c.execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE output_product_id = ?"
        " UNION ALL"
        " SELECT COUNT(*) FROM conversion_formula_inputs WHERE product_id = ?",
        (new_pid, new_pid)).fetchall()
    assert [r[0] for r in referenced] == [0, 0]
    assert models.cross_unit_hazard(c, new_pid, 'อัน') != {'kind': 'configuration_error'}


def test_read_path_does_not_open_a_write_transaction_on_the_callers_connection(
        empty_db_conn):
    """The other half of the contract, and the reason this is not simply
    "always use the caller's connection": a list-builder calls cross_unit_hazard
    once per pending row, mid-SELECT. Filing the alert there must not leave a
    write transaction open on that connection — it would hold the lock for the
    rest of the request."""
    c = empty_db_conn
    fid = _seed_malformed_pack_formula(c)
    assert not c.in_transaction          # control: clean before

    hz = models.cross_unit_hazard(c, PACK, 'อัน')

    assert hz['kind'] == 'configuration_error'
    assert not c.in_transaction, "read path started a write transaction on the caller's conn"
    assert [r['dedupe_key'] for r in _open_alerts(c)] == [str(fid)]
