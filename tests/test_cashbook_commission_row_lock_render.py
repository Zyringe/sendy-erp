"""account_ledger.html renders a commission-linked row locked (lock icon +
link to the commission payouts page, no edit/delete) — same treatment as the
pre-existing salary/advance/payout rows (issue #540). Server guards
(`_reject_if_commission_row`) are already correct and out of scope; this file
only pins the RENDER surface.

Assertions are scoped to each row's OWN `<tr>`/modal fragment, never a
whole-page substring (.claude/rules/verification-discipline.md — the page
script and sibling rows carry the same strings), with a manual row in the
SAME response as a positive control that edit/delete buttons render at all.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest

import database
import commission as commission_mod

SP_CODE = 'RENDER-TEST-SP'
SP_NAME = 'ทดสอบเรนเดอร์ /99'


@pytest.fixture
def conn(tmp_db):
    database.init_db()
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # Force the state: don't rely on the cloned dev DB already holding a
    # commission-linked row or this salesperson code.
    c.execute("DELETE FROM cashbook_transactions WHERE commission_payout_id IS NOT NULL")
    c.execute("DELETE FROM commission_payouts WHERE salesperson_code = ?", (SP_CODE,))
    c.execute("DELETE FROM salespersons WHERE code = ?", (SP_CODE,))
    c.execute("INSERT INTO salespersons (code, name) VALUES (?, ?)", (SP_CODE, SP_NAME))
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _client_as(role, tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _account_id(conn, code='392'):
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()
    assert row is not None, f"expected seeded cashbook_accounts row code={code}"
    return row['id']


def _row_fragment(html, marker):
    """The single <tr>...</tr> block containing `marker`, scoped so the
    assertion can't accidentally match a sibling row or the page's own
    <script> (which repeats the same icon classes/hooks)."""
    rows = re.findall(r'<tr\b.*?</tr>', html, re.DOTALL)
    hits = [r for r in rows if marker in r]
    assert len(hits) == 1, f"expected exactly one row containing {marker!r}, found {len(hits)}"
    return hits[0]


def _seed_commission_row(conn):
    """Real write-back path (commission.record_payout with account_id set —
    same call the /commission/payout route makes), so the fixture matches a
    real linked row byte-for-byte, not a hand-typed approximation."""
    account_id = _account_id(conn)
    payout_id = commission_mod.record_payout(
        year_month='2026-07', salesperson_code=SP_CODE, amount_paid=1234.0,
        paid_date='2026-07-05', paid_method='cash', paid_by='test-admin',
        account_id=account_id, conn=conn,
    )
    conn.commit()
    marker = f'ค่าคอมมิชชั่น 2026-07 — {SP_NAME}'
    txn_id = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchone()['id']
    return account_id, txn_id, marker


def test_commission_row_shows_lock_not_edit_delete_manual_row_still_does(conn, tmp_db):
    account_id, txn_id, commission_marker = _seed_commission_row(conn)
    # Control row: SAME category ('จ่ายค่าคอมมิชชั่น') and a description that
    # also contains 'คอมมิชชั่น' — mirrors a real prod row (id 763, manual,
    # commission_payout_id NULL). A row that only differed in category/text
    # would let an implementation keyed on category or description text pass
    # this test for the wrong reason; this control only passes when the
    # branch keys on the commission_payout_id FK, like the real guard does.
    manual_marker = 'RENDER-TEST-MANUAL-ROW-540'
    manual_description = f'ค่าคอมมิชชั่น (คีย์มือ) {manual_marker}'
    cur = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, '2026-07-06', 'expense', 'จ่ายค่าคอมมิชชั่น', 111, ?, 'พุธ')",
        (account_id, manual_description),
    )
    manual_id = cur.lastrowid
    conn.commit()

    html = _client_as('admin', tmp_db).get(
        f'/cashbook/account/{account_id}?month=2026-07'
    ).get_data(as_text=True)

    commission_row = _row_fragment(html, commission_marker)
    assert 'bi-lock-fill' in commission_row
    assert 'ยกเลิกได้ที่หน้าคอมมิชชั่นเท่านั้น' in commission_row
    assert f'href="/commission/payouts"' in commission_row
    assert 'bi-pencil' not in commission_row, "a commission row must not offer an edit button"
    assert 'bi-trash' not in commission_row, "a commission row must not offer a delete button"
    # The edit modal for this row must not be emitted at all, not merely hidden.
    assert f'id="editTxnModal{txn_id}"' not in html

    manual_row = _row_fragment(html, manual_marker)
    assert 'bi-pencil' in manual_row and 'bi-trash' in manual_row, (
        "control: a manual row (same category + 'คอมมิชชั่น' in its description, "
        "but commission_payout_id NULL) must still show edit/delete — proves the "
        "branch keys on the FK, not on category/description text")
    assert 'bi-lock-fill' not in manual_row
    # Positive control for the txn_id absence-check above: modals DO render
    # for a non-commission row on this same page, so an implementation that
    # dropped the WHOLE modal-include block (e.g. `{% if false %}`) — which
    # would trivially satisfy "editTxnModal{txn_id} not in html" — is caught.
    assert f'id="editTxnModal{manual_id}"' in html


def test_commission_link_visible_to_manager(conn, tmp_db):
    """The commission-payouts link has no role gate of its own beyond
    can_edit_cashbook — pin it for a second cashbook role, not just admin.
    (See test_commission_payouts_page_open_to_every_cashbook_role below for
    the direct route-level check this template check alone can't provide.)"""
    account_id, _txn_id, commission_marker = _seed_commission_row(conn)

    html = _client_as('manager', tmp_db).get(
        f'/cashbook/account/{account_id}?month=2026-07'
    ).get_data(as_text=True)

    commission_row = _row_fragment(html, commission_marker)
    assert f'href="/commission/payouts"' in commission_row


def test_commission_payouts_page_open_to_every_cashbook_role(conn, tmp_db):
    """A rendered href proves the template printed a link, not that the route
    behind it actually answers for that role — a role gate could land on
    commission.commission_payouts_list itself while the template link stays
    unconditional, and the previous test alone would not catch it. Drive the
    route directly for every role that can see the lock+link on the cashbook
    page (admin/manager/shareholder — can_edit_cashbook, access_control.py)."""
    for role in ('admin', 'manager', 'shareholder'):
        resp = _client_as(role, tmp_db).get('/commission/payouts')
        assert resp.status_code == 200, f"{role} could not open /commission/payouts"
