"""PR3 (mapping-suggest-clone) — "สร้างเลย" (create_now action on
/mapping/save) + the sku_code duplicate guard + the create_now concurrency
guard.

projects/mapping-suggest-clone/plan.md, "PR3 — clone", items 5-6.

Covers:
  - role gate: staff refused 403 (admin/manager/shareholder only — Q7)
  - end-to-end: one request creates the product, maps the bsn_code, marks
    the staged suggestion approved, and returns the new product id
  - duplicate guard: 409 naming an INACTIVE colliding product (Q8 — the
    guard is unfiltered on is_active, on purpose), then success once the
    client confirms
  - concurrency: two create_now calls for the SAME bsn_code must not let one
    approve the other's staged payload (the race the discarded-sid bug used
    to open) — injected at the real seam via monkeypatch, not threads, per
    erp-engineering-discipline.md's check-then-write testing guidance.

⚠ tests/conftest.py::tmp_db clones the LIVE dev DB *with its data*.
pending_product_suggestions.bsn_code and products.sku_code are both UNIQUE —
every fixture deletes its own bsn_code's rows first (mirrors
test_mapping_subcategory.py's _clean_row).
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

# Stable seed row already validated live (test_mapping_subcategory.py):
# category id 6 = ค้อน, short_code HMR.
_CAT_ID = 6
_CAT_SHORT = 'HMR'

_BSN_CREATE_NOW = 'ZZTEST-CREATENOW-01'
_BSN_DUP = 'ZZTEST-DUPGUARD-01'
_BSN_RACE = 'ZZTEST-RACE-01'


def _clean(conn, bsn_code):
    """Force our own state: wipe any prior test-run leftovers keyed on this
    bsn_code (pending_product_suggestions.bsn_code is UNIQUE) and any
    product/mapping row a prior create_now/approve created."""
    row = conn.execute(
        "SELECT approved_product_id FROM pending_product_suggestions WHERE bsn_code=?",
        (bsn_code,),
    ).fetchone()
    if row and row[0]:
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (row[0],))
        conn.execute("DELETE FROM products WHERE id=?", (row[0],))
    conn.execute("DELETE FROM pending_product_suggestions WHERE bsn_code=?", (bsn_code,))
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (bsn_code,))
    conn.commit()


def _fields(bsn_code, **overrides):
    f = {
        'category_id': _CAT_ID,
        'sub_category': 'ทดสอบ',
        'sub_category_short_code': 'ZTST',
        'series': None,
        'brand_id': None,
        'model': 'ZZCLONE',
        'size': '99mm',
        'color_th': None,
        'color_code': None,
        'packaging': None,
        'condition': None,
        'pack_variant': None,
        'suggested_cost': 0.0,
        'suggested_unit_type': 'ตัว',
        'units_per_carton': None,
        'units_per_box': None,
        'brand_other_name': None,
        'color_code_other': None,
        'packaging_other': None,
        'bsn_unit': None,
        'unit_conversion_ratio': None,
        'clone_source_pid': None,
        'bsn_code': bsn_code,
        'bsn_name': f'raw {bsn_code}',
        'suggested_name': f'สินค้าทดสอบ {bsn_code}',
        'category': 'ทดสอบ',
    }
    f.update(overrides)
    return f


@pytest.fixture
def manager_client(tmp_db):
    conn = sqlite3.connect(tmp_db)
    for code in (_BSN_CREATE_NOW, _BSN_DUP, _BSN_RACE):
        _clean(conn, code)
    conn.close()

    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c, tmp_db


@pytest.fixture
def staff_client(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _clean(conn, _BSN_CREATE_NOW)
    conn.close()

    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-staff'
        sess['role'] = 'staff'
    return c


# ── role gate ────────────────────────────────────────────────────────────────

def test_create_now_staff_forbidden(staff_client):
    resp = staff_client.post('/mapping/save', json={'mappings': [
        dict(_fields(_BSN_CREATE_NOW), action='create_now')
    ]})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body['ok'] is False


# ── end to end ───────────────────────────────────────────────────────────────

def test_create_now_creates_maps_and_returns_pid(manager_client):
    client, db_path = manager_client
    resp = client.post('/mapping/save', json={'mappings': [
        dict(_fields(_BSN_CREATE_NOW), action='create_now')
    ]})
    assert resp.status_code == 200, resp.data[:500]
    body = resp.get_json()
    assert body['ok'] is True
    new_pid = body['product_id']
    assert isinstance(new_pid, int)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prod = conn.execute(
        "SELECT product_name, sku_code, category_id FROM products WHERE id=?",
        (new_pid,)
    ).fetchone()
    assert prod is not None
    assert prod['category_id'] == _CAT_ID
    assert prod['sku_code'].startswith(_CAT_SHORT)

    mapping = conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=? AND bsn_unit=''",
        (_BSN_CREATE_NOW,)
    ).fetchone()
    assert mapping is not None, "create_now must map the bsn_code to the new product"
    assert mapping['product_id'] == new_pid

    sug = conn.execute(
        "SELECT status, approved_product_id FROM pending_product_suggestions WHERE bsn_code=?",
        (_BSN_CREATE_NOW,)
    ).fetchone()
    assert sug['status'] == 'approved'
    assert sug['approved_product_id'] == new_pid
    conn.close()


# ── duplicate guard ──────────────────────────────────────────────────────────

def test_create_now_duplicate_guard_inactive_then_confirm(manager_client):
    client, db_path = manager_client
    fields = _fields(_BSN_DUP)

    # Learn the sku_code OUR OWN guard will compute — same function the
    # route uses, so this is not an independent oracle, it's pinning the
    # CONTRACT (the value the guard checks against products.sku_code).
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    import sku_code_utils
    proposed = sku_code_utils.preview_sku_code(conn, fields)
    assert proposed, "fields should build a real (non-fallback) sku_code"

    conn.execute(
        "INSERT INTO products (product_name, sku_code, is_active, unit_type) "
        "VALUES (?, ?, 0, 'ตัว')",
        ('สินค้าเก่า ปิดใช้งานแล้ว', proposed),
    )
    conn.commit()
    dup_pid = conn.execute(
        "SELECT id FROM products WHERE sku_code=?", (proposed,)
    ).fetchone()['id']
    conn.close()

    resp = client.post('/mapping/save', json={'mappings': [
        dict(fields, action='create_now')
    ]})
    assert resp.status_code == 409, resp.data[:500]
    body = resp.get_json()
    assert body['ok'] is False
    assert body['duplicate_of']['id'] == dup_pid
    assert body['duplicate_of']['sku_code'] == proposed
    assert body['duplicate_of']['is_active'] == 0

    # The refused attempt must not have staged anything.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    n_staged = conn.execute(
        "SELECT COUNT(*) c FROM pending_product_suggestions WHERE bsn_code=?",
        (_BSN_DUP,)
    ).fetchone()['c']
    conn.close()
    assert n_staged == 0

    # Retry with the client's confirmed flag → succeeds, takes the -<id>
    # collision suffix (never a hard block — decision Q8).
    resp2 = client.post('/mapping/save', json={'mappings': [
        dict(fields, action='create_now', confirm_duplicate=True)
    ]})
    assert resp2.status_code == 200, resp2.data[:500]
    body2 = resp2.get_json()
    assert body2['ok'] is True
    new_pid = body2['product_id']
    assert new_pid != dup_pid

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    new_row = conn.execute(
        "SELECT sku_code FROM products WHERE id=?", (new_pid,)
    ).fetchone()
    conn.close()
    assert new_row['sku_code'] == f"{proposed}-{new_pid}"


# ── concurrency ──────────────────────────────────────────────────────────────

def test_create_now_concurrent_second_call_refused(manager_client, monkeypatch):
    """Two create_now calls for the SAME bsn_code, interleaved at the real
    seam (between A's own save and A's own approve) via monkeypatch — not
    threads, per erp-engineering-discipline.md's check-then-write guidance.
    B must be refused (SuggestionAlreadyStagedError), and the product that
    ends up created must be A's payload, never B's."""
    _client, db_path = manager_client
    import models.suggestions as sugg_mod

    payload_a = _fields(_BSN_RACE, suggested_name='สินค้า A (ควรชนะ)')
    payload_b = _fields(_BSN_RACE, suggested_name='สินค้า B (ต้องโดนปฏิเสธ)')

    real_approve = sugg_mod.approve_pending_suggestion
    captured = {}

    def fake_approve(sid, edits, reviewer_id):
        # Simulate B's create_now arriving while A's request is between its
        # own save and its own approve — exactly the window the discarded-
        # sid bug used to leave open.
        try:
            sugg_mod.create_now(payload_b, user_id=1)
            captured['b_succeeded'] = True
        except sugg_mod.SuggestionAlreadyStagedError as e:
            captured['b_error'] = e
        return real_approve(sid, edits, reviewer_id)

    monkeypatch.setattr(sugg_mod, 'approve_pending_suggestion', fake_approve)

    new_pid_a = sugg_mod.create_now(payload_a, user_id=1)

    assert 'b_succeeded' not in captured, \
        "B must NOT be allowed to approve alongside A for the same bsn_code"
    assert 'b_error' in captured, f"B should have been refused; captured={captured}"
    assert captured['b_error'].bsn_code == _BSN_RACE

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prod = conn.execute(
        "SELECT product_name FROM products WHERE id=?", (new_pid_a,)
    ).fetchone()
    n_products = conn.execute(
        "SELECT COUNT(*) c FROM pending_product_suggestions WHERE bsn_code=?",
        (_BSN_RACE,)
    ).fetchone()['c']
    conn.close()
    assert prod['product_name'] == payload_a['suggested_name'], \
        "the created product must be A's payload, not B's"
    # COUNT first (vacuity guard): exactly one suggestion row, not two.
    assert n_products == 1
