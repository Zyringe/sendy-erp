"""TDD for Phase 2 of projects/customer-edit-card/plan.md — edit-on-card
modal + commission warning.

Covers:
  - the modal renders every group-1 (salesperson/region_id) + group-2
    (nickname/phone/fax/contact/address/contact_note) input, even ones the
    card itself hides because they're empty (the card's `{% if ci.phone %}`
    guard must not leak into the modal)
  - the save path (models.update_customer_edit / partners.customer_reassign):
      * stamps contact_normalized_at/_by only when a group-2 field changed
      * a salesperson-only save leaves contact_normalized_at untouched
      * a stamped row survives the next BSN import unchanged (protected
        branch of import_customers_from_bsn)
      * a changed group-2 field refreshes a PENDING customer_contact_review
        row's proposed_* columns (status stays pending) so the review queue
        can't silently revert the edit
  - the commission warning shows for manager+admin; the admin-only link to
    /commission/reassign/new is present for admin and absent for manager
  - GET /commission/reassign/new?customer_code=X prefills the field
  - the old standalone "เปลี่ยน salesperson / เขตการขาย" card is gone
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from urllib.parse import quote


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


# ── Modal shows every field, even ones the card hides when empty ───────────
# 11ม06 has fax/nickname/contact_note NULL (no real billing customer in prod
# has phone AND address both NULL — verified 2026-08-01) — this is still the
# behaviour the plan's requirement is pinning: none of these inputs may be
# gated behind the card's `{% if ci.<field> %}` truthiness check.

def test_modal_renders_every_field_including_empty_ones(tmp_db):
    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote("11ม06")}').data.decode()
    assert 'id="customerEditModal"' in html
    for name in ('salesperson', 'region_id', 'nickname', 'phone', 'fax',
                 'contact', 'address', 'contact_note'):
        assert f'name="{name}"' in html, f'{name} input missing from modal'
    # fax/nickname/contact_note are NULL for this customer and the OLD card
    # hid empty fields entirely — the modal must show the input anyway.
    import re
    fax_input = re.search(r'<input[^>]*name="fax"[^>]*>', html)
    assert fax_input, 'fax input not found'
    assert 'value=""' in fax_input.group(0) or "value=''" in fax_input.group(0)


def test_modal_absent_for_staff(tmp_db):
    """is_manager gates the button AND the modal itself (staff can't reach
    the save endpoint — partners.customer_reassign isn't in _STAFF_POST_OK)."""
    c = _client(tmp_db, role='staff')
    html = c.get(f'/customer/code/{quote("11ม06")}').data.decode()
    assert 'id="customerEditModal"' not in html


# ── Old card is gone ─────────────────────────────────────────────────────

def test_old_reassign_card_removed_modal_present_instead(tmp_db):
    """A bare Thai-substring check would false-pass (the modal's warning
    text also mentions the concept) — assert on the form action URL count:
    the old card and the modal both posted to the same reassign endpoint,
    so the removal must drop it from 2 occurrences to 1."""
    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote("43ท013")}').data.decode()
    action = 'action="/customer/43%E0%B8%97013/reassign"'
    assert html.count(action) == 1, (
        f'expected exactly 1 reassign form (the modal), found {html.count(action)}')
    assert 'เปลี่ยน salesperson / เขตการขาย' not in html


# ── Save path: contact_normalized_at stamping ───────────────────────────

def _customer_row(tmp_db, code):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM customers WHERE code=?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def test_saving_a_changed_phone_stamps_contact_normalized_at(tmp_db):
    before = _customer_row(tmp_db, '11ม06')
    assert before['contact_normalized_at'] is None or before['contact_normalized_at'] == ''

    c = _client(tmp_db, role='admin')
    r = c.post('/customer/11ม06/reassign', data={
        'salesperson': before['salesperson'] or '',
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '',
        'phone': '099-999-9999',  # changed from "เฮีย 081-5502828"
        'fax': before['fax'] or '',
        'contact': before['contact'] or '',
        'address': before['address'] or '',
        'contact_note': before['contact_note'] or '',
    }, follow_redirects=False)
    assert r.status_code == 302

    after = _customer_row(tmp_db, '11ม06')
    assert after['phone'] == '099-999-9999'
    assert after['contact_normalized_at']
    assert after['contact_normalized_by'] == 'admin'


def test_saving_only_salesperson_does_not_stamp_or_touch_review_row(tmp_db):
    """Re-submitting the SAME contact values (as the modal always does when
    the user only touches the salesperson field) must not fire the stamp —
    over-protecting a row nobody meant to normalize."""
    before = _customer_row(tmp_db, '11ม06')
    assert before['contact_normalized_at'] is None or before['contact_normalized_at'] == ''

    import sqlite3
    conn = sqlite3.connect(tmp_db)
    review_before = conn.execute(
        "SELECT proposed_phone, status FROM customer_contact_review WHERE customer_code='11ม06'"
    ).fetchone()
    conn.close()
    assert review_before is not None and review_before[1] == 'pending'

    c = _client(tmp_db, role='admin')
    r = c.post('/customer/11ม06/reassign', data={
        'salesperson': '02',  # changed: was '00'
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '',
        'phone': before['phone'] or '',
        'fax': before['fax'] or '',
        'contact': before['contact'] or '',
        'address': before['address'] or '',
        'contact_note': before['contact_note'] or '',
    }, follow_redirects=False)
    assert r.status_code == 302

    after = _customer_row(tmp_db, '11ม06')
    assert after['salesperson'] == '02'
    assert after['contact_normalized_at'] is None or after['contact_normalized_at'] == ''
    assert after['phone'] == before['phone']

    conn = sqlite3.connect(tmp_db)
    review_after = conn.execute(
        "SELECT proposed_phone, status FROM customer_contact_review WHERE customer_code='11ม06'"
    ).fetchone()
    conn.close()
    assert review_after == review_before, 'unrelated (salesperson-only) save must not touch the review row'


# ── Save path: pending review-row sync ──────────────────────────────────

def test_saving_a_changed_contact_field_updates_pending_review_row(tmp_db):
    """11ม06 has a PENDING customer_contact_review row (id 315 in prod).
    Without the sync, clicking ยืนยัน there later reverts this save (that
    page prefills from the frozen proposed_* snapshot, not the live row)."""
    before = _customer_row(tmp_db, '11ม06')

    c = _client(tmp_db, role='admin')
    r = c.post('/customer/11ม06/reassign', data={
        'salesperson': before['salesperson'] or '',
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '',
        'phone': '099-999-9999',
        'fax': before['fax'] or '',
        'contact': before['contact'] or '',
        'address': before['address'] or '',
        'contact_note': before['contact_note'] or '',
    }, follow_redirects=False)
    assert r.status_code == 302

    import sqlite3
    conn = sqlite3.connect(tmp_db)
    review = conn.execute(
        "SELECT proposed_phone, status FROM customer_contact_review WHERE customer_code='11ม06'"
    ).fetchone()
    conn.close()
    assert review is not None
    assert review[0] == '099-999-9999'
    assert review[1] == 'pending'  # status must NOT flip — only Put confirming does that


# ── Save path: stamped row survives the next BSN import ─────────────────

def test_stamped_contact_survives_next_bsn_import(tmp_db):
    import models

    before = _customer_row(tmp_db, '11ม06')
    c = _client(tmp_db, role='admin')
    c.post('/customer/11ม06/reassign', data={
        'salesperson': before['salesperson'] or '',
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '',
        'phone': '099-999-9999',
        'fax': before['fax'] or '',
        'contact': before['contact'] or '',
        'address': before['address'] or '',
        'contact_note': before['contact_note'] or '',
    })
    stamped = _customer_row(tmp_db, '11ม06')
    assert stamped['contact_normalized_at']

    inserted, updated, protected = models.import_customers_from_bsn([{
        'code': '11ม06', 'name': stamped['name'], 'salesperson': stamped['salesperson'],
        'zone': stamped['zone'], 'customer_type': stamped['customer_type'],
        'credit_days': stamped['credit_days'], 'tax_id': stamped['tax_id'],
        'address': 'FILE ADDRESS — SHOULD NOT LAND',
        'phone': 'FILE PHONE — SHOULD NOT LAND',
        'contact': 'FILE CONTACT — SHOULD NOT LAND',
    }])
    assert protected == 1
    assert inserted == 0 and updated == 0

    after_import = _customer_row(tmp_db, '11ม06')
    assert after_import['phone'] == '099-999-9999'
    assert after_import['address'] == stamped['address']
    assert after_import['contact'] == stamped['contact']


# ── Commission warning + admin-only link ────────────────────────────────

def test_warning_text_present_for_manager_and_admin(tmp_db):
    for role in ('manager', 'admin'):
        c = _client(tmp_db, role=role)
        html = c.get(f'/customer/code/{quote("43ท013")}').data.decode()
        assert 'คอมมิชชั่นไม่ขยับตาม' in html, f'warning missing for {role}'


def test_commission_reassign_link_present_for_admin_absent_for_manager(tmp_db):
    href = 'href="/commission/reassign/new?customer_code=43%E0%B8%97013"'

    c = _client(tmp_db, role='admin')
    html_admin = c.get(f'/customer/code/{quote("43ท013")}').data.decode()
    assert href in html_admin

    c = _client(tmp_db, role='manager')
    html_manager = c.get(f'/customer/code/{quote("43ท013")}').data.decode()
    assert href not in html_manager
    assert 'commission_reassign_new' not in html_manager.replace('commission.commission_reassign_new', '')


def test_modal_absent_for_shareholder(tmp_db):
    """shareholder is read-only (not is_manager) — no modal at all, which
    means no commission link either. Distinct guard layer from the
    is_admin check covered above."""
    c = _client(tmp_db, role='shareholder')
    html = c.get(f'/customer/code/{quote("43ท013")}').data.decode()
    assert 'id="customerEditModal"' not in html


# ── GET /commission/reassign/new prefill ────────────────────────────────

def test_commission_reassign_new_get_prefills_from_query_arg(tmp_db):
    c = _client(tmp_db, role='admin')
    r = c.get('/commission/reassign/new?customer_code=43%E0%B8%97013')
    assert r.status_code == 200
    html = r.data.decode()
    assert 'value="43ท013"' in html


def test_commission_reassign_new_get_without_arg_renders_blank(tmp_db):
    c = _client(tmp_db, role='admin')
    r = c.get('/commission/reassign/new')
    assert r.status_code == 200
    html = r.data.decode()
    assert 'value=""' in html or "placeholder=" in html


# ── The review sync must be PER FIELD, not all six ─────────────────────────
# Review finding (2026-08-01): the first cut wrote all six proposed_* columns
# on any contact change, so editing just the phone destroyed the normalizer's
# un-reviewed proposals for the other fields. 53 of the 62 pending rows have at
# least one proposed_* that differs from the live value, so this was routine
# data loss, e.g. 11ม06's proposed_address carries a postcode the live row lacks.

def test_review_sync_touches_only_the_fields_that_changed(tmp_db):
    import sqlite3
    before_row = _customer_row(tmp_db, '11ม06')
    conn = sqlite3.connect(tmp_db)
    prop_before = conn.execute(
        "SELECT proposed_contact, proposed_address, proposed_nickname "
        "FROM customer_contact_review WHERE customer_code='11ม06' AND status='pending'"
    ).fetchone()
    conn.close()
    assert prop_before is not None
    # The fixture must actually contain a proposal that differs from the live
    # row, or this test cannot fail.
    assert (prop_before[0] or '') != (before_row['contact'] or '') \
        or (prop_before[1] or '') != (before_row['address'] or ''), \
        'fixture no longer has a divergent proposal — this test would be vacuous'

    c = _client(tmp_db, role='admin')
    r = c.post('/customer/11ม06/reassign', data={
        'salesperson': before_row['salesperson'] or '',
        'region_id': str(before_row['region_id'] or ''),
        'nickname': before_row['nickname'] or '',
        'phone': '02-111-2222',                    # the ONLY change
        'fax': before_row['fax'] or '',
        'contact': before_row['contact'] or '',
        'address': before_row['address'] or '',
        'contact_note': before_row['contact_note'] or '',
    })
    assert r.status_code == 302

    conn = sqlite3.connect(tmp_db)
    prop_after = conn.execute(
        "SELECT proposed_contact, proposed_address, proposed_nickname, proposed_phone "
        "FROM customer_contact_review WHERE customer_code='11ม06' AND status='pending'"
    ).fetchone()
    conn.close()
    assert prop_after[3] == '02-111-2222', 'the changed field must sync'
    assert prop_after[0] == prop_before[0], 'proposed_contact was not edited — must survive'
    assert prop_after[1] == prop_before[1], 'proposed_address was not edited — must survive'
    assert prop_after[2] == prop_before[2], 'proposed_nickname was not edited — must survive'


def test_clear_filter_link_still_uses_the_code_after_phase2(tmp_db):
    """Phase 1's blocker: a link built from data.customer ejects the user on
    ambiguous and bill-less customers. Phase 2 rewrote most of this template."""
    import re
    c = _client(tmp_db, role='admin')
    html = c.get(f'/customer/code/{quote("43ท013")}').data.decode()
    m = re.search(
        r'href="([^"]*)"\s*\n?\s*class="btn btn-sm btn-outline-secondary ms-1"', html)
    assert m, 'clear-filter link not found'
    from urllib.parse import unquote
    assert unquote(m.group(1)) == '/customer/code/43ท013'
