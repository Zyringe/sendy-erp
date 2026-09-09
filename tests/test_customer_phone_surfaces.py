"""Per-surface render tests for the shared customer-phone rendering (#462).

`customers.phone` holds a LIST (ADR 0011). #461 proved the display seam on the
call card; this file pins the behaviour on every other surface that shows a
customer's numbers, so "which page did you open?" stops changing the answer.

Rendered at the TEMPLATE layer with no DB, for the reason
`test_call_card_render.py` gives: a route test calls `get_connection()` and
creates `inventory_app/instance/inventory.db` inside the worktree, which
poisons every later run (conftest resolves LIVE_DB to it).

Every test carries a CONTROL asserting the fixture actually reached the region
under test — a phone assertion over a page that never rendered pins nothing.
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

# A customer with two callable numbers and a fax, the shape 62% of the book
# carries. Stored spellings differ from dial digits on purpose: the reader must
# see what is stored, the dialler must get bare digits.
PHONE = '02-435-8899,081-234-5678,F:02-111-2222'
DIALS = ['024358899', '0812345678']
ADDRESS = '123 ถนนทดสอบ กรุงเทพฯ'


def _app():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    return flask_app


def _render(template, path='/', **ctx):
    """Render one template against a real request context (url_for/csrf_token
    both need a request, and every page here extends base.html)."""
    app = _app()
    with app.test_request_context(path):
        return app.jinja_env.get_template(template).render(**ctx)


def _dials(html):
    return re.findall(r'href="tel:([^"]*)"', html)


def _assert_no_comma_dial(html):
    for d in _dials(html):
        assert ',' not in d, f'comma-joined dial target: {d!r}'


# ── mobile customer screen ───────────────────────────────────────────────────

def _m_customer(**over):
    customer = {'code': 'C001', 'name': 'ร้านทดสอบ', 'zone': 'A',
                'phone': PHONE, 'address': ADDRESS,
                'credit_days': None, 'lat': None, 'lng': None,
                'salesperson': None}
    customer.update(over)
    return _render('m/customer.html', path='/m/customer/x',
                   customer_name=customer['name'], customer=customer,
                   region=None, unpaid=[], unpaid_total=0,
                   unpaid_snapshot_date=None, last_sales=[], stats={})


def test_mobile_customer_screen_offers_one_dial_target_per_number():
    """This screen carried the inline `.split(',')[0]` workaround: it dialled
    the FIRST number and silently hid the rest. ADR 0011 rejects picking a
    primary — nothing in the data says which number that is.
    """
    html = _m_customer()
    assert ADDRESS in html, 'CONTROL: the contact row never rendered'

    assert _dials(html) == DIALS
    _assert_no_comma_dial(html)
    assert '081-234-5678' in html, 'the second number is not even shown'


def test_mobile_customer_screen_labels_a_fax_and_never_dials_it():
    html = _m_customer()
    assert ADDRESS in html, 'CONTROL'
    assert 'F:02-111-2222' in html, 'the fax entry is not shown at all'
    assert 'แฟกซ์' in html
    assert '021112222' not in _dials(html)


def test_mobile_customer_screen_single_number_renders_as_before():
    """885 customers hold exactly one number. The common case must not get
    noisier for the sake of the multi-number fix."""
    html = _m_customer(phone='02-435-8899')
    assert ADDRESS in html, 'CONTROL'
    assert _dials(html) == ['024358899']
    assert '02-435-8899' in html, 'the reader must still see the stored spelling'


def test_mobile_customer_screen_marks_a_customer_with_no_number():
    """An empty phone used to render nothing, so "not recorded" and "the button
    failed to render" looked identical."""
    html = _m_customer(phone=None)
    assert ADDRESS in html, 'CONTROL'
    assert 'ไม่ได้บันทึกเบอร์' in html
    assert _dials(html) == []


def test_mobile_customer_screen_keeps_the_dial_target_for_a_glued_fax():
    """The regression the rollout nearly shipped. The deleted inline workaround
    did `.split('F:')[0]`, so it dialled a chunk that glues a fax onto a phone
    with no comma. Real stored value, customer 053ช22 — 7 customers were
    affected, and #462 rules out exactly this ("no behaviour regression on that
    screen"). Fixed in `filters._split_inline_fax`, pinned here at the surface
    because that is where the loss would have been felt.
    """
    html = _m_customer(phone='053-295633-7 F:053-295638')
    assert ADDRESS in html, 'CONTROL'
    assert _dials(html) == ['053295633']
    assert 'แฟกซ์' in html, 'the fax half is not shown and labelled'


# ── mobile sales-trip screen ─────────────────────────────────────────────────

def _sales_trip(**over):
    cust = {'code': 'C001', 'name': 'ร้านทดสอบทริป', 'region': 'ตะวันออก',
            'region_id': 1, 'last_sale': '2026-09-01', 'phone': PHONE,
            'outstanding': 0}
    cust.update(over)
    return _render('m/sales_trip.html', path='/m/sales-trip',
                   grouped={1: [cust]}, all_regions=[], region_id=None,
                   total_outstanding=0)


def test_sales_trip_row_shows_every_number_not_just_the_first():
    """The row is itself an <a> to the customer screen, so the numbers are shown
    rather than made tappable — a nested <a href="tel:"> is invalid HTML and the
    row's own link is the tap target. What must not survive is the old
    `.split(',')[0]`, which showed one number and hid the rest.
    """
    html = _sales_trip()
    assert 'ร้านทดสอบทริป' in html, 'CONTROL: the trip row never rendered'

    assert '02-435-8899' in html
    assert '081-234-5678' in html, 'only the first number is shown'
    assert PHONE not in html, 'the raw comma-joined blob reached the page'


def test_sales_trip_row_marks_a_fax_as_a_fax():
    html = _sales_trip()
    assert 'ร้านทดสอบทริป' in html, 'CONTROL'
    assert 'F:02-111-2222' in html
    assert 'แฟกซ์' in html


def test_sales_trip_row_with_no_number_stays_quiet():
    """A trip list is scanned, not read. A customer with no number recorded gets
    no phone chip at all here — the explicit marker belongs on the screens that
    show one customer, not on every row of a list."""
    html = _sales_trip(phone=None)
    assert 'ร้านทดสอบทริป' in html, 'CONTROL'
    assert 'bi-telephone' not in html


# ── call list ────────────────────────────────────────────────────────────────

def _call_list(**over):
    import call_card as cc
    row = {'customer_code': 'C001', 'name': 'ร้านทดสอบโทร', 'province': 'เชียงใหม่',
           'region': 'เหนือ', 'last_buy': '2026-02-09', 'spend': 105524.0,
           'call_status': 'never', 'call_days': None, 'last_called': None,
           'phone': PHONE, 'badges': {'ar': 0, 'quiet': False, 'special': False}}
    row.update(over)
    return _render('call/list.html', path='/call', rows=[row],
                   regions=['เหนือ'], salespersons=[{'code': '00', 'name': 'บริษัท /00'}],
                   args={}, spend_window='1y',
                   elapsed_th=cc.elapsed_th, status_label=cc.STATUS_LABEL)


def _phone_cell(html):
    """The rendered <td class="cc-phone"> only — asserting over the whole page
    would let a number match somewhere else entirely."""
    m = re.search(r'<td class="cc-phone">(.*?)</td>', html, re.DOTALL)
    assert m, 'CONTROL: the phone cell never rendered'
    return m.group(1)


def test_call_list_breaks_the_blob_into_one_number_per_line():
    """This cell used to print the stored column verbatim: a reader scanning the
    list to pick who to ring got `02-4178295,01-643-4024 02-4178287,089-2032484`
    as one run of characters."""
    cell = _phone_cell(_call_list())
    assert '02-435-8899' in cell
    assert '081-234-5678' in cell
    assert PHONE not in cell, 'the raw comma-joined blob still reaches the page'


def test_call_list_marks_a_fax():
    cell = _phone_cell(_call_list())
    assert 'F:02-111-2222' in cell
    assert 'แฟกซ์' in cell


def test_call_list_offers_no_dial_target_of_its_own():
    """Deliberate: the row is a click target that opens the call card, and a
    tel: link inside it would fire the dialler AND the navigation on one tap.
    The card one click away is where the dial buttons live (#461)."""
    html = _call_list()
    # CONTROL first: an empty `_dials` is also what a page that never rendered
    # returns, so assert the numbers ARE on the page before asserting they are
    # not links.
    assert '081-234-5678' in _phone_cell(html)
    assert _dials(html) == []


def test_call_list_shows_a_dash_when_no_number_is_recorded():
    assert _phone_cell(_call_list(phone=None)).strip() == '—'


# ── customer summary (desktop customer page) ─────────────────────────────────

def _summary(**over):
    ci = {'code': 'C001', 'name': 'ร้านทดสอบสรุป', 'customer_type': 'ร้านค้า',
          'phone': PHONE, 'contact': 'คุณทดสอบ', 'tax_id': None}
    ci.update(over)
    return _render('customer_summary.html', path='/customer/code/C001',
                   data={'customer': 'ร้านทดสอบสรุป', 'customer_code': 'C001',
                         'customer_info': ci, 'exists': True,
                         'summary': {'doc_count': 0, 'first_date': None,
                                     'last_date': None, 'total_net': 0,
                                     'total_qty': 0},
                         'docs': [], 'monthly': [], 'top_products': [],
                         'date_from': None, 'date_to': None,
                         'region': None, 'region_code': None,
                         'salesperson': None, 'salesperson_orphan': False},
                   audit_history=[], unpaid_bills=[], unpaid_total=0,
                   unpaid_snapshot_date=None, master={'code': 'C001'},
                   salespersons=[], regions=[], orphan_codes=[],
                   is_manager=True)


def _summary_entries(html):
    """The rendered phone entries only. The edit-form input holds the RAW
    column (deliberately — it is a write path), so asserting over the whole
    page would let that input satisfy an assertion about the display."""
    return re.findall(r'class="[^"]*phone-entry[^"]*"[^>]*>(.*?)</div>',
                      html, re.DOTALL)


def test_customer_summary_gives_one_dial_target_per_number():
    html = _summary()
    assert 'คุณทดสอบ' in html, 'CONTROL: the contact panel never rendered'
    assert _dials(html) == DIALS
    _assert_no_comma_dial(html)


def test_customer_summary_shows_the_fax_without_offering_it_as_a_call():
    html = _summary()
    assert 'คุณทดสอบ' in html, 'CONTROL'
    entries = _summary_entries(html)
    assert len(entries) == 3, entries          # count first, then the property
    assert 'F:02-111-2222' in entries[2]
    assert 'แฟกซ์' in entries[2]
    assert 'tel:' not in entries[2]


def test_customer_summary_single_number_renders_as_before():
    html = _summary(phone='02-435-8899')
    assert 'คุณทดสอบ' in html, 'CONTROL'
    entries = _summary_entries(html)
    assert len(entries) == 1
    assert '02-435-8899' in entries[0]


def test_customer_summary_leaves_the_edit_form_holding_the_stored_value():
    """The edit box is a WRITE path: it must carry the column exactly as stored,
    or saving the form would write the split back into the DB (ADR 0011 keeps
    the column comma-joined)."""
    html = _summary()
    m = re.search(r'<input[^>]*name="phone"[^>]*value="([^"]*)"', html)
    assert m, 'CONTROL: the edit form never rendered'
    assert m.group(1) == PHONE


# ── customer map popup ───────────────────────────────────────────────────────
#
# The popup is assembled in JavaScript from a JSON array, so the split cannot
# happen in a Jinja pipe. The ROUTE runs the same filter and ships its output,
# which is why `partners.py` is named in the sweep's positive control.

def test_route_attaches_phone_entries_to_every_mapped_customer():
    from blueprints.partners import _with_phone_entries

    out = _with_phone_entries([{'code': 'C001', 'phone': PHONE},
                               {'code': 'C002', 'phone': None}])
    assert [c['code'] for c in out] == ['C001', 'C002'], 'CONTROL: rows were dropped'
    assert [e['dial'] for e in out[0]['phone_entries']] == DIALS + [None]
    assert out[0]['phone_entries'][2]['is_fax'] is True
    assert out[1]['phone_entries'] == []


def test_route_leaves_the_original_rows_untouched():
    """The popup gets a new key; nothing about the caller's rows changes. A
    mutating version would silently rewrite whatever else holds those dicts."""
    from blueprints.partners import _with_phone_entries

    rows = [{'code': 'C001', 'phone': PHONE}]
    _with_phone_entries(rows)
    assert rows == [{'code': 'C001', 'phone': PHONE}]


def _map_popup(**over):
    cust = {'code': 'C001', 'name': 'ร้านทดสอบแผนที่', 'zone': 'A',
            'customer_type': 'ร้านค้า', 'address': ADDRESS, 'lat': 13.7,
            'lng': 100.5, 'credit_days': None, 'phone': PHONE}
    cust.update(over)
    from blueprints.partners import _with_phone_entries
    return _render('customer_map.html', path='/customers/map',
                   customers_json=_with_phone_entries([cust]),
                   zones=[], ctypes=[], sel_zone='', sel_type='',
                   total=1, geocoded=1)


def test_map_popup_builds_its_dial_targets_from_the_shared_entries():
    """A JS surface can drift from the Jinja ones without anything noticing, so
    pin that the popup reads `phone_entries` and dials `e.dial` — never the
    stored field."""
    html = _map_popup()
    # `| tojson` escapes Thai to \uXXXX, so the name is not in the page as
    # typed — control on the ASCII code, which is.
    assert 'C001' in html, 'CONTROL: the customer never reached the page'

    popup = html.split('bindPopup', 1)[1].split('markers.addLayer', 1)[0]
    assert 'phone_entries' in popup
    assert 'tel:${' in popup and 'dial' in popup
    assert '${c.phone}' not in popup, 'the raw column is still interpolated'


def test_map_data_carries_the_split_entries_not_just_the_raw_column():
    html = _map_popup()
    data = html.split('const ALL_CUSTOMERS = ', 1)[1].split(';\n', 1)[0]
    assert '024358899' in data, 'the dial digits never reached the page'
    assert 'is_fax' in data


# ── contact-review screens ───────────────────────────────────────────────────

def _review_detail(**over):
    review = {'proposed_name': 'ร้านทดสอบตรวจ', 'proposed_nickname': None,
              'proposed_phone': '02-435-8899,081-234-5678',
              'proposed_fax': None, 'proposed_contact': None,
              'proposed_address': None, 'proposed_note': None,
              'confidence': 'auto', 'status': 'pending'}
    review.update(over.pop('review', {}))
    ctx = {'customer_code': 'C001', 'review': review,
           'orig': {'name': 'ร้านทดสอบตรวจ', 'phone': PHONE,
                    'contact': 'คุณตรวจสอบ', 'address': ADDRESS},
           'live': {'name': 'ร้านทดสอบตรวจ', 'phone': PHONE, 'fax': None,
                    'nickname': None, 'contact': 'คุณตรวจสอบ', 'address': ADDRESS},
           'issues': []}
    ctx.update(over)
    return _render('customer_review/detail.html',
                   path='/customer-review/C001', **ctx)


def test_review_detail_renders_both_sides_through_the_shared_entries():
    """The reviewer compares stored against proposed. Both sides go through the
    same rendering, or the comparison is between two different conventions."""
    html = _review_detail()
    assert 'คุณตรวจสอบ' in html, 'CONTROL: the review panels never rendered'

    entries = re.findall(r'class="[^"]*phone-entry[^"]*"[^>]*>(.*?)</div>',
                         html, re.DOTALL)
    # orig (3) + live (3). The proposed value's only rendering is the edit
    # input, which must keep the raw column — asserted by the next test.
    assert len(entries) == 6, entries
    assert PHONE not in html.split('<form', 1)[0], \
        'the raw blob is still shown outside the edit form'


def test_review_detail_edit_box_still_holds_the_value_being_reviewed():
    """The proposed value is what gets WRITTEN on confirm, so its input must
    carry the exact string — split for reading, stored as typed."""
    html = _review_detail()
    m = re.search(r'<input[^>]*name="proposed_phone"[^>]*value="([^"]*)"', html)
    assert m, 'CONTROL: the confirm form never rendered'
    assert m.group(1) == '02-435-8899,081-234-5678'


def _review_list(**over):
    row = {'id': 1, 'customer_code': 'C001', 'live_name': 'ร้านทดสอบลิสต์',
           'proposed_name': 'ร้านทดสอบลิสต์', 'proposed_fax': None,
           'proposed_note': None, 'orig_phone': PHONE,
           'proposed_phone': '02-435-8899,081-234-5678', 'issues': [],
           'confidence': 'auto', 'status': 'pending', 'reviewed_at': None}
    row.update(over)
    return _render('customer_review/list.html', path='/customer-review',
                   rows=[row], status_filter='pending', q='',
                   counts={'pending': 1, 'applied': 0, 'confirmed': 0,
                           'skipped': 0},
                   args={})


def test_review_list_shows_both_columns_one_number_per_line():
    html = _review_list()
    assert 'ร้านทดสอบลิสต์' in html, 'CONTROL: the row never rendered'
    assert PHONE not in html, 'the raw comma-joined blob still reaches the page'
    assert '02-435-8899' in html and 'F:02-111-2222' in html


def test_map_popup_javascript_actually_produces_the_dial_targets():
    """The popup is the one surface pytest cannot execute: it is a JS template
    literal with nested literals and two ternaries, and a Jinja render only
    proves the SOURCE reached the page. Run it.

    ⚠ Skips without node, so it is a bonus rather than the floor — the
    assertions above (route output + rendered source) run everywhere.
    """
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which('node')
    if node is None:
        import pytest
        pytest.skip('node not available — the source-level assertions still ran')

    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'inventory_app', 'templates', 'customer_map.html'),
               encoding='utf-8').read()
    start = src.index('m.bindPopup(`') + len('m.bindPopup(')
    literal = src[start:src.index('`);', start) + 1]
    assert '{{' not in literal and '{%' not in literal, \
        'the popup literal now carries Jinja — this extraction no longer holds'

    entries = [{'text': '02-435-8899', 'dial': '024358899', 'is_fax': False},
               {'text': '081-234-5678', 'dial': '0812345678', 'is_fax': False},
               {'text': 'F:02-111-2222', 'dial': None, 'is_fax': True}]
    script = textwrap.dedent('''
        const color = '#e74c3c';
        const render = c => %s;
        const out = render(%s);
        console.log(JSON.stringify({
          dials: [...out.matchAll(/href="tel:([^"]*)"/g)].map(m => m[1]),
          fax: out.includes('F:02-111-2222') && out.includes('แฟกซ์'),
          texts: out.includes('02-435-8899') && out.includes('081-234-5678'),
          no_phone_key: !render({code: 'C002'}).includes('tel:'),
        }));
    ''') % (literal, json.dumps({'name': 'ร้านทดสอบ', 'code': 'C001', 'zone': 'A',
                                 'customer_type': 'ร้านค้า', 'address': ADDRESS,
                                 'credit_days': None, 'phone_entries': entries}))
    proc = subprocess.run([node, '-e', script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)

    assert got['texts'], 'CONTROL: the fixture never reached the popup'
    assert got['dials'] == DIALS
    assert got['fax'], 'the fax is not shown and labelled'
    assert got['no_phone_key'], \
        'a customer without the key at all breaks the popup instead of skipping it'
