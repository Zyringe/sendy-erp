"""Phase 2 — product-label (ป้ายสินค้า) admin manage/edit UI.

Covers the `labels.*` blueprint: /labels/manage list+filter, /labels/<id>/edit,
/labels/bulk-size, /labels/company-block, admin-only gating, and the nav pin
(desktop sidebar + mobile drawer + _ENDPOINT_MODULE — see erp-engineering-
discipline "A navigable page lives in THREE nav surfaces").

Runs against `tmp_db` (a copy of the live DB), which already carries Phase 1's
imported product_labels (1,104 rows, ~9 flagged) + the seeded label_company_block
row — no fixture data needed. Expected counts are re-derived by direct SQL
query on tmp_db per test, never hardcoded, so the assertions track real DB
state instead of a magic number (see verification-discipline).

Python 3.9 — Optional[...] not X | None.
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')


# ── helpers ──────────────────────────────────────────────────────────────────

def _client(role, user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _one_label_id(db):
    return _conn(db).execute(
        "SELECT id FROM product_labels WHERE is_active = 1 ORDER BY id LIMIT 1"
    ).fetchone()['id']


def _flagged_ids(db):
    return [r['id'] for r in _conn(db).execute(
        "SELECT id FROM product_labels WHERE is_active = 1 AND needs_review = 1"
    ).fetchall()]


# ── /labels/manage — list/search/filter, admin gate ──────────────────────────

def test_manage_admin_ok(tmp_db):
    r = _client('admin').get('/labels/manage')
    assert r.status_code == 200


def test_manage_staff_forbidden(tmp_db):
    r = _client('staff').get('/labels/manage')
    assert r.status_code == 403


def test_manage_general_redirected(tmp_db):
    # general is mobile-kiosk-only; require_login redirects before the route's
    # own _require_admin() ever runs.
    r = _client('general').get('/labels/manage')
    assert r.status_code == 302


def test_manage_needs_review_filter_count_matches_db(tmp_db):
    expected = len(_flagged_ids(tmp_db))
    assert expected > 0, "fixture DB should carry Phase-1 flagged rows"
    html = _client('admin').get('/labels/manage?review=flagged').get_data(as_text=True)
    assert f'พบ {expected} รายการ' in html


def test_manage_search_by_barcode(tmp_db):
    row = _conn(tmp_db).execute(
        "SELECT barcode FROM product_labels WHERE barcode IS NOT NULL AND barcode <> '' "
        "AND is_active = 1 LIMIT 1"
    ).fetchone()
    html = _client('admin').get(f'/labels/manage?q={row["barcode"]}').get_data(as_text=True)
    assert row['barcode'] in html


# ── /labels/<id>/edit ──────────────────────────────────────────────────────

def test_edit_get_admin_ok(tmp_db):
    lid = _one_label_id(tmp_db)
    r = _client('admin').get(f'/labels/{lid}/edit')
    assert r.status_code == 200


def test_edit_staff_forbidden(tmp_db):
    lid = _one_label_id(tmp_db)
    assert _client('staff').get(f'/labels/{lid}/edit').status_code == 403
    # POST is blocked earlier, by require_login()'s per-role POST whitelist
    # (redirect+flash), before the route's own _require_admin() ever runs —
    # same convention as tests/test_hr_phase7.py::test_staff_cannot_reach_advances.
    assert _client('staff').post(f'/labels/{lid}/edit', data={}).status_code in (302, 403)


def test_edit_post_updates_row_and_clears_needs_review(tmp_db):
    lid = _flagged_ids(tmp_db)[0]
    before = _conn(tmp_db).execute(
        "SELECT product_name, updated_at, needs_review FROM product_labels WHERE id=?", (lid,)
    ).fetchone()
    assert before['needs_review'] == 1

    r = _client('admin').post(f'/labels/{lid}/edit', data={
        'product_name': 'ชื่อทดสอบแก้ไข',
        'brand': 'SENDAI',
        'barcode': '8857069999999',
        'usage_th': '', 'warning_th': '', 'packaging_th': '', 'size_th': '',
        'label_size': 'small',
        # needs_review checkbox omitted => unchecked => clears the flag
        'review_note': '',
    })
    assert r.status_code == 302

    after = _conn(tmp_db).execute(
        "SELECT product_name, barcode, label_size, needs_review, updated_at, review_note "
        "FROM product_labels WHERE id=?", (lid,)
    ).fetchone()
    assert after['product_name'] == 'ชื่อทดสอบแก้ไข'
    assert after['barcode'] == '8857069999999'
    assert after['label_size'] == 'small'
    assert after['needs_review'] == 0
    assert after['review_note'] is None
    assert after['updated_at'] != before['updated_at']


def test_edit_get_unknown_id_404(tmp_db):
    r = _client('admin').get('/labels/999999/edit')
    assert r.status_code == 404


# ── /labels/bulk-size ─────────────────────────────────────────────────────

def test_bulk_size_selected_only_affects_chosen_ids(tmp_db):
    conn = _conn(tmp_db)
    ids = [r['id'] for r in conn.execute(
        "SELECT id FROM product_labels WHERE is_active=1 AND label_size='big' LIMIT 2"
    ).fetchall()]
    assert len(ids) == 2
    other_id = conn.execute(
        "SELECT id FROM product_labels WHERE is_active=1 AND label_size='big' "
        "AND id NOT IN (?,?) LIMIT 1", ids
    ).fetchone()['id']

    r = _client('admin').post('/labels/bulk-size', data={
        'label_size': 'small', 'scope': 'selected',
        'label_ids': [str(i) for i in ids],
        'q': '', 'review': '', 'page': '1',
    })
    assert r.status_code == 302

    conn2 = _conn(tmp_db)
    for i in ids:
        assert conn2.execute(
            "SELECT label_size FROM product_labels WHERE id=?", (i,)
        ).fetchone()['label_size'] == 'small'
    assert conn2.execute(
        "SELECT label_size FROM product_labels WHERE id=?", (other_id,)
    ).fetchone()['label_size'] == 'big'


def test_bulk_size_filtered_applies_to_all_matching(tmp_db):
    flagged = _flagged_ids(tmp_db)
    r = _client('admin').post('/labels/bulk-size', data={
        'label_size': 'small', 'scope': 'filtered',
        'q': '', 'review': 'flagged', 'page': '1',
    })
    assert r.status_code == 302

    conn = _conn(tmp_db)
    for i in flagged:
        assert conn.execute(
            "SELECT label_size FROM product_labels WHERE id=?", (i,)
        ).fetchone()['label_size'] == 'small'
    # an unrelated (non-flagged) row must be untouched
    other = conn.execute(
        "SELECT label_size FROM product_labels WHERE is_active=1 AND needs_review=0 LIMIT 1"
    ).fetchone()
    assert other['label_size'] == 'big'


def test_bulk_size_staff_forbidden(tmp_db):
    # POST is blocked earlier by require_login()'s per-role POST whitelist (redirect).
    r = _client('staff').post('/labels/bulk-size', data={'label_size': 'small', 'scope': 'filtered'})
    assert r.status_code in (302, 403)


# ── /labels/company-block ────────────────────────────────────────────────

def test_company_block_get_admin_ok(tmp_db):
    assert _client('admin').get('/labels/company-block').status_code == 200


def test_company_block_staff_forbidden(tmp_db):
    assert _client('staff').get('/labels/company-block').status_code == 403


def test_company_block_post_updates_row(tmp_db):
    before = _conn(tmp_db).execute("SELECT updated_at FROM label_company_block LIMIT 1").fetchone()
    r = _client('admin').post('/labels/company-block', data={
        'distributor_th': 'บริษัท บุญสวัสดิ์นำชัย จำกัด (แก้ไข)',
        'importer_th': 'นำเข้าโดย : บริษัท เซ็นได เทรดดิ้ง จำกัด',
        'address_th': 'ที่อยู่ทดสอบ',
        'importer_addr1_th': '', 'importer_addr2_th': '',
        'country_th': 'ประเทศที่ผลิต: ประเทศ PRC',
        'quality_th': '',
        'price_line_th': 'ราคา : ตรวจสอบ ณ จุดขาย',
    })
    assert r.status_code == 302
    after = _conn(tmp_db).execute(
        "SELECT distributor_th, importer_th, updated_at FROM label_company_block LIMIT 1"
    ).fetchone()
    assert after['distributor_th'] == 'บริษัท บุญสวัสดิ์นำชัย จำกัด (แก้ไข)'
    assert after['importer_th'] == 'นำเข้าโดย : บริษัท เซ็นได เทรดดิ้ง จำกัด'
    assert after['updated_at'] != before['updated_at']


# ── Nav pin (post-ship-fix regression, per erp-engineering-discipline) ───────

def test_labels_manage_nav_present_desktop_and_mobile(tmp_db):
    from app import _ENDPOINT_MODULE
    assert _ENDPOINT_MODULE.get('labels.manage') == 'operation'
    assert _ENDPOINT_MODULE.get('labels.edit') == 'operation'
    assert _ENDPOINT_MODULE.get('labels.bulk_size') == 'operation'
    assert _ENDPOINT_MODULE.get('labels.company_block') == 'operation'

    html = _client('admin').get('/labels/manage').get_data(as_text=True)
    # both the desktop sidebar AND the mobile drawer must link to /labels/manage
    assert html.count('href="/labels/manage"') >= 2, "จัดการป้ายสินค้า link missing from a nav"
