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


# ── C: freeze the pre-edit state once ──────────────────────────────────────
# 1,674 of 2,665 customers have contact_orig_json NULL, i.e. no record of what
# the row looked like before anyone touched it. The first modal edit fills it.

def test_first_contact_edit_freezes_pre_edit_snapshot(tmp_db):
    import json, sqlite3
    conn = sqlite3.connect(tmp_db)
    code, before_phone = conn.execute(
        "SELECT code, phone FROM customers "
        " WHERE contact_orig_json IS NULL AND COALESCE(phone,'') <> '' LIMIT 1"
    ).fetchone()
    conn.close()
    before = _customer_row(tmp_db, code)

    c = _client(tmp_db, role='admin')
    payload = {
        'salesperson': before['salesperson'] or '',
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '', 'fax': before['fax'] or '',
        'contact': before['contact'] or '', 'address': before['address'] or '',
        'contact_note': before['contact_note'] or '',
        'phone': '02-000-1111',
    }
    assert c.post(f'/customer/{code}/reassign', data=payload).status_code == 302

    conn = sqlite3.connect(tmp_db)
    oj = conn.execute(
        "SELECT contact_orig_json FROM customers WHERE code=?", (code,)).fetchone()[0]
    conn.close()
    assert oj, 'the first contact edit must freeze a snapshot'
    assert json.loads(oj)['phone'] == before_phone, 'must freeze the PRE-edit value'

    # A second edit must NOT clobber it (COALESCE).
    payload['phone'] = '02-222-3333'
    c.post(f'/customer/{code}/reassign', data=payload)
    conn = sqlite3.connect(tmp_db)
    oj2 = conn.execute(
        "SELECT contact_orig_json FROM customers WHERE code=?", (code,)).fetchone()[0]
    conn.close()
    assert oj2 == oj, 'an existing snapshot must never be overwritten'


def test_salesperson_only_edit_does_not_freeze_a_snapshot(tmp_db):
    """The snapshot exists to protect CONTACT data; a rep change must not
    consume the one-shot freeze with values nobody edited."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT code FROM customers WHERE contact_orig_json IS NULL LIMIT 1").fetchone()
    conn.close()
    code = row[0]
    before = _customer_row(tmp_db, code)
    c = _client(tmp_db, role='admin')
    c.post(f'/customer/{code}/reassign', data={
        'salesperson': '00',
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '', 'phone': before['phone'] or '',
        'fax': before['fax'] or '', 'contact': before['contact'] or '',
        'address': before['address'] or '', 'contact_note': before['contact_note'] or '',
    })
    conn = sqlite3.connect(tmp_db)
    oj = conn.execute(
        "SELECT contact_orig_json FROM customers WHERE code=?", (code,)).fetchone()[0]
    conn.close()
    assert oj is None


# ── MISSING is not CLEAR: the legacy assignment-only form ──────────────────
# Codex review (2026-08-01): /customer/<code>/reassign is the SAME url the
# pre-modal card posted to, with only salesperson + region_id. A page rendered
# before this deploy and submitted after it reached the new full-edit path and
# read its absent contact keys as blanks. Reproduced on a live copy for 11ม06:
# phone/contact/address all went NULL, the row was stamped as curated, and the
# pending review row's proposed_* were NULLed too.

def test_legacy_assignment_only_post_does_not_clear_contact(tmp_db):
    import sqlite3
    before = _customer_row(tmp_db, '11ม06')
    assert (before['phone'] or before['contact'] or before['address']), \
        'fixture has no contact data — this test would be vacuous'

    c = _client(tmp_db, role='admin')
    # EXACTLY the old form's payload: no contact keys at all.
    assert c.post('/customer/11ม06/reassign',
                  data={'salesperson': '00', 'region_id': '3'}).status_code == 302

    after = _customer_row(tmp_db, '11ม06')
    for f in ('phone', 'contact', 'address', 'nickname', 'fax', 'contact_note'):
        assert after[f] == before[f], f'legacy POST cleared {f}'
    assert after['contact_normalized_at'] == before['contact_normalized_at'], \
        'legacy POST must not stamp the row as curated'
    assert after['salesperson'] == '00' and after['region_id'] == 3, \
        'the assignment it DID send must still be applied'

    conn = sqlite3.connect(tmp_db)
    prop = conn.execute(
        "SELECT proposed_phone, proposed_contact FROM customer_contact_review "
        " WHERE customer_code='11ม06' AND status='pending'").fetchone()
    conn.close()
    if prop is not None:
        assert prop[0] is not None or prop[1] is not None, \
            'legacy POST must not push NULLs into the pending review row'


def test_partial_contact_post_is_rejected_not_guessed(tmp_db):
    """Half a contact payload is neither shape — refuse rather than pick a half."""
    before = _customer_row(tmp_db, '11ม06')
    c = _client(tmp_db, role='admin')
    r = c.post('/customer/11ม06/reassign',
               data={'salesperson': before['salesperson'] or '',
                     'region_id': str(before['region_id'] or ''),
                     'phone': '02-000-0000'},          # phone only, 5 keys missing
               follow_redirects=True)
    assert r.status_code == 200
    after = _customer_row(tmp_db, '11ม06')
    assert after['phone'] == before['phone'], 'a partial payload must not be applied'
    assert after['contact'] == before['contact']


# ── Codex round 2 ──────────────────────────────────────────────────────────

def test_model_rejects_a_short_contact_payload(tmp_db):
    """update_customer_edit is public through the models facade, so the route's
    0/6/partial branch is not the only entry point. A caller that skips the
    route and passes a short dict must be refused, not have its missing keys
    read as blanks."""
    import models
    before = _customer_row(tmp_db, '11ม06')
    r = models.update_customer_edit(
        '11ม06', before['salesperson'], before['region_id'],
        {'phone': '02-000-0000'}, 'tester')          # 5 keys missing
    assert r['ok'] is False
    assert 'ไม่ครบ' in r['error']
    after = _customer_row(tmp_db, '11ม06')
    for f in ('phone', 'contact', 'address', 'nickname', 'fax', 'contact_note'):
        assert after[f] == before[f], f'a rejected payload still changed {f}'


def test_full_payload_of_blanks_clears_deliberately(tmp_db):
    """All six keys present with empty values IS the modal saying "clear these".

    Pins the destructive path across EVERY contact field, not a sample of two:
    a regression that left `contact`/`nickname`/`fax`/`contact_note` populated,
    or that skipped syncing a changed proposal, must turn this red.

    The field -> proposed_* map is restated here on purpose rather than imported
    from models: a test that reuses the mapping under test cannot catch a bug in
    it. Note `contact_note` -> `proposed_note`, which is NOT the mechanical
    `proposed_` + name the other five follow.
    """
    import json, sqlite3
    FIELDS = ('nickname', 'phone', 'fax', 'contact', 'address', 'contact_note')
    REVIEW_COL = {'nickname': 'proposed_nickname', 'phone': 'proposed_phone',
                  'fax': 'proposed_fax', 'contact': 'proposed_contact',
                  'address': 'proposed_address', 'contact_note': 'proposed_note'}

    before = _customer_row(tmp_db, '11ม06')
    populated = [f for f in FIELDS if before[f]]
    assert len(populated) >= 2, \
        f'fixture only has {populated} populated — this test would barely assert anything'

    cols = ', '.join(REVIEW_COL[f] for f in FIELDS)
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        f"SELECT {cols} FROM customer_contact_review"
        " WHERE customer_code='11ม06' AND status='pending'").fetchone()
    conn.close()
    prop_before = dict(zip(FIELDS, row)) if row else None

    c = _client(tmp_db, role='admin')
    assert c.post('/customer/11ม06/reassign', data={
        'salesperson': before['salesperson'] or '',
        'region_id': str(before['region_id'] or ''),
        'nickname': '', 'phone': '', 'fax': '',
        'contact': '', 'address': '', 'contact_note': '',
    }).status_code == 302

    after = _customer_row(tmp_db, '11ม06')
    for f in FIELDS:
        assert after[f] is None, f'{f} was not cleared'
    assert after['contact_normalized_at'] is not None, 'a deliberate clear still stamps'

    # The snapshot schema carries name + the three contact values.
    snap = json.loads(after['contact_orig_json'] or '{}')
    assert snap.get('name') == before['name']
    for f in ('phone', 'contact', 'address'):
        assert snap.get(f) == (before[f] or ''), \
            f'snapshot must hold the PRE-clear {f}'

    if prop_before is None:
        return
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        f"SELECT {cols} FROM customer_contact_review"
        " WHERE customer_code='11ม06' AND status='pending'").fetchone()
    conn.close()
    prop_after = dict(zip(FIELDS, row))
    for f in FIELDS:
        if before[f]:                      # changed nonblank -> NULL, so it syncs
            assert prop_after[f] is None, f'{f} changed but its proposal did not sync'
        else:                              # already NULL = unchanged, proposal survives
            assert prop_after[f] == prop_before[f], \
                f'{f} did not change but its proposal was overwritten'
