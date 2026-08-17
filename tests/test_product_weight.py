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
