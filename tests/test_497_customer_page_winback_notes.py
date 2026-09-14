"""TDD for #497 milestone 3 — customer page: win-back badge/overflow, notes
card + add-note box, validated return, contact_note.

Seam 1 (data layer): models.get_customer_summary_by_code's new winback
wiring — code-scoped (never merges a shared bill name), date-filter
INDEPENDENT (full history always), agrees with call_card.get_card for the
same code.
Seam 2 (HTTP / render): the customer page's notes card, add-note box role
gate, contact_note display, win-back badge + overflow line — session-injected
roles, prior art tests/test_493_slice2_product_card.py + test_call_routes.py.
Seam 3 (redirect): call.call_note's return_to allow-list — no open redirect.
Seam 4: access_control.role_can_post actually reads the real POST
whitelist, for every role — not just the roles this page happens to render
for.

Prior art for fixtures: tests/test_493_slice2_product_card.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
from urllib.parse import quote, urlsplit

import pytest

import access_control

SENDAI_BRAND_ID = 3

TEST_CODE = 'TEST4972'
TEST_NAME = 'ลูกค้าทดสอบ 497 หน้าลูกค้า'

SHARED_NAME = 'ร้านทดสอบ 497 หน้าลูกค้าชื่อซ้ำ'
CODE_A = 'TEST4972A'
CODE_B = 'TEST4972B'

# N7: every role the real gate distinguishes, derived from the gate's OWN
# whitelist dict rather than hand-typed — a hand-typed list would rot the
# moment a new role is added, exactly the drift risk role_can_post exists
# to avoid. 'admin' is not a key in _ROLE_POST_OK (it bypasses the
# whitelist check entirely in require_login), so it's added explicitly.
_ROLES_TO_CHECK = sorted(access_control._ROLE_POST_OK.keys()) + ['admin']

_pid_counter = [497200]


def _mk_product(conn, name='สินค้าทดสอบ 497 หน้าลูกค้า', unit_type='ตัว', base=100.0, cost=60.0):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES (?,?,?,?,?,1)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, SENDAI_BRAND_ID),
    )
    conn.commit()
    return cur.lastrowid


def _mk_customer(conn, code, name, contact_note=None):
    conn.execute(
        "INSERT INTO customers (code, name, contact_note) VALUES (?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name, "
        "contact_note = excluded.contact_note",
        (code, name, contact_note),
    )
    conn.commit()


def _clear(conn, *codes):
    for code in codes:
        conn.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (code,))
        conn.execute("DELETE FROM customer_call_log WHERE customer_code = ?", (code,))
    conn.commit()


def _line(conn, *, doc_base, pid, date_iso, code, name=TEST_NAME, qty=1,
          unit_price=100, net=100, unit='ตัว'):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
        (date_iso, f'{doc_base}-1', doc_base, pid, name, code, qty, unit, unit_price, net, net),
    )
    conn.commit()


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


@pytest.fixture
def cust(tmp_db_conn):
    conn = tmp_db_conn
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    pid = _mk_product(conn)
    yield conn, pid
    _clear(conn, TEST_CODE)


def _winback_eligible(conn, pid, code, name=TEST_NAME, prefix='IVW'):
    for i, d in enumerate(['2020-01-01', '2020-02-01', '2020-03-01']):
        _line(conn, doc_base=f'{prefix}{i}', pid=pid, date_iso=d, code=code, name=name)


# ── Seam 1: data layer ───────────────────────────────────────────────────────

def test_shared_bill_name_never_leaks_into_the_other_codes_winback(tmp_db_conn):
    conn = tmp_db_conn
    _mk_customer(conn, CODE_A, SHARED_NAME)
    _mk_customer(conn, CODE_B, SHARED_NAME)
    _clear(conn, CODE_A, CODE_B)
    pid_a = _mk_product(conn, name='สินค้าเฉพาะ A หน้าลูกค้า')
    pid_b = _mk_product(conn, name='สินค้าเฉพาะ B หน้าลูกค้า')
    _winback_eligible(conn, pid_a, CODE_A, name=SHARED_NAME, prefix='IVWA')
    _winback_eligible(conn, pid_b, CODE_B, name=SHARED_NAME, prefix='IVWB')

    import models
    data_a = models.get_customer_summary_by_code(CODE_A)
    data_b = models.get_customer_summary_by_code(CODE_B)
    keys_a = {(w['product_id'], w['unit']) for w in data_a['winback']}
    keys_b = {(w['product_id'], w['unit']) for w in data_b['winback']}

    # Control: each code's own product IS flagged under its own code.
    assert (pid_a, 'ตัว') in keys_a
    assert (pid_b, 'ตัว') in keys_b
    # The assertion: it never leaks to the OTHER code just because the bill
    # name is shared.
    assert (pid_b, 'ตัว') not in keys_a
    assert (pid_a, 'ตัว') not in keys_b

    _clear(conn, CODE_A, CODE_B)


def test_date_filter_leaves_winback_unchanged_but_changes_product_cards(cust):
    """Control: the SAME filter DOES change the product-card rows (fewer
    evidenced purchases inside the window) — proving the filter reaches the
    query at all — while win-back (always full history) stays identical."""
    conn, pid = cust
    _winback_eligible(conn, pid, TEST_CODE)

    import models
    unfiltered = models.get_customer_summary_by_code(TEST_CODE)
    filtered = models.get_customer_summary_by_code(TEST_CODE, date_from='2020-01-15')

    def _card(data):
        return next(c for c in data['product_cards'] if c['product_id'] == pid)

    # Control: the filter excludes the 2020-01-01 purchase from the card.
    assert _card(unfiltered)['times_bought'] == 3
    assert _card(filtered)['times_bought'] == 2

    # The assertion: win-back is computed over FULL history regardless.
    assert unfiltered['winback'] == filtered['winback']
    assert unfiltered['winback_overflow_count'] == filtered['winback_overflow_count']


def test_call_card_and_customer_page_flag_the_same_items(cust):
    conn, pid = cust
    _winback_eligible(conn, pid, TEST_CODE)

    import call_card as cc
    import models
    page = models.get_customer_summary_by_code(TEST_CODE)
    card = cc.get_card(conn, TEST_CODE)

    page_keys = {(w['product_id'], w['unit']) for w in page['winback']}
    card_keys = {(w['product_id'], w['unit']) for w in card['winback']}
    assert page_keys == card_keys
    assert (pid, 'ตัว') in page_keys


def test_product_card_carries_its_own_winback_flag(cust):
    conn, pid = cust
    _winback_eligible(conn, pid, TEST_CODE)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = next(c for c in data['product_cards'] if c['product_id'] == pid)
    assert card['winback'] is not None
    assert card['winback']['median_gap_days'] > 0
    assert card['winback']['days_since'] > card['winback']['median_gap_days']


def test_overflow_count_excludes_items_already_on_the_card(cust):
    """A winback-flagged (product, unit) that IS among the rendered product
    cards must not ALSO be counted in the overflow line."""
    conn, pid = cust
    _winback_eligible(conn, pid, TEST_CODE)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card_keys = {(c['product_id'], c['unit']) for c in data['product_cards']}
    assert (pid, 'ตัว') in card_keys
    assert data['winback_overflow_count'] == 0


def test_overflow_count_positive_when_flagged_item_falls_outside_the_card_union(cust):
    """20 filler products, each bought 3x at a much higher net, exactly
    fill BOTH top-20 orderings (times_bought ties at 3 for everyone, so the
    tie-break -net keeps every filler ahead of the flagged product in BOTH
    orderings) and push the flagged product out of the union entirely — it
    must show up in the overflow count, not silently vanish."""
    conn, pid = cust
    _winback_eligible(conn, pid, TEST_CODE)  # pushed out of the union
    for n in range(20):
        filler = _mk_product(conn, name=f'สินค้าเติม 497 หน้าลูกค้า {n}')
        for i, d in enumerate(['2024-01-01', '2024-01-02', '2024-01-03']):
            _line(conn, doc_base=f'IVFILL{n:02d}{i}', pid=filler, date_iso=d,
                  code=TEST_CODE, unit_price=1000000, net=1000000)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card_keys = {(c['product_id'], c['unit']) for c in data['product_cards']}
    assert (pid, 'ตัว') not in card_keys
    assert data['winback_overflow_count'] == 1


# ── Seam 2: HTTP / render ────────────────────────────────────────────────────

def test_customer_page_shows_contact_note_when_set(tmp_db):
    """The read-only display, scoped to its OWN marker — not a page-wide
    substring. The manager-only edit modal already carries
    `<textarea name="contact_note">{{ ci.contact_note }}</textarea>` (label
    "หมายเหตุติดต่อ") REGARDLESS of this feature, so both
    'วางบิลทุกวันที่ 5' and even bare 'หมายเหตุ' (a substring of
    'หมายเหตุติดต่อ') are already present on an unmodified page — verified
    against the pre-change template before writing this assertion."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME, contact_note='วางบิลทุกวันที่ 5')
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'data-contact-note-value>วางบิลทุกวันที่ 5<' in html
    assert '>หมายเหตุ<' in html


def test_customer_page_omits_contact_note_block_when_unset(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME, contact_note=None)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'วางบิลทุกวันที่ 5' not in html
    assert 'data-contact-note-value' not in html


def test_customer_page_notes_card_lists_newest_first(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.execute(
        "INSERT INTO customer_call_log (customer_code, kind, body, created_by, created_at) "
        "VALUES (?,?,?,?,?)", (TEST_CODE, 'note', 'บันทึกเก่า', 'sanchai', '2026-01-01 08:00:00'))
    conn.execute(
        "INSERT INTO customer_call_log (customer_code, kind, body, created_by, created_at) "
        "VALUES (?,?,?,?,?)", (TEST_CODE, 'note', 'บันทึกใหม่', 'sanchai', '2026-06-01 08:00:00'))
    conn.commit()
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert html.index('บันทึกใหม่') < html.index('บันทึกเก่า')
    assert 'ยังไม่มีบันทึก' not in html


def test_customer_page_notes_card_shows_placeholder_with_no_entries(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'ยังไม่มีบันทึก' in html


def test_soft_deleted_note_is_hidden_control_live_note_shows(tmp_db):
    """N8: no PAGE-level test pinned that a soft-deleted note stays hidden
    here — it was only covered indirectly, via call_card.get_log's own
    tests, and only because this route happens to call that shared
    function rather than reading customer_call_log itself."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.execute(
        "INSERT INTO customer_call_log (customer_code, kind, body, created_by) "
        "VALUES (?,?,?,?)", (TEST_CODE, 'note', 'บันทึกที่ยังอยู่', 'sanchai'))
    conn.execute(
        "INSERT INTO customer_call_log (customer_code, kind, body, created_by, deleted_at, deleted_by) "
        "VALUES (?,?,?,?,datetime('now'),?)",
        (TEST_CODE, 'note', 'บันทึกที่ถูกลบแล้ว', 'sanchai', 'sanchai'))
    conn.commit()
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    # Control: the live note DOES render.
    assert 'บันทึกที่ยังอยู่' in html
    assert 'บันทึกที่ถูกลบแล้ว' not in html


def test_staff_sees_the_add_note_box(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db, role='staff')
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert f'action="{url_customer_note(TEST_CODE)}"' in html


def test_add_note_button_has_min_width_and_body_input_is_required(tmp_db):
    """F6: `input-group-sm` shrinks the เพิ่ม button below the 44px touch
    floor (measured 39.2px wide in real Chrome) even with btn-touch's own
    min-height:44px applied. N11: the blank-note SERVER behaviour is
    pre-existing (same as the call card's own form) and stays as-is —
    this only pins the client-side `required` guard on the new input."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db, role='staff')
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    action, body_html = _form_with_action_containing(html, f'/call/{TEST_CODE}/note')
    body_input = re.search(r'<input\b[^>]*name="body"[^>]*>', body_html)
    submit_btn = re.search(r'<button\b[^>]*type="submit"[^>]*>', body_html)
    assert body_input and 'required' in body_input.group(0)
    assert submit_btn and 'min-width:44px' in submit_btn.group(0)


def test_shareholder_sees_the_list_but_no_add_note_box(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.execute(
        "INSERT INTO customer_call_log (customer_code, kind, body, created_by) "
        "VALUES (?,?,?,?)", (TEST_CODE, 'note', 'โน้ตของผู้ถือหุ้นอ่านได้', 'sanchai'))
    conn.commit()
    conn.close()

    c = _client(tmp_db, role='shareholder')
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'โน้ตของผู้ถือหุ้นอ่านได้' in html
    assert f'action="{url_customer_note(TEST_CODE)}"' not in html


@pytest.mark.parametrize('role', _ROLES_TO_CHECK)
def test_role_can_post_matches_the_real_post_gate_for_call_note(role, tmp_db):
    """N7: role_can_post's own docstring promises it reads the same
    whitelist require_login enforces — re-typing it as a hardcoded
    ('admin','manager','staff') tuple passed every render test (24/24), so
    nothing actually pins it to the gate it claims to mirror. Drive the
    REAL POST for every role the gate distinguishes and assert the
    helper's prediction matches whether a row actually got written."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    predicted = access_control.role_can_post(role, 'call.call_note')
    c = _client(tmp_db, role=role)
    c.post(f'/call/{TEST_CODE}/note', data={'body': f'จาก {role}'})

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM customer_call_log WHERE customer_code = ?", (TEST_CODE,)
    ).fetchall()
    conn.close()
    actual = len(rows) == 1
    assert predicted == actual, f"role={role}: predicted={predicted}, actual={actual}"


def test_role_can_post_matches_the_real_gate_across_endpoints(tmp_db, monkeypatch):
    """N7, stronger form: `call.call_note` happens to be whitelisted for
    EXACTLY {admin, manager, staff} today, so a hardcoded
    `role in ('admin','manager','staff')` retyping of role_can_post matches
    the real gate for THAT ONE endpoint by pure coincidence — verified: the
    reviewer's own M3c mutation (that exact retype) stays green against a
    call.call_note-only test, including the one directly above. Two more
    real endpoints with DIFFERENT role membership close that gap:
    partners.customer_reassign (manager/admin, NOT staff) and
    cashbook.new_transaction (manager/shareholder/admin, NOT staff) — a
    hardcoded tuple mispredicts staff on both. Each view is stubbed to a
    no-op (no side effects on customer/cashbook data), so this exercises
    ONLY require_login's routing decision, never the endpoints' own logic."""
    from app import app as flask_app
    paths = {
        'call.call_note': f'/call/{TEST_CODE}/note',
        'partners.customer_reassign': f'/customer/{TEST_CODE}/reassign',
        'cashbook.new_transaction': '/cashbook/new',
    }
    for ep in paths:
        monkeypatch.setitem(flask_app.view_functions, ep, lambda **kw: ('stub', 200))

    for ep, path in paths.items():
        for role in _ROLES_TO_CHECK:
            predicted = access_control.role_can_post(role, ep)
            c = _client(tmp_db, role=role)
            r = c.post(path, data={})
            actual = r.status_code == 200
            assert predicted == actual, (
                f"endpoint={ep} role={role}: predicted={predicted} "
                f"actual={actual} status={r.status_code}")


def url_customer_note(code):
    return f'/call/{code}/note'


def _table_row_containing(html, marker):
    """The single `<tr ...>...</tr>` block whose text contains `marker` —
    scopes an assertion to ONE product card row instead of the whole page
    (a page-wide substring match would also hit the product's own NAME if it
    happened to contain the badge text, or a `<script>` block)."""
    rows = re.findall(r'<tr\b.*?</tr>', html, re.S)
    hits = [r for r in rows if marker in r]
    assert len(hits) == 1, f"expected exactly one row containing {marker!r}, found {len(hits)}"
    return hits[0]


def _form_with_action_containing(html, needle):
    """The (action, inner-HTML) of the single `<form>` on the page whose
    action URL contains `needle`. Review finding F2: a test that hand-crafts
    its own POST payload (`data={'body': ..., 'return_to': 'customer'}`)
    proves the ROUTE works but not that the RENDERED form actually submits
    those fields — deleting the template's hidden `return_to` input left
    the hand-crafted version green while a real click would have silently
    landed on the call card."""
    forms = re.findall(r'<form\b([^>]*)>(.*?)</form>', html, re.S)
    for attrs_str, body in forms:
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', attrs_str))
        if needle in attrs.get('action', ''):
            return attrs.get('action'), body
    raise AssertionError(f"no <form> with action containing {needle!r}")


def _hidden_inputs(form_inner_html):
    """{name: value} for every `type="hidden"` `<input>` inside a form's
    inner HTML — attribute-order independent, so it doesn't matter whether
    the template writes `type` before or after `name`/`value`."""
    hidden = {}
    for tag in re.findall(r'<input\b[^>]*>', form_inner_html):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if attrs.get('type') == 'hidden' and 'name' in attrs:
            hidden[attrs['name']] = attrs.get('value', '')
    return hidden


def test_winback_badge_shown_only_on_flagged_row(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    # Deliberately NOT named with the badge word itself (ขาดช่วง) — a page-wide
    # substring assertion must not be satisfiable by the fixture's own product
    # name (that trap bit this exact test on first write: the product name
    # alone made 'ขาดช่วง'.count(html) == 1 pass with the feature unimplemented).
    pid_lapsed = _mk_product(conn, name='สินค้าซื้อนานแล้ว 497 หน้าลูกค้า')
    pid_fresh = _mk_product(conn, name='สินค้าซื้อสม่ำเสมอ 497 หน้าลูกค้า')
    for i, d in enumerate(['2020-01-01', '2020-02-01', '2020-03-01']):
        _line(conn, doc_base=f'IVWF{i}', pid=pid_lapsed, date_iso=d, code=TEST_CODE)
    # Fresh product: 3 purchases with the LAST one recent -> not flagged.
    import datetime as _dt
    recent = _dt.date.today().isoformat()
    for i, d in enumerate(['2020-01-01', '2020-02-01', recent]):
        _line(conn, doc_base=f'IVWG{i}', pid=pid_fresh, date_iso=d, code=TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    # Control: the page rendered both product rows at all.
    lapsed_row = _table_row_containing(html, 'สินค้าซื้อนานแล้ว')
    fresh_row = _table_row_containing(html, 'สินค้าซื้อสม่ำเสมอ')
    assert 'ขาดช่วง' in lapsed_row
    assert 'ปกติซื้อทุก' in lapsed_row
    assert 'ขาดช่วง' not in fresh_row


def test_winback_badge_only_on_the_flagged_units_own_card_row(tmp_db):
    """F5 render check: ONE product bought as both ตัว (lapsed) and โหล
    (fresh) must produce TWO card rows (models/customers.py keys product
    cards by (product_id, unit) too), and the badge must land on the ตัว
    row only — never bleed onto the โหล row for the same product name."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    pid = _mk_product(conn, name='สินค้าสองหน่วย 497 หน้าลูกค้า')
    for i, d in enumerate(['2020-01-01', '2020-02-01', '2020-03-01']):
        _line(conn, doc_base=f'IVUN{i}', pid=pid, date_iso=d, code=TEST_CODE, unit='ตัว')
    import datetime as _dt
    recent = _dt.date.today().isoformat()
    for i, d in enumerate(['2025-11-01', '2025-12-01', recent]):
        _line(conn, doc_base=f'IVUL{i}', pid=pid, date_iso=d, code=TEST_CODE,
              unit='โหล', unit_price=1000, net=1000)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    piece_row = _table_row_containing(html, '(ตัว)</span>')
    dozen_row = _table_row_containing(html, '(โหล)</span>')
    assert 'ขาดช่วง' in piece_row
    assert 'ขาดช่วง' not in dozen_row


def test_overflow_line_absent_at_zero(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'รายการซื้อขาดช่วง' not in html


def test_overflow_line_renders_with_count_and_call_card_link(tmp_db):
    """F3: `winback_overflow_count` was pinned only at the data layer — the
    render was untested in the POSITIVE direction (only the absent-at-zero
    case had a render test), so `{% if data.winback_overflow_count %}`
    could have been replaced with `{% if false %}` and every render test
    would still pass. Same 20-filler shape as
    test_overflow_count_positive_when_flagged_item_falls_outside_the_card_union
    (data layer) pushes ONE flagged product out of the card union."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    pid = _mk_product(conn)
    for i, d in enumerate(['2020-01-01', '2020-02-01', '2020-03-01']):
        _line(conn, doc_base=f'IVOVF{i}', pid=pid, date_iso=d, code=TEST_CODE)
    for n in range(20):
        filler = _mk_product(conn, name=f'สินค้าเติมล้น {n}')
        for i, d in enumerate(['2024-01-01', '2024-01-02', '2024-01-03']):
            _line(conn, doc_base=f'IVOVFILL{n:02d}{i}', pid=filler, date_iso=d,
                  code=TEST_CODE, unit_price=1000000, net=1000000)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    m = re.search(r'<a\b([^>]*)href="([^"]*)"([^>]*)>\s*⏰\s*อีก\s*(\d+)\s*รายการซื้อขาดช่วง', html)
    assert m, "overflow line with a count not found"
    attrs_before, href, attrs_after, count = m.groups()
    assert count == '1'
    assert urlsplit(href).path == f'/call/{TEST_CODE}'
    # F6: this link's text-only height (17px, real Chrome) was under the
    # 44px mobile tap-target floor — btn-touch (mobile-conventions.md §5)
    # gives it min-height:44px.
    assert 'btn-touch' in (attrs_before + attrs_after)


# ── Seam 3: redirect ─────────────────────────────────────────────────────────

def test_post_from_customer_page_creates_row_and_returns_to_customer_page(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db, role='staff')
    r = c.post(f'/call/{TEST_CODE}/note',
               data={'body': 'เยี่ยมร้านวันนี้', 'return_to': 'customer'})
    assert r.status_code in (302, 303)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM customer_call_log WHERE customer_code = ?", (TEST_CODE,)
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]['body'] == 'เยี่ยมร้านวันนี้'

    assert urlsplit(r.headers['Location']).path == f'/customer/code/{TEST_CODE}'


def test_rendered_add_note_form_submits_its_own_fields_and_returns_to_customer_page(tmp_db):
    """F2: the ABOVE test hand-crafts `return_to=customer` and never touches
    the rendered form at all — deleting the template's hidden `return_to`
    input left it green. Here we GET the real page, scrape the note form's
    action + every hidden input, and submit exactly that (plus the note
    text), the way a real click does."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db, role='staff')
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    action, body_html = _form_with_action_containing(html, f'/call/{TEST_CODE}/note')
    hidden = _hidden_inputs(body_html)
    # Control: the scrape actually found the field under test — if this
    # fails, the assertion below is meaningless (see F2's break-it-once).
    assert hidden.get('return_to') == 'customer'

    payload = dict(hidden)
    payload['body'] = 'จากฟอร์มจริงบนหน้าลูกค้า'
    r = c.post(action, data=payload)
    assert r.status_code in (302, 303)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM customer_call_log WHERE customer_code = ?", (TEST_CODE,)
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]['body'] == 'จากฟอร์มจริงบนหน้าลูกค้า'
    assert urlsplit(r.headers['Location']).path == f'/customer/code/{TEST_CODE}'


def test_post_from_call_card_still_returns_to_call_card(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db, role='staff')
    r = c.post(f'/call/{TEST_CODE}/note', data={'body': 'จากการ์ดโทร'})
    assert urlsplit(r.headers['Location']).path == f'/call/{TEST_CODE}'


def test_external_return_target_falls_back_to_call_card(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn, TEST_CODE, TEST_NAME)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db, role='staff')
    r = c.post(f'/call/{TEST_CODE}/note',
               data={'body': 'พยายามเปิดทาง', 'return_to': 'http://evil.example/'})
    location = r.headers['Location']
    parsed = urlsplit(location)
    assert parsed.netloc == ''  # never an absolute external URL
    assert parsed.path == f'/call/{TEST_CODE}'
