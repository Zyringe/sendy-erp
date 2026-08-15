"""cross_unit_hazard must never raise past its own boundary — Codex review
finding 3 (major, PR #388): component_product_id's ConversionRoleError on a
malformed multi-input [แพ็ค] formula (no roles, or roles that don't match the
role contract) used to propagate straight out of cross_unit_hazard. That
function has 6 call sites spanning WRITE paths (unit_conversions save/edit,
suggestion approval) and READ/LIST paths (the two /mapping and
/unit-conversions pending-lists) — a raise from a list-builder loop turns
one malformed formula into a 500 for the WHOLE page and aborts bulk work,
instead of blocking just the one risky mapping.

Fix: cross_unit_hazard catches ConversionRoleError and returns
{'kind': 'configuration_error', 'formula_id', 'message'} instead — still
failing closed (every WRITE caller must treat it as an unconditional block,
same as 'pair'), but as a VALUE the read/list callers can render as a
warning row. It also records a durable system_alerts row (dedupe-keyed on
the formula id) so the malformed formula surfaces on /alerts even from a
read path nobody is watching.

See tests/test_pair_form_packaging.py for the role-contract's OWN unit tests
(conversion_roles.validate_pack_inputs / component_product_id — those still
raise; this file is about the ONE caller that must not let it escape) and
tests/test_cross_unit_hazard_configuration_error_coverage.py for the sweep
that keeps every call site accounted for.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import models
from models.conversion_roles import ConversionRoleError


PACK, LOOSE, CARD = 960101, 960102, 960103


def _seed_product(conn, pid, name, unit="ตัว"):
    conn.execute("INSERT INTO products (id, product_name, unit_type) VALUES (?, ?, ?)", (pid, name, unit))


def _seed_malformed_pack_formula(conn):
    """A hand-corrupted active [แพ็ค] with 2 inputs and NO roles — the exact
    shape component_product_id refuses to guess at (pinned by
    test_cross_unit_hazard_fails_closed_on_roleless_multi_input in
    test_pair_form_packaging.py, which now asserts the VALUE this returns
    rather than an exception)."""
    _seed_product(conn, PACK, "hammer pack", "แผง")
    _seed_product(conn, LOOSE, "hammer loose", "อัน")
    _seed_product(conn, CARD, "blister card", "แผง")
    fid = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty) VALUES (?,?,?)",
        ("[แพ็ค] hammer pack ⟵ 1 อัน + blister card", PACK, 1)).lastrowid
    conn.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,NULL)",
                (fid, LOOSE, 1))
    conn.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,NULL)",
                (fid, CARD, 1))
    conn.commit()
    return fid


def _open_alerts(conn, kind):
    return conn.execute(
        "SELECT id, dedupe_key, context_json FROM system_alerts"
        " WHERE kind=? AND resolved_at IS NULL", (kind,)).fetchall()


# ── cross_unit_hazard itself: value, not exception ──────────────────────────

def test_returns_configuration_error_dict_not_raise(empty_db_conn):
    c = empty_db_conn
    fid = _seed_malformed_pack_formula(c)
    hz = models.cross_unit_hazard(c, PACK, 'อัน')                # must not raise
    assert hz is not None
    assert hz['kind'] == 'configuration_error'
    assert hz['formula_id'] == fid
    assert hz['message']                                          # operator-readable Thai text


def test_configuration_error_also_reachable_from_the_input_side(empty_db_conn):
    """The second cross_unit_hazard loop (product as an INPUT of a pack half)
    hits the same malformed formula from LOOSE's own side."""
    c = empty_db_conn
    fid = _seed_malformed_pack_formula(c)
    hz = models.cross_unit_hazard(c, LOOSE, 'แผง')
    assert hz is not None and hz['kind'] == 'configuration_error' and hz['formula_id'] == fid


def test_records_a_durable_system_alert(empty_db_conn):
    c = empty_db_conn
    fid = _seed_malformed_pack_formula(c)
    before = _open_alerts(c, 'conversion_role_error')
    assert before == []
    models.cross_unit_hazard(c, PACK, 'อัน')
    after = _open_alerts(c, 'conversion_role_error')
    assert len(after) == 1
    assert after[0]['dedupe_key'] == str(fid)


def test_repeated_calls_do_not_spam_the_alert(empty_db_conn):
    """A list-builder loop calls cross_unit_hazard once PER PENDING ROW —
    several rows can partner with the same malformed formula. Must stay ONE
    open alert, dedupe-keyed on the formula id alone."""
    c = empty_db_conn
    _seed_malformed_pack_formula(c)
    for _ in range(5):
        models.cross_unit_hazard(c, PACK, 'อัน')
        models.cross_unit_hazard(c, LOOSE, 'แผง')
    after = _open_alerts(c, 'conversion_role_error')
    assert len(after) == 1


def test_unaffected_products_still_get_a_clean_verdict(empty_db_conn):
    """Control: cross_unit_hazard on a product with NO pack/unpack formula at
    all is unaffected by any of this — same-unit is a clean None, and a
    pack-unit product with no pair still reports the (unrelated) pack_piece
    hazard exactly as before."""
    c = empty_db_conn
    _seed_product(c, 970001, "plain product", "ตัว")
    _seed_product(c, 970002, "lonepack", "แผง")
    c.commit()
    assert models.cross_unit_hazard(c, 970001, 'ตัว') is None       # same unit — no hazard at all
    hz = models.cross_unit_hazard(c, 970002, 'ชิ้น')                # piece bsn_unit on a pack-typed product, no pair
    assert hz is not None and hz['kind'] == 'pack_piece'


# ── write callers: unconditional block, regardless of the submitted ratio ──

def test_save_unit_conversions_blocks_unconditionally(empty_db_conn):
    c = empty_db_conn
    _seed_malformed_pack_formula(c)
    result = models.save_unit_conversions(
        [{'product_id': PACK, 'bsn_unit': 'อัน', 'ratio': 1}], )
    # save_unit_conversions opens its own connection — this test only proves
    # it does not raise and blocks; run against the LIVE empty_db path via
    # patched DATABASE_PATH (empty_db_conn's fixture already did that).
    assert result['saved'] == 0
    assert len(result['blocked']) == 1
    assert result['blocked'][0]['kind'] == 'configuration_error'
    row = c.execute(
        "SELECT COUNT(*) FROM unit_conversions WHERE product_id=? AND bsn_unit='อัน'", (PACK,)
    ).fetchone()
    assert row[0] == 0


def test_update_unit_conversion_ratio_blocks_unconditionally(empty_db_conn):
    c = empty_db_conn
    _seed_malformed_pack_formula(c)
    result = models.update_unit_conversion_ratio(PACK, 'อัน', 1)
    assert 'blocked' in result
    assert result['blocked']['kind'] == 'configuration_error'


def test_upsert_unit_conversion_blocks_unconditionally(empty_db_conn):
    c = empty_db_conn
    _seed_malformed_pack_formula(c)
    ok = models.upsert_unit_conversion(PACK, 'อัน', 1)
    assert ok is False
    row = c.execute(
        "SELECT COUNT(*) FROM unit_conversions WHERE product_id=? AND bsn_unit='อัน'", (PACK,)
    ).fetchone()
    assert row[0] == 0


# ── read/list callers: no 500, surfaced as a warning row ────────────────────

def _seed_pending_sale(conn, doc, pid, code, qty, unit):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(batch_id,date_iso,doc_no,doc_base,product_id,bsn_code,product_name_raw,"
        " customer,customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
        " synced_to_stock) VALUES ('t','2025-01-01',?,?,?,?,'raw','C','C1',?,?,10,0,0,0,0,0)",
        (doc, doc, pid, code, qty, unit))


def test_get_pending_unit_conversions_does_not_500(empty_db_conn):
    c = empty_db_conn
    c.execute("PRAGMA foreign_keys = OFF")     # batch_id='t' has no import_log row
    _seed_malformed_pack_formula(c)
    _seed_pending_sale(c, 'IV-CFG-1', PACK, 'PACKCODE1', 2, 'อัน')
    c.commit()
    pending = models.get_pending_unit_conversions()               # must not raise
    row = next(r for r in pending if r['product_id'] == PACK and r['bsn_unit'] == 'อัน')
    assert row['hazard']['kind'] == 'configuration_error'


def test_get_pending_split_mappings_does_not_500(empty_db_conn):
    c = empty_db_conn
    c.execute("PRAGMA foreign_keys = OFF")     # batch_id='t' has no import_log row
    _seed_malformed_pack_formula(c)
    _seed_pending_sale(c, 'IV-CFG-2', PACK, 'PACKCODE2', 2, 'อัน')
    c.commit()
    pending = models.get_pending_split_mappings()                 # must not raise
    row = next(r for r in pending if r['product_id'] == PACK and r['bsn_unit'] == 'อัน')
    assert row['hazard']['kind'] == 'configuration_error'


# ── route level: no 500, flash renders cleanly ──────────────────────────────

@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def test_route_unit_conversions_page_renders_with_configuration_error_row(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _seed_malformed_pack_formula(conn)
    _seed_pending_sale(conn, 'IV-CFG-3', PACK, 'PACKCODE3', 2, 'อัน')
    conn.commit()
    conn.close()

    resp = admin_client.get('/unit-conversions')
    assert resp.status_code == 200, resp.data[:500]


def test_route_unit_conversions_save_blocked_flash_does_not_500(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _seed_malformed_pack_formula(conn)
    _seed_pending_sale(conn, 'IV-CFG-4', PACK, 'PACKCODE4', 2, 'อัน')
    conn.commit()
    conn.close()

    resp = admin_client.post('/unit-conversions/save', data={
        f'ratio_{PACK}_อัน': '1',
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.data[:500]                # _flash_unit_hazard must not KeyError
