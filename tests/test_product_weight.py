"""products.weight_kg / weight_source — migration 159 and its write path.

A parcel weight has to survive three things that have each broken a column in
this codebase before:

  * a form that does not render the box must not blank a stored value
    (`(get(k) or '').strip()` destroyed payroll notes in PR #386);
  * a borrowed marketplace number must stay distinguishable from a scale
    reading, or the first person who bulk-seeds the column erases the
    distinction for good (Codex round 2 on the TikTok listing design);
  * a weight change must leave an audit row — `audit_products_update`
    enumerates its columns, so a new one is invisible until the trigger is
    rewritten.

⚠ `tmp_db` clones the LIVE dev DB *with its data*, so every test here FORCES
the weight state it asserts on rather than inheriting whatever a real product
happens to carry.

    ~/.virtualenvs/erp/bin/python -m pytest tests/test_product_weight.py -q
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


# --------------------------------------------------------------------------
# weight_edit_fields — pure, no DB
# --------------------------------------------------------------------------

def _fn():
    from models.products import weight_edit_fields
    return weight_edit_fields


def test_an_absent_key_writes_nothing():
    """The whole point: a form with no weight box preserves the stored value.

    update_product only writes keys present in the dict, so {} means "leave the
    column alone" — the same contract that stops the edit form zeroing
    hard_to_sell.
    """
    assert _fn()({}) == {}
    assert _fn()({'cost_price': '10'}) == {}


def test_a_blank_box_clears_both_columns():
    assert _fn()({'weight_kg': ''}) == {'weight_kg': None, 'weight_source': None}
    assert _fn()({'weight_kg': '   '}) == {'weight_kg': None, 'weight_source': None}
    assert _fn()({'weight_kg': None}) == {'weight_kg': None, 'weight_source': None}


def test_a_number_carries_its_provenance():
    assert _fn()({'weight_kg': '0.85'}) == {'weight_kg': 0.85,
                                            'weight_source': 'measured'}


def test_absent_and_blank_do_not_collapse_into_each_other():
    """CONTROL for the payroll-note trap: these two must never be equal.

    If someone 'simplifies' the membership check into `(get(k) or '')`, the
    left side becomes the right side and a missing box silently clears a
    weight. This assertion is the one that goes red.
    """
    assert _fn()({}) != _fn()({'weight_kg': ''})


@pytest.mark.parametrize('raw', ['0', '-1', '-0.5', 'abc', '1.2.3'])
def test_a_weight_that_the_check_would_refuse_raises_first(raw):
    """Refused in Python so the operator gets a Thai flash, not a 500 from the
    CHECK added in migration 159."""
    with pytest.raises(ValueError):
        _fn()({'weight_kg': raw})


@pytest.mark.parametrize('raw', ['1e309', '-1e309', 'inf', '-inf', 'Infinity', 'nan'])
def test_a_non_finite_weight_is_refused(raw):
    """The CHECK cannot catch these, so Python is the only place that can.

    `float('1e309')` is `inf`, which is > 0 and therefore satisfies
    `weight_kg > 0` — SQLite stores a REAL infinity and every parcel cost
    derived from it is infinite. `nan` is stored by SQLite as NULL, landing the
    row as (NULL, 'measured') and tripping the pair CHECK as a 500 instead of a
    Thai flash. SQLite has no portable isfinite() for a constraint.
    """
    with pytest.raises(ValueError):
        _fn()({'weight_kg': raw})


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        _fn()({'weight_kg': '1.0'}, source='shopee')


def test_every_allowed_source_is_actually_allowed():
    """CONTROL: the guard above must be capable of passing."""
    from models.products import WEIGHT_SOURCES
    for src in WEIGHT_SOURCES:
        assert _fn()({'weight_kg': '1.0'}, source=src)['weight_source'] == src


def test_update_product_is_allowed_to_write_the_columns():
    """A helper that returns the right dict is useless if update_product's
    allowlist drops the keys on the floor."""
    from models.products import _UPDATABLE_PRODUCT_COLUMNS
    assert 'weight_kg' in _UPDATABLE_PRODUCT_COLUMNS
    assert 'weight_source' in _UPDATABLE_PRODUCT_COLUMNS


@pytest.mark.parametrize('kg', [float('inf'), float('-inf'), float('nan'), 0, -1])
def test_the_model_boundary_refuses_a_bad_weight_without_the_form_helper(kg):
    """`weight_kg` is in the allowlist, so a caller that never touches
    `weight_edit_fields` — a future marketplace or estimated-weight importer, a
    one-off script — reaches the column directly. That path needs its own
    guard, or `inf` gets stored by anything except the HTML form.
    (Codex, second pass.)
    """
    from models.products import _validate_weight_fields
    with pytest.raises(ValueError):
        _validate_weight_fields({'weight_kg': kg, 'weight_source': 'measured'})


def test_the_model_boundary_refuses_an_unknown_source():
    from models.products import _validate_weight_fields
    with pytest.raises(ValueError):
        _validate_weight_fields({'weight_kg': 1.0, 'weight_source': 'shopee'})


def test_update_product_itself_refuses_a_non_finite_weight(tmp_db, tmp_db_conn):
    """Calls the REAL entry point, not the validator it delegates to.

    The parametrized tests above call `_validate_weight_fields` directly, so
    they stay green if the CALL is deleted from `update_product` — proved by
    mutation, and it is the "test exercises a neighbour, not the subject"
    trap. This one goes red for that edit.
    """
    import models
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=?, weight_source=? WHERE id=?",
        (1.5, 'measured', pid))
    tmp_db_conn.commit()

    with pytest.raises(ValueError):
        models.update_product(pid, {'weight_kg': float('inf'),
                                    'weight_source': 'measured'})

    got = tmp_db_conn.execute(
        "SELECT weight_kg FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert got == 1.5, "the refused write must not have landed"


def test_update_product_still_writes_a_good_weight(tmp_db, tmp_db_conn):
    """CONTROL for the test above: if update_product could not write a weight
    at all, that test would pass for the wrong reason."""
    import models
    pid = _pid(tmp_db_conn)
    models.update_product(pid, {'weight_kg': 3.25, 'weight_source': 'estimated'})
    got = tmp_db_conn.execute(
        "SELECT weight_kg, weight_source FROM products WHERE id=?", (pid,)).fetchone()
    assert (got[0], got[1]) == (3.25, 'estimated')


def test_the_model_boundary_accepts_what_it_should():
    """CONTROL: the guard above must be capable of passing, including the
    clear-to-NULL case and a weight_kg sent on its own (whose pair coherence is
    the CHECK's job, not this function's)."""
    from models.products import _validate_weight_fields
    _validate_weight_fields({'weight_kg': 0.85, 'weight_source': 'measured'})
    _validate_weight_fields({'weight_kg': None, 'weight_source': None})
    _validate_weight_fields({'weight_kg': 2.0})
    _validate_weight_fields({'cost_price': 10})


# --------------------------------------------------------------------------
# migration 159 — schema, constraints, audit
# --------------------------------------------------------------------------

def _pid(conn) -> int:
    row = conn.execute(
        "SELECT id FROM products WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()
    if row is None:
        pytest.skip("No active products in the live DB clone")
    return row[0]


def test_the_columns_exist(tmp_db_conn):
    cols = {r[1] for r in tmp_db_conn.execute("PRAGMA table_info(products)")}
    assert 'weight_kg' in cols, "migration 159 not applied to the dev DB"
    assert 'weight_source' in cols


def test_a_valid_pair_is_accepted(tmp_db_conn):
    """CONTROL for the four refusals below — if this cannot land, those tests
    prove nothing about the CHECK and everything about a broken fixture."""
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=?, weight_source=? WHERE id=?",
        (0.9, 'measured', pid))
    got = tmp_db_conn.execute(
        "SELECT weight_kg, weight_source FROM products WHERE id=?", (pid,)).fetchone()
    assert (got[0], got[1]) == (0.9, 'measured')


def test_null_null_is_accepted_because_every_existing_row_is_that(tmp_db_conn):
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=NULL, weight_source=NULL WHERE id=?", (pid,))
    got = tmp_db_conn.execute(
        "SELECT weight_kg, weight_source FROM products WHERE id=?", (pid,)).fetchone()
    assert got[0] is None and got[1] is None


@pytest.mark.parametrize('kg,src', [
    (0.9, 'shopee'),      # source outside the enum
    (0.9, None),          # a weight nobody can vouch for
    (None, 'measured'),   # provenance for a weight that does not exist
    (0.0, 'measured'),    # a parcel cannot weigh nothing
    (-1.0, 'measured'),
])
def test_the_check_refuses_a_broken_pair(tmp_db_conn, kg, src):
    pid = _pid(tmp_db_conn)
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db_conn.execute(
            "UPDATE products SET weight_kg=?, weight_source=? WHERE id=?",
            (kg, src, pid))


def test_a_weight_change_leaves_an_audit_row(tmp_db_conn):
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=NULL, weight_source=NULL WHERE id=?", (pid,))
    tmp_db_conn.execute("DELETE FROM audit_log WHERE table_name='products' AND row_id=?",
                        (pid,))
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=?, weight_source=? WHERE id=?",
        (1.25, 'measured', pid))

    rows = tmp_db_conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='products' AND row_id=? AND action='UPDATE'", (pid,)).fetchall()
    assert len(rows) == 1, rows          # count first — an empty list proves nothing
    assert 'weight_kg' in rows[0][0]
    assert 'weight_source' in rows[0][0]


def test_inserting_a_weighed_product_audits_the_weight(tmp_db_conn):
    """All THREE products audit triggers enumerate their columns, so INSERT
    needed rewriting too — my review brief wrongly claimed it logged only the
    row id, and Codex caught it."""
    tmp_db_conn.execute(
        "INSERT INTO products (product_name, unit_type, weight_kg, weight_source) "
        "VALUES ('เทสต์น้ำหนัก', 'ตัว', 1.5, 'measured')")
    pid = tmp_db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    rows = tmp_db_conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='products' AND row_id=? AND action='INSERT'", (pid,)).fetchall()
    assert len(rows) == 1, rows
    assert '"weight_kg":1.5' in rows[0][0].replace(' ', '')
    assert 'measured' in rows[0][0]


def test_deleting_a_weighed_product_keeps_its_weight_on_the_record(tmp_db_conn):
    """Otherwise the last known weight and its provenance vanish with the row."""
    tmp_db_conn.execute(
        "INSERT INTO products (product_name, unit_type, weight_kg, weight_source) "
        "VALUES ('เทสต์น้ำหนักลบ', 'ตัว', 2.25, 'marketplace')")
    pid = tmp_db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    tmp_db_conn.execute("DELETE FROM audit_log WHERE table_name='products' AND row_id=?",
                        (pid,))
    tmp_db_conn.execute("DELETE FROM products WHERE id=?", (pid,))
    rows = tmp_db_conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='products' AND row_id=? AND action='DELETE'", (pid,)).fetchall()
    assert len(rows) == 1, rows
    assert '"weight_kg":2.25' in rows[0][0].replace(' ', '')
    assert 'marketplace' in rows[0][0]


def test_the_delete_payload_keeps_its_pre_existing_asymmetry(tmp_db_conn):
    """CONTROL against tidying: DELETE has never carried low_stock_threshold
    while INSERT does. Migration 159 reproduces that verbatim, and the rollback
    depends on it to come back byte-identical."""
    tmp_db_conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('เทสต์ asym', 'ตัว')")
    pid = tmp_db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # ⚠ table_name is load-bearing: audit_log row_ids are per-table, and
    # without it this read a commission_payouts row that shared the id.
    ins = tmp_db_conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='products' AND row_id=? AND action='INSERT'",
        (pid,)).fetchone()[0]
    tmp_db_conn.execute("DELETE FROM products WHERE id=?", (pid,))
    dele = tmp_db_conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='products' AND row_id=? AND action='DELETE'",
        (pid,)).fetchone()[0]
    assert 'low_stock_threshold' in ins
    assert 'low_stock_threshold' not in dele


def test_the_audit_trigger_still_logs_the_columns_it_always_did(tmp_db_conn):
    """CONTROL: 159 rewrites `audit_products_update` wholesale, so prove the
    pre-existing half survived rather than only testing the new half."""
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute("DELETE FROM audit_log WHERE table_name='products' AND row_id=?",
                        (pid,))
    old = tmp_db_conn.execute(
        "SELECT low_stock_threshold FROM products WHERE id=?", (pid,)).fetchone()[0]
    tmp_db_conn.execute("UPDATE products SET low_stock_threshold=? WHERE id=?",
                        ((old or 0) + 7, pid))
    rows = tmp_db_conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='products' AND row_id=?", (pid,)).fetchall()
    assert len(rows) == 1, rows
    assert 'low_stock_threshold' in rows[0][0]


# --------------------------------------------------------------------------
# the route — a 302 is not evidence, so every case asserts a positive change
# --------------------------------------------------------------------------

@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _form(**over):
    base = {'units_per_carton': '1', 'units_per_box': '1', 'unit_type': 'ตัว',
            'cost_price': '10', 'base_sell_price': '20',
            'low_stock_threshold': '10'}
    base.update(over)
    return base


def test_posting_a_weight_stores_it_as_measured(admin_client, tmp_db_conn):
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=NULL, weight_source=NULL WHERE id=?", (pid,))
    tmp_db_conn.commit()

    resp = admin_client.post(f'/products/{pid}/edit',
                             data=_form(weight_kg='0.775', low_stock_threshold='33'))
    assert resp.status_code == 302, resp.data[:400]

    got = tmp_db_conn.execute(
        "SELECT weight_kg, weight_source, low_stock_threshold FROM products WHERE id=?",
        (pid,)).fetchone()
    # the sentinel first: proves the route reached the model at all, rather
    # than bailing at a guard and redirecting with nothing written.
    assert got[2] == 33, "route never wrote — the weight assertions below would be vacuous"
    assert got[0] == 0.775
    assert got[1] == 'measured'


def test_a_post_without_the_weight_field_preserves_a_stored_weight(admin_client,
                                                                   tmp_db_conn):
    """The payroll-note failure, in its product-form shape."""
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=?, weight_source=? WHERE id=?",
        (2.5, 'measured', pid))
    tmp_db_conn.commit()

    resp = admin_client.post(f'/products/{pid}/edit',
                             data=_form(low_stock_threshold='44'))
    assert resp.status_code == 302, resp.data[:400]

    got = tmp_db_conn.execute(
        "SELECT weight_kg, weight_source, low_stock_threshold FROM products WHERE id=?",
        (pid,)).fetchone()
    assert got[2] == 44, "route never wrote — preservation below would be vacuous"
    assert got[0] == 2.5
    assert got[1] == 'measured'


def test_a_post_with_an_empty_weight_field_clears_it(admin_client, tmp_db_conn):
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=?, weight_source=? WHERE id=?",
        (2.5, 'measured', pid))
    tmp_db_conn.commit()

    resp = admin_client.post(f'/products/{pid}/edit',
                             data=_form(weight_kg='', low_stock_threshold='55'))
    assert resp.status_code == 302, resp.data[:400]

    got = tmp_db_conn.execute(
        "SELECT weight_kg, weight_source, low_stock_threshold FROM products WHERE id=?",
        (pid,)).fetchone()
    assert got[2] == 55, "route never wrote — the clear below would be vacuous"
    assert got[0] is None
    assert got[1] is None


def test_a_bad_weight_is_refused_without_touching_the_row(admin_client, tmp_db_conn):
    pid = _pid(tmp_db_conn)
    tmp_db_conn.execute(
        "UPDATE products SET weight_kg=?, weight_source=?, low_stock_threshold=? "
        "WHERE id=?", (2.5, 'measured', 11, pid))
    tmp_db_conn.commit()

    resp = admin_client.post(f'/products/{pid}/edit',
                             data=_form(weight_kg='-3', low_stock_threshold='66'))
    assert resp.status_code == 200, "expected the form re-render, not a redirect"

    got = tmp_db_conn.execute(
        "SELECT weight_kg, low_stock_threshold FROM products WHERE id=?",
        (pid,)).fetchone()
    assert got[0] == 2.5
    assert got[1] == 11, "the refusal must not have written the other fields either"


def test_the_edit_form_renders_the_weight_box(admin_client, tmp_db_conn):
    pid = _pid(tmp_db_conn)
    html = admin_client.get(f'/products/{pid}/edit').get_data(as_text=True)
    # assert on the ELEMENT, not a bare Thai substring that the page chrome
    # could already contain
    assert 'name="weight_kg"' in html


# --------------------------------------------------------------------------
# migration 159 round trip — rollback THEN forward, so this still works on a
# machine where 159 is already applied (the `pre134_conn` shape; a fixture that
# only ran the forward would die with "duplicate column name")
# --------------------------------------------------------------------------

_TRIGGERS = ('audit_products_insert', 'audit_products_delete', 'audit_products_update')


def _read(path):
    from pathlib import Path
    return Path(__file__).resolve().parents[1].joinpath(path).read_text()


def _bodies(conn):
    return {n: conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (n,)
    ).fetchone()[0] for n in _TRIGGERS}


@pytest.fixture
def pre159_conn(tmp_db_conn):
    """A connection whose DB has been rolled back to its pre-159 state."""
    tmp_db_conn.executescript(_read('data/migrations/159_product_parcel_weight.rollback.sql'))
    cols = {r[1] for r in tmp_db_conn.execute("PRAGMA table_info(products)")}
    assert 'weight_kg' not in cols, "rollback did not drop the column"
    return tmp_db_conn


def test_rollback_restores_all_three_triggers_byte_for_byte(pre159_conn):
    """The whole point of capturing the pre-159 bodies. If a future edit
    'tidies' the DELETE payload, this goes red — which is correct."""
    before = _bodies(pre159_conn)
    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.sql'))
    after_fwd = _bodies(pre159_conn)
    for n in _TRIGGERS:
        assert 'weight_kg' in after_fwd[n], f"{n} does not mention the new column"
        assert after_fwd[n] != before[n], f"{n} was not actually rewritten"

    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.rollback.sql'))
    restored = _bodies(pre159_conn)
    for n in _TRIGGERS:
        assert restored[n] == before[n], f"{n} did not come back byte-identical"


_PRE159_INSERT_KEYS = frozenset({
    'product_name', 'unit_type', 'cost_price', 'base_sell_price',
    'low_stock_threshold', 'is_active'})
_PRE159_DELETE_KEYS = _PRE159_INSERT_KEYS - {'low_stock_threshold'}
# The UPDATE trigger tracks four columns the other two do not.
_PRE159_UPDATE_FIELDS = _PRE159_INSERT_KEYS | {
    'units_per_carton', 'units_per_box', 'hard_to_sell'}


def _when_fields(body):
    """Columns in the UPDATE trigger's WHEN guard — what makes it FIRE."""
    import re
    m = re.search(r"WHEN\s*\((.*?)\)\s*BEGIN", body, re.S)
    assert m, "could not find the WHEN guard"
    return frozenset(re.findall(r"OLD\.([a-z_]+)\s+IS NOT NEW\.", m.group(1)))


def _update_payload_fields(body):
    """Columns in the UPDATE trigger's body — what it RECORDS once it fires.

    ⚠ Split from `_when_fields` deliberately. A single regex over the whole
    body returns the union of the two, so deleting a column from just one of
    them left the set unchanged and the assertion green — proved by mutation.
    The two lists genuinely can drift apart: a column in WHEN but not the body
    fires the trigger and logs nothing, and the reverse never logs at all.
    """
    import re
    after = body.split('BEGIN', 1)[1]
    return frozenset(re.findall(r"OLD\.([a-z_]+)\s+IS NOT NEW\.", after))


def _payload_keys(body):
    """The json_object keys a trigger writes, i.e. the quoted names followed by
    a column reference.

    `'products', NEW.id` — the audit_log table_name argument — matches the same
    shape, so it is excluded by name rather than by a cleverer regex.
    """
    import re
    return frozenset(re.findall(r"'([a-z_]+)',\s*(?:NEW|OLD)\.", body)) - {'products'}


def test_the_restored_triggers_match_the_pre159_field_lists(pre159_conn):
    """INDEPENDENT ORACLE for the byte-identity test below.

    That test compares the rollback's output against the rollback's own output,
    so it is self-consistent and blind to the rollback drifting from the real
    pre-159 definitions — proved by mutation: adding low_stock_threshold to the
    rollback's DELETE payload left it green. These literal key sets are the
    pre-159 state on the record, and they are what actually goes red.
    """
    bodies = _bodies(pre159_conn)
    assert _payload_keys(bodies['audit_products_insert']) == _PRE159_INSERT_KEYS
    assert _payload_keys(bodies['audit_products_delete']) == _PRE159_DELETE_KEYS
    # UPDATE needs its own literal too — pinning only INSERT/DELETE left a
    # legacy column deletable from the rollback's UPDATE trigger while the
    # byte-comparison stayed green, because that comparison takes both sides
    # from the same rollback definition. (Codex, second pass.)
    assert _when_fields(bodies['audit_products_update']) == _PRE159_UPDATE_FIELDS
    assert _update_payload_fields(bodies['audit_products_update']) == _PRE159_UPDATE_FIELDS
    for name, body in bodies.items():
        assert 'weight' not in body, f"{name} still mentions the 159 columns"


def test_the_forward_migration_adds_the_weight_keys_to_all_three(pre159_conn):
    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.sql'))
    bodies = _bodies(pre159_conn)
    assert _payload_keys(bodies['audit_products_insert']) == (
        _PRE159_INSERT_KEYS | {'weight_kg', 'weight_source'})
    assert _payload_keys(bodies['audit_products_delete']) == (
        _PRE159_DELETE_KEYS | {'weight_kg', 'weight_source'})
    expected = _PRE159_UPDATE_FIELDS | {'weight_kg', 'weight_source'}
    assert _when_fields(bodies['audit_products_update']) == expected
    assert _update_payload_fields(bodies['audit_products_update']) == expected


def test_forward_applies_again_after_a_rollback(pre159_conn):
    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.sql'))
    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.rollback.sql'))
    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.sql'))
    cols = {r[1] for r in pre159_conn.execute("PRAGMA table_info(products)")}
    assert {'weight_kg', 'weight_source'} <= cols


def test_the_rollback_preserves_rows_written_after_the_forward_ran(pre159_conn):
    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.sql'))
    pre159_conn.execute(
        "INSERT INTO products (product_name, unit_type, weight_kg, weight_source) "
        "VALUES ('หลังไมเกรชัน', 'ตัว', 3.0, 'measured')")
    pid = pre159_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    pre159_conn.executescript(_read('data/migrations/159_product_parcel_weight.rollback.sql'))
    row = pre159_conn.execute(
        "SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
    assert row is not None and row[0] == 'หลังไมเกรชัน'
