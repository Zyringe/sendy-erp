"""Unmapped BSN codes accumulate silently -- raise them on /alerts (plan F6).

A BSN code with no product behind it is registered into product_code_mapping
and then nothing happens. The import reports success, the ledger stays
internally consistent, and the sales on that code simply never deduct stock.
Measured on prod 2026-08-17: 7 open, oldest 2026-07-30, with ฿5,804 of the
08-15 sales not moving stock. (Re-measured 08-19: 8, and one of them arrived
on that morning's import -- it accumulates exactly as described.)

Unlike staleness, this event genuinely exists at import time, so the importer
raises it directly -- no request hook needed. It clears itself once the backlog
is empty, for the same reason the staleness one does: an alert that only a
human click can retire is one people learn to click past.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import models
from models import system_alerts as sa


def _unmapped(conn, code, created_at, ignored=0):
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id,"
        " is_ignored, created_at) VALUES (?, ?, NULL, ?, ?)",
        (code, 'ของทดสอบ ' + code, ignored, created_at))
    conn.commit()


def _mapped(conn, code):
    conn.execute("INSERT INTO products (id, product_name, unit_type)"
                 " VALUES (777, 'x', 'ตัว')")
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id,"
        " is_ignored, created_at) VALUES (?, 'y', 777, 0, '2026-08-01')", (code,))
    conn.commit()


def _open_alerts(conn):
    return conn.execute(
        "SELECT id, message, context_json FROM system_alerts"
        " WHERE kind = ? AND resolved_at IS NULL",
        (sa.KIND_UNMAPPED_CODES,)).fetchall()


def test_unmapped_codes_raise_one_alert_carrying_count_and_oldest(empty_db_conn):
    _unmapped(empty_db_conn, 'AAA1', '2026-07-30 17:00:48')
    _unmapped(empty_db_conn, 'BBB2', '2026-08-15 16:57:26')
    models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1, [dict(r) for r in rows]
    assert '2' in rows[0]['message']
    assert '2026-07-30' in rows[0]['message']


def test_repeat_imports_hold_one_alert_not_many(empty_db_conn):
    _unmapped(empty_db_conn, 'AAA1', '2026-07-30 17:00:48')
    for _ in range(4):
        models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()
    assert len(_open_alerts(empty_db_conn)) == 1


def test_a_mapped_code_does_not_count(empty_db_conn):
    _mapped(empty_db_conn, 'MAP1')
    models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()
    assert _open_alerts(empty_db_conn) == []


def test_an_ignored_code_does_not_count(empty_db_conn):
    """ไม่นำเข้า is a deliberate decision, not a backlog item -- and it has its
    own alert (record_ignored_import_lines_alert) with a different message."""
    _unmapped(empty_db_conn, 'IGN1', '2026-07-30 17:00:48', ignored=1)
    models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()
    assert _open_alerts(empty_db_conn) == []


def test_clearing_the_backlog_resolves_the_alert(empty_db_conn):
    _unmapped(empty_db_conn, 'AAA1', '2026-07-30 17:00:48')
    models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()
    assert len(_open_alerts(empty_db_conn)) == 1        # control

    empty_db_conn.execute("DELETE FROM product_code_mapping WHERE bsn_code='AAA1'")
    empty_db_conn.commit()
    models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()
    assert _open_alerts(empty_db_conn) == []
    row = empty_db_conn.execute(
        "SELECT resolved_by FROM system_alerts WHERE kind = ?",
        (sa.KIND_UNMAPPED_CODES,)).fetchone()
    assert 'auto' in (row['resolved_by'] or '')


def test_an_open_alert_refreshes_its_count_instead_of_going_stale(empty_db_conn):
    """Codex round 7: dedupe must not mean the number freezes at raise time.

    A second incident would be wrong (one backlog, one alert), but so is an
    alert that still says 1 when 12 codes are waiting. The open row is UPDATED
    in place -- same alert, current facts."""
    import json
    _unmapped(empty_db_conn, 'AAA1', '2026-07-30 17:00:48')
    models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()
    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1 and '1' in rows[0]['message']         # control
    first_id = rows[0]['id']

    for i in range(2, 6):
        _unmapped(empty_db_conn, f'BBB{i}', '2026-08-19 17:08:04')
    models.record_unmapped_bsn_codes_alert(conn=empty_db_conn)
    empty_db_conn.commit()

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1, [dict(r) for r in rows]              # still ONE
    assert rows[0]['id'] == first_id, 'must update in place, not re-raise'
    assert '5' in rows[0]['message'], rows[0]['message']
    assert json.loads(rows[0]['context_json'])['count'] == 5
    # the oldest must not drift forward as newer codes arrive
    assert '2026-07-30' in rows[0]['message']
