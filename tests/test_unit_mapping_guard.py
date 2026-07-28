"""Pin the cross-unit ratio guard + split-mapping flow (decisions/log.md
2026-07-26/27 pack/loose arc): a bill in a piece unit must never deduct a
pack-stocked SKU fractionally via unit_conversions, and a product with an
active [แพ็ค]/[แกะ] pair must never take a cross-unit ratio in either
direction — the fix for those rows is a per-unit split mapping
(/mapping/split-save), not a ratio.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import models

PACK = 950001      # unit_type แผง, has active [แพ็ค]/[แกะ] pair with LOOSE
LOOSE = 950002     # unit_type ตัว, pair partner of PACK
PLAIN = 950003     # unit_type ตัว, no pair
LONEPACK = 950004  # unit_type แผง, no pair


def _seed(conn):
    # First statement on the connection: batch_id references import_log,
    # which _sale()'s seeded rows never populate — must be OFF before ANY
    # other write, since sqlite ignores this PRAGMA once a transaction is
    # already open (see tests/test_apply_stock_and_mapping.py's _seed).
    conn.execute("PRAGMA foreign_keys = OFF")
    for pid, name, unit in (
        (PACK, 'PACK', 'แผง'),
        (LOOSE, 'LOOSE', 'ตัว'),
        (PLAIN, 'PLAIN', 'ตัว'),
        (LONEPACK, 'LONEPACK', 'แผง'),
    ):
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, sku_code, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (pid, name, unit, f"SKU-{pid}"))
    conn.commit()
    models.upsert_pack_unpack_pair(PACK, LOOSE, 2, 'both')


def _sale(conn, doc, pid, code, qty, unit):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(batch_id,date_iso,doc_no,doc_base,product_id,bsn_code,product_name_raw,"
        " customer,customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
        " synced_to_stock) VALUES ('t','2025-01-01',?,?,?,?,'raw','C','C1',?,?,10,0,0,0,0,0)",
        (doc, doc, pid, code, qty, unit))


@pytest.fixture
def admin_client(empty_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 't'
        sess['role'] = 'admin'
    return c


@pytest.fixture
def staff_client(empty_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 2
        sess['username'] = 's'
        sess['role'] = 'staff'
    return c


# ── cross_unit_hazard ─────────────────────────────────────────────────────────

def test_hazard_pair_pack_side(empty_db_conn):
    _seed(empty_db_conn)
    hz = models.cross_unit_hazard(empty_db_conn, PACK, 'ตัว')
    assert hz is not None
    assert hz['kind'] == 'pair'
    assert hz['partner_id'] == LOOSE


def test_hazard_pair_loose_side(empty_db_conn):
    _seed(empty_db_conn)
    hz = models.cross_unit_hazard(empty_db_conn, LOOSE, 'แผง')
    assert hz is not None
    assert hz['kind'] == 'pair'
    assert hz['partner_id'] == PACK


def test_hazard_pack_piece_without_pair(empty_db_conn):
    _seed(empty_db_conn)
    hz = models.cross_unit_hazard(empty_db_conn, LONEPACK, 'ตัว')
    assert hz is not None
    assert hz['kind'] == 'pack_piece'


def test_no_hazard_same_unit_or_bulk(empty_db_conn):
    _seed(empty_db_conn)
    assert models.cross_unit_hazard(empty_db_conn, PACK, 'แผง') is None
    assert models.cross_unit_hazard(empty_db_conn, PLAIN, 'โหล') is None
    assert models.cross_unit_hazard(empty_db_conn, LONEPACK, 'กิโลกรัม') is None


def test_hazard_acronym_normalized(empty_db_conn):
    import bsn_units
    if bsn_units.normalize_unit('ตว') != 'ตัว':
        pytest.skip('ตว is not mapped to ตัว in bsn_unit_full.json on this checkout')
    _seed(empty_db_conn)
    hz = models.cross_unit_hazard(empty_db_conn, PACK, 'ตว')
    assert hz is not None
    assert hz['kind'] == 'pair'


# ── save_unit_conversions / update_unit_conversion_ratio / upsert_unit_conversion ──

def test_save_blocks_pair_and_fractional_pack_piece(empty_db_conn):
    _seed(empty_db_conn)
    result = models.save_unit_conversions([
        {'product_id': PACK, 'bsn_unit': 'ตัว', 'ratio': 0.5},
        {'product_id': LONEPACK, 'bsn_unit': 'ตัว', 'ratio': 0.5},
        {'product_id': LONEPACK, 'bsn_unit': 'ตัว', 'ratio': 1},
        {'product_id': PLAIN, 'bsn_unit': 'โหล', 'ratio': 12},
    ])
    assert result['saved'] == 2
    blocked_pairs = {(b['product_id'], b['bsn_unit']) for b in result['blocked']}
    assert blocked_pairs == {(PACK, 'ตัว'), (LONEPACK, 'ตัว')}

    rows = empty_db_conn.execute(
        "SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY product_id"
    ).fetchall()
    saved_pairs = {(r['product_id'], r['bsn_unit']): r['ratio'] for r in rows}
    assert saved_pairs == {(LONEPACK, 'ตัว'): 1.0, (PLAIN, 'โหล'): 12.0}


def test_update_ratio_blocked(empty_db_conn):
    _seed(empty_db_conn)
    # A real ledger row for PLAIN/โหล is required here: update_unit_conversion_ratio's
    # stock_levels rebuild does `SELECT product_id, SUM(...) FROM transactions
    # WHERE product_id=?` with no GROUP BY — with zero matching rows sqlite
    # returns one row with product_id=NULL (a pre-existing landmine, unrelated
    # to this guard), which then fails the stock_levels FK. Seeding a synced
    # sale gives it a real transactions row to rebuild from.
    _sale(empty_db_conn, 'IVPLAIN', PLAIN, 'CPLAIN', 1, 'โหล')
    empty_db_conn.commit()
    models.save_unit_conversions([{'product_id': PLAIN, 'bsn_unit': 'โหล', 'ratio': 12}])

    blocked_result = models.update_unit_conversion_ratio(PACK, 'ตัว', 0.5)
    assert 'blocked' in blocked_result
    row = empty_db_conn.execute(
        "SELECT 1 FROM unit_conversions WHERE product_id=? AND bsn_unit=?", (PACK, 'ตัว')
    ).fetchone()
    assert row is None

    ok_result = models.update_unit_conversion_ratio(PLAIN, 'โหล', 6)
    assert ok_result == {'ok': True}
    row = empty_db_conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?", (PLAIN, 'โหล')
    ).fetchone()
    assert row['ratio'] == 6


def test_upsert_unit_conversion_blocked(empty_db_conn):
    _seed(empty_db_conn)
    assert models.upsert_unit_conversion(PACK, 'ตัว', 1) is False
    row = empty_db_conn.execute(
        "SELECT 1 FROM unit_conversions WHERE product_id=? AND bsn_unit=?", (PACK, 'ตัว')
    ).fetchone()
    assert row is None

    assert models.upsert_unit_conversion(PLAIN, 'โหล', 12) is True
    row = empty_db_conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?", (PLAIN, 'โหล')
    ).fetchone()
    assert row['ratio'] == 12


# ── pending lists ──────────────────────────────────────────────────────────────

def test_pending_annotated_with_hazard(empty_db_conn):
    _seed(empty_db_conn)
    _sale(empty_db_conn, 'IV1', PACK, 'C001', 3, 'ตัว')
    empty_db_conn.commit()

    pending = models.get_pending_unit_conversions()
    row = next(p for p in pending if p['product_id'] == PACK and p['bsn_unit'] == 'ตัว')
    assert row['hazard'] is not None
    assert row['hazard']['kind'] == 'pair'


def test_pending_split_mappings_listed(empty_db_conn):
    _seed(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
        "VALUES (?, ?, ?, '')", ('C001', 'raw name', PACK))
    _sale(empty_db_conn, 'IV1', PACK, 'C001', 2, 'ตัว')
    _sale(empty_db_conn, 'IV2', PACK, 'C001', 2, 'ตัว')
    _sale(empty_db_conn, 'IV3', PACK, 'C001', 5, 'แผง')  # control: same-unit, no hazard
    empty_db_conn.commit()

    splits = models.get_pending_split_mappings()
    assert len(splits) == 1
    row = splits[0]
    assert row['bsn_code'] == 'C001'
    assert row['bsn_unit'] == 'ตัว'
    assert row['product_id'] == PACK
    assert row['row_count'] == 2
    assert row['hazard']['partner_id'] == LOOSE


# ── /mapping/split-save route ───────────────────────────────────────────────────

def test_split_save_route_repoints(admin_client, empty_db_conn):
    _seed(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
        "VALUES (?, ?, ?, '')", ('C777', 'raw', PACK))
    empty_db_conn.execute(
        "INSERT INTO stock_levels (product_id, quantity) VALUES (?, 10)", (PACK,))
    _sale(empty_db_conn, 'IV777', PACK, 'C777', 2, 'ตัว')
    empty_db_conn.commit()

    resp = admin_client.post('/mapping/split-save', data={
        'bsn_code': 'C777', 'bsn_unit': 'ตัว', 'product_id': str(LOOSE),
    })
    assert resp.status_code == 302

    row = empty_db_conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=? AND bsn_unit=?",
        ('C777', 'ตัว')).fetchone()
    assert row is not None
    assert row['product_id'] == LOOSE

    sale = empty_db_conn.execute(
        "SELECT product_id, synced_to_stock FROM sales_transactions WHERE doc_no='IV777'"
    ).fetchone()
    assert sale['product_id'] == LOOSE
    assert sale['synced_to_stock'] == 1

    def _stock(pid):
        r = empty_db_conn.execute(
            "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
        return r['quantity'] if r else None

    assert _stock(LOOSE) == -2
    assert _stock(PACK) == 10


def test_split_save_route_staff_forbidden(staff_client, empty_db_conn):
    _seed(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
        "VALUES (?, ?, ?, '')", ('C778', 'raw', PACK))
    _sale(empty_db_conn, 'IV778', PACK, 'C778', 2, 'ตัว')
    empty_db_conn.commit()

    resp = staff_client.post('/mapping/split-save', data={
        'bsn_code': 'C778', 'bsn_unit': 'ตัว', 'product_id': str(LOOSE),
    })
    # access_control's whitelist gate (before_request) redirects staff away
    # from a manager-only endpoint (302) BEFORE the route's own inline
    # abort(403) defense-in-depth check ever runs — same idiom already
    # accepted in tests/test_route_hygiene_b567.py for the same reason.
    assert resp.status_code in (302, 403)

    sale = empty_db_conn.execute(
        "SELECT product_id FROM sales_transactions WHERE doc_no='IV778'"
    ).fetchone()
    assert sale['product_id'] == PACK  # untouched — staff must not repoint


# ── template rendering ──────────────────────────────────────────────────────────

def test_unit_conversions_page_renders_block(admin_client, empty_db_conn):
    _seed(empty_db_conn)
    _sale(empty_db_conn, 'IV1', PACK, 'C001', 2, 'ตัว')
    _sale(empty_db_conn, 'IV2', LONEPACK, 'C002', 3, 'ตัว')
    empty_db_conn.commit()

    resp = admin_client.get('/unit-conversions')
    html = resp.get_data(as_text=True)

    assert 'name="ratio_950001_ตัว"' not in html
    assert 'แยก mapping ตามหน่วย' in html

    assert 'name="ratio_950004_ตัว"' in html
    idx = html.index('name="ratio_950004_ตัว"')
    tag_end = html.index('>', idx)
    assert 'readonly' in html[idx:tag_end]


def test_mapping_page_renders_split_section(admin_client, empty_db_conn):
    _seed(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
        "VALUES (?, ?, ?, '')", ('C001', 'raw', PACK))
    _sale(empty_db_conn, 'IV1', PACK, 'C001', 2, 'ตัว')
    _sale(empty_db_conn, 'IV2', PACK, 'C001', 2, 'ตัว')
    empty_db_conn.commit()

    resp = admin_client.get('/mapping')
    html = resp.get_data(as_text=True)
    assert 'split-section' in html
    assert 'C001' in html


# ── Phase F: approve_pending_suggestion's own unit_conversion write ────────────
# (4th write path found by /scrutinize 2026-07-29 — bypassed cross_unit_hazard)

@pytest.fixture
def suggestion_uid(empty_db_conn):
    """pending_product_suggestions.suggested_by_user_id/reviewed_by_user_id
    are real FKs to users(id) — insert one real user row to satisfy them."""
    empty_db_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, is_active) "
        "VALUES ('phasef-tester', 'x', 'Tester', 'admin', 1)")
    empty_db_conn.commit()
    return empty_db_conn.execute(
        "SELECT id FROM users WHERE username='phasef-tester'").fetchone()[0]


def _suggestion_payload(bsn_code, *, suggested_unit_type='ตัว', bsn_unit=None, ratio=None):
    return {
        'bsn_code': bsn_code,
        'bsn_name': f'raw {bsn_code}',
        'suggested_name': f'product for {bsn_code}',
        'category': None, 'series': None, 'brand_id': None,
        'model': None, 'size': None, 'color_th': None, 'color_code': None,
        'packaging': None, 'condition': None, 'pack_variant': None,
        'suggested_cost': 10.0,
        'suggested_unit_type': suggested_unit_type,
        'units_per_carton': None, 'units_per_box': None,
        'bsn_unit': bsn_unit,
        'unit_conversion_ratio': ratio,
    }


def test_approve_suggestion_blocks_pack_piece_ratio(empty_db_conn, suggestion_uid):
    """A manager staging a NEW แผง SKU from a code whose bills arrive in ตัว,
    typing ratio 0.5 at stage time, must NOT get a unit_conversions row —
    that recreates the pack/loose bug through the approve door."""
    sid = models.save_pending_suggestion(
        _suggestion_payload('SUGF01', suggested_unit_type='แผง', bsn_unit='ตัว', ratio=0.5),
        user_id=suggestion_uid)
    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=suggestion_uid)

    row = empty_db_conn.execute(
        "SELECT 1 FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
        (new_pid, 'ตัว')).fetchone()
    assert row is None


def test_approve_suggestion_writes_safe_ratio(empty_db_conn, suggestion_uid):
    """Positive control: a real bulk-unit ratio (โหล=12 on a ตัว product) has
    no hazard and must still be written by approve."""
    sid = models.save_pending_suggestion(
        _suggestion_payload('SUGF02', suggested_unit_type='ตัว', bsn_unit='โหล', ratio=12),
        user_id=suggestion_uid)
    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=suggestion_uid)

    row = empty_db_conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
        (new_pid, 'โหล')).fetchone()
    assert row is not None
    assert row['ratio'] == 12
