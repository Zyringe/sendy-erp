"""account_ledger.html renders a payout-sourced row locked (lock icon + link
to the marketplace page instead of edit/delete buttons) — same treatment as
the pre-existing salary/advance rows (issue #533). Assertions are scoped to
each row's OWN table cell (never a whole-page substring — see
.claude/rules/verification-discipline.md on why a page-wide check can pass
vacuously) with a manual row in the SAME response as a positive control that
edit/delete buttons render at all.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest

import database
import cashbook_payout_mirror as mirror


@pytest.fixture
def conn(tmp_db):
    database.init_db()
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("DELETE FROM marketplace_payouts")
    c.execute("DELETE FROM cashbook_transactions WHERE payout_platform IS NOT NULL")
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


def _row_fragment(html, marker):
    """The single <tr>...</tr> block containing `marker` (a unique
    description string), scoped so the assertion can't accidentally match a
    sibling row or a <script>/comment elsewhere on the page."""
    rows = re.findall(r'<tr\b.*?</tr>', html, re.DOTALL)
    hits = [r for r in rows if marker in r]
    assert len(hits) == 1, f"expected exactly one row containing {marker!r}, found {len(hits)}"
    return hits[0]


def test_payout_row_shows_lock_not_edit_delete_manual_row_still_does(conn, tmp_db):
    spx = conn.execute("SELECT id FROM cashbook_accounts WHERE code='SPX'").fetchone()['id']
    manual_marker = 'RENDER-TEST-MANUAL-ROW'
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, '2026-03-01', 'income', 'ยอดขายของ', 111, ?, 'พุธ')",
        (spx, manual_marker),
    )
    conn.execute(
        "INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders, status)"
        " VALUES ('shopee', '2026-03-10', 3596.00, 12, 'reconciled')"
    )
    conn.commit()
    mirror.mirror_platform(conn, 'shopee')
    payout_marker = 'Shopee โอนเงิน (12 ออเดอร์)'

    html = _client_as('admin', tmp_db).get(f'/cashbook/account/{spx}').get_data(as_text=True)

    payout_row = _row_fragment(html, payout_marker)
    assert 'bi-lock-fill' in payout_row
    assert 'ดูที่มาร์เก็ตเพลส' in payout_row
    assert 'bi-pencil' not in payout_row, "a payout row must not offer an edit button"
    assert 'bi-trash' not in payout_row, "a payout row must not offer a delete button"

    manual_row = _row_fragment(html, manual_marker)
    assert 'bi-pencil' in manual_row and 'bi-trash' in manual_row, (
        "control: a manual row must still show edit/delete — proves the assertions "
        "above are scoped correctly and not just a page-wide absence")
    assert 'bi-lock-fill' not in manual_row
