"""TDD for Phase 4b of projects/customer-edit-card/plan.md — restore the
customer change-history card, keyed by `audit_log.row_key` (migration 150),
not `customers.rowid`.

Background: the card was written in #346 and pulled before merge because
`get_customer_audit_history` joined on `customers.rowid`, which is IMPLICIT
for this table (PK is TEXT `code`) — SQLite explicitly permits VACUUM to
renumber implicit rowids, and a renumber would re-point old audit_log rows
at whatever customer now holds that rowid, showing ANOTHER customer's
history, confidently. Migration 150 added `audit_log.row_key`, written by
the trigger at the same time as `row_id`, always equal to the business key
(`customers.code`) — stable regardless of what SQLite does with rowids.
Migration 151 additionally indexes `(table_name, row_key)` and makes `code`
itself immutable.

Covers:
  - the card renders for a customer with history and shows an old->new pair
  - it is absent for `staff` (manager+ gate)
  - it never claims WHO made the edit (audit_log.user is NULL on every row —
    the trigger is SQL-level and cannot see the session user)
  - it reads by row_key, not row_id: a synthetic audit_log row whose row_id
    points at a DIFFERENT customer's rowid, but whose row_key still names
    the original customer, must show up for the original and NOT leak into
    the other customer's page. This is the exact bug the migration exists
    for — constructed directly rather than by trying to make VACUUM
    renumber anything.
"""
import json
import os
import re
import sqlite3

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


def _customer_row(tmp_db, code):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM customers WHERE code=?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _pick_two_customer_codes(tmp_db):
    """Any two distinct customers.code values with distinct rowids — the
    real content doesn't matter, only that both exist."""
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute("SELECT code, rowid FROM customers LIMIT 2").fetchall()
    conn.close()
    assert len(rows) == 2, 'need at least 2 customers in the live DB to run this test'
    return rows[0], rows[1]


# ── Card renders + manager gate ─────────────────────────────────────────────

def test_change_history_card_shows_the_edit_and_is_manager_gated(tmp_db):
    (code, _rowid), _other = _pick_two_customer_codes(tmp_db)
    before = _customer_row(tmp_db, code)
    c = _client(tmp_db, role='admin')
    r = c.post(f'/customer/{code}/reassign', data={
        'salesperson': before['salesperson'] or '',
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '', 'phone': '02-777-6666',
        'fax': before['fax'] or '', 'contact': before['contact'] or '',
        'address': before['address'] or '', 'contact_note': before['contact_note'] or '',
    })
    assert r.status_code == 302

    html = c.get(f'/customer/code/{quote(code)}').data.decode()
    assert 'id="customerAuditHistory"' in html
    assert '02-777-6666' in html, 'the new value must appear in the history'
    assert (before['phone'] or '—') in html, 'the old value must appear too'

    staff_html = _client(tmp_db, role='staff').get(
        f'/customer/code/{quote(code)}').data.decode()
    assert 'id="customerAuditHistory"' not in staff_html


def test_change_history_never_claims_who_made_the_edit(tmp_db):
    """audit_log.user is NULL on every customers row (SQL triggers cannot
    see the session user), so the card must not grow a 'by whom' column."""
    (code, _rowid), _other = _pick_two_customer_codes(tmp_db)
    before = _customer_row(tmp_db, code)
    c = _client(tmp_db, role='admin')
    c.post(f'/customer/{code}/reassign', data={
        'salesperson': before['salesperson'] or '',
        'region_id': str(before['region_id'] or ''),
        'nickname': before['nickname'] or '', 'phone': '02-111-2222',
        'fax': before['fax'] or '', 'contact': before['contact'] or '',
        'address': before['address'] or '', 'contact_note': before['contact_note'] or '',
    })
    html = c.get(f'/customer/code/{quote(code)}').data.decode()
    card = re.search(r'id="customerAuditHistory".*?</div>\s*</div>', html, re.S)
    assert card, 'history card not found'
    assert '<th>โดย' not in card.group(0) and '>ผู้แก้ไข<' not in card.group(0)


def test_no_history_card_absent_even_for_manager(tmp_db):
    """A customer with zero audit_log rows must not render an empty card —
    the template guards on `is_manager and audit_history`."""
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("""
        SELECT code FROM customers c
         WHERE NOT EXISTS (
             SELECT 1 FROM audit_log a
              WHERE a.table_name='customers' AND a.row_key = c.code
         )
         LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        import pytest
        pytest.skip('every customer in this live DB snapshot has audit history')
    code = row[0]
    html = _client(tmp_db, role='admin').get(
        f'/customer/code/{quote(code)}').data.decode()
    assert 'id="customerAuditHistory"' not in html


# ── The bug this migration exists for: row_key, never row_id ───────────────

def test_history_reads_by_row_key_not_rowid(tmp_db):
    """Simulate the exact failure state #346 was pulled for: a `customers`
    audit_log row whose `row_id` points at a DIFFERENT customer's rowid
    (as an implicit rowid could read after a VACUUM renumber) while
    `row_key` still correctly names the original customer. The card must
    follow row_key and show this entry ONLY on the original customer's
    page, never on the customer whose rowid the (stale/wrong) row_id
    happens to match.
    """
    (a_code, a_rowid), (b_code, b_rowid) = _pick_two_customer_codes(tmp_db)
    assert a_rowid != b_rowid

    marker_old, marker_new = 'ROWKEY-TEST-OLD', 'ROWKEY-TEST-NEW'
    conn = sqlite3.connect(tmp_db)
    conn.execute("""
        INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields)
        VALUES ('customers', ?, ?, 'UPDATE', ?)
    """, (b_rowid, a_code, json.dumps({'phone': [marker_old, marker_new]})))
    conn.commit()
    conn.close()

    import models
    a_history = models.get_customer_audit_history(a_code)
    b_history = models.get_customer_audit_history(b_code)

    def _has_marker(history):
        return any(
            ch['new'] == marker_new
            for h in history for ch in h['changes']
        )

    assert _has_marker(a_history), (
        'row_key correctly resolved this row to customer A, despite row_id '
        'pointing at customer B\'s rowid')
    assert not _has_marker(b_history), (
        'must NOT leak into customer B via the wrong/stale row_id — '
        'this is the exact misattribution bug the migration exists to prevent')


def test_history_route_reads_by_row_key_not_rowid(tmp_db):
    """Same scenario as above, exercised through the real route/template
    (not just the model function) — proves the wiring in
    blueprints/partners.py::customer_detail passes the right value through."""
    (a_code, a_rowid), (b_code, b_rowid) = _pick_two_customer_codes(tmp_db)

    marker_new = 'ROWKEY-ROUTE-TEST-NEW'
    conn = sqlite3.connect(tmp_db)
    conn.execute("""
        INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields)
        VALUES ('customers', ?, ?, 'UPDATE', ?)
    """, (b_rowid, a_code, json.dumps({'phone': ['ROWKEY-ROUTE-TEST-OLD', marker_new]})))
    conn.commit()
    conn.close()

    c = _client(tmp_db, role='admin')
    a_html = c.get(f'/customer/code/{quote(a_code)}').data.decode()
    b_html = c.get(f'/customer/code/{quote(b_code)}').data.decode()

    assert marker_new in a_html
    assert marker_new not in b_html
