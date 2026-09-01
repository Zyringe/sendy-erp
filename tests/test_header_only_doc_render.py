"""A credit note with NO product lines must not read as an unmapped product.

Express keeps 90 all-time `SR` credit notes whose `ARTRN` header exists with
zero `STCRD` detail lines (SR6700176 is one; 3 of the 90 fall inside Sendy's
import window). `parse_weekly.parse_credit_notes` deliberately emits ONE
placeholder row for each so the document is still tracked, with
`bsn_code = None` as its marker and `product_name_raw = None` because
`STCRD.STKDES` genuinely does not exist (MAPPING.md).

`get_sales_by_doc` then yields `display_name = COALESCE(p.product_name,
s.product_name_raw)` = NULL, and Jinja stringifies None, so `/sales/doc/<SR>`
and `/sales` both rendered the literal text **None** inside the yellow
`text-warning` "this product is not mapped yet" branch. That branch is wrong
twice over: there is no product to map, and no BSN code to map it with.

Third neutral state instead, exactly like the `ไม่นับสต็อก` badge already in
`sales_doc.html`. The discriminator is **`bsn_code`**, never the display name:
a real line can carry a code whose name is blank, and that one MUST keep its
warning. Verified on prod 2026-09-01: `bsn_code` is NULL on 3 of 20,259 rows,
empty-string on 0, and no row has a missing code but a present name.

FIXTURE DISCIPLINE: `tmp_db` clones the live dev DB *with its data*, and the
real SR6700176 row is in it — a test that simply GETs that document would pass
on inherited state. Every row read here is INSERTed under a doc_base asserted
absent first, and the CONTROL line (a genuine unmapped code) is what proves the
change did not swallow the warning it was meant to keep.
"""
import sqlite3

import pytest

SALES_DOC = 'SRHEADERONLYRENDER1'
CONTROL_CODE = 'HDRONLY-UNMAPPED'          # a real code, deliberately unmapped
CONTROL_NAME = 'สินค้าที่ยังไม่ผูก'

WARN_TAG = '<i class="bi bi-exclamation-triangle me-1"></i>'
NO_LINES_CELL = '>ไม่มีรายการสินค้า<'
NO_LINES_BADGE = '>ไม่มีรายการ<'
WAITING_BADGE = '>รอ<'


@pytest.fixture
def admin_client(tmp_db):
    """Authed admin test client. Session injection, NOT a real login: this
    machine's Python has no hashlib.scrypt, so a real HTTP login against a
    scrypt-hashed user throws. Same pattern as tests/test_nonstock_doc_badge.py."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


@pytest.fixture
def seeded(tmp_db):
    """Two lines under one doc: a genuine unmapped line, and the placeholder.

    Raises if the doc_base is not clean, so a colliding fixture fails loudly
    instead of asserting against inherited rows.
    """
    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE doc_no LIKE ?",
            (SALES_DOC + '-%',)).fetchone()[0]
        assert existing == 0, (
            'sales_transactions already holds rows for {} — fixture would be '
            'inheriting state'.format(SALES_DOC))
        conn.executemany(
            "INSERT INTO sales_transactions (date_iso, doc_no, product_id,"
            " bsn_code, product_name_raw, customer, qty, unit, unit_price,"
            " vat_type, total, net, synced_to_stock)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                # CONTROL: a real BSN code, no product mapped yet → must KEEP
                # the yellow warning and the grey 'รอ' badge.
                ('2025-11-10', SALES_DOC + '-1', None, CONTROL_CODE,
                 CONTROL_NAME, 'ลูกค้าทดสอบ', 2, 'ตัว', 50.0, 1, 100.0, 100.0, 0),
                # The header-only placeholder: no code, no name, no product.
                ('2025-11-10', SALES_DOC + '-2', None, None,
                 None, 'ลูกค้าทดสอบ', 0, '', 0.0, 1, 0.0, 0.0, 0),
            ])
        conn.commit()
        seeded = conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE doc_no LIKE ?",
            (SALES_DOC + '-%',)).fetchone()[0]
        assert seeded == 2, 'seeded {} rows, expected 2'.format(seeded)
    finally:
        conn.close()


def _doc_html(admin_client):
    resp = admin_client.get('/sales/doc/' + SALES_DOC)
    assert resp.status_code == 200, 'doc page returned {}'.format(resp.status_code)
    return resp.get_data(as_text=True)


def _list_html(admin_client):
    resp = admin_client.get(
        '/sales?date_from=2025-11-01&date_to=2025-11-30&doc_no=' + SALES_DOC)
    assert resp.status_code == 200, 'list page returned {}'.format(resp.status_code)
    return resp.get_data(as_text=True)


# ── /sales/doc/<doc_base> ────────────────────────────────────────────────────

def test_doc_control_line_keeps_its_unmapped_warning(admin_client, seeded):
    """The control must render the warning — if it does not, every assertion
    below about the placeholder is measuring an empty table, not the fix."""
    html = _doc_html(admin_client)
    assert html.count(WARN_TAG + CONTROL_NAME) == 1
    assert html.count(WAITING_BADGE) == 1


def test_doc_placeholder_reads_as_no_line_items_not_as_unmapped(admin_client, seeded):
    html = _doc_html(admin_client)
    assert html.count(NO_LINES_CELL) == 1
    assert html.count(NO_LINES_BADGE) == 1
    # Exactly one product-cell warning on the page: the control's. The
    # placeholder's cell must not have produced a second one. Count WARN_TAG
    # (the me-1 variant), not the bare icon class — the nav's แจ้งเตือน link
    # uses the same icon with me-3 and would make the count 2 either way.
    assert html.count(WARN_TAG) == 1


def test_doc_never_renders_the_literal_string_none(admin_client, seeded):
    """Jinja stringifies None. Both the สินค้า cell and the รหัส BSN cell
    printed it before the fix."""
    html = _doc_html(admin_client)
    assert '"></i>None' not in html          # สินค้า cell
    assert '>None</td>' not in html          # รหัส BSN cell
    assert html.count('<td class="font-mono small text-subtle">–</td>') == 1


# ── /sales list ──────────────────────────────────────────────────────────────

def test_list_control_line_keeps_its_unmapped_warning(admin_client, seeded):
    html = _list_html(admin_client)
    assert html.count(WARN_TAG + CONTROL_NAME) == 1


def test_list_placeholder_reads_as_no_line_items_not_as_unmapped(admin_client, seeded):
    html = _list_html(admin_client)
    assert html.count(NO_LINES_CELL) == 1
    assert html.count(WARN_TAG) == 1
    assert '"></i>None' not in html
