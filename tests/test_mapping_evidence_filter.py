"""A BSN code whose bills were all edited away in Express is residue, not work.

WHY THIS EXISTS
    `models/imports.py` registers a code into product_code_mapping the first
    time it appears on any imported line, and NOTHING removes that row when the
    line later disappears. The team fixes a mis-keyed code in Express, the next
    import correctly deletes the ledger row, and the placeholder sits on
    /mapping forever asking to be mapped to a product nobody is buying or
    selling.

    Measured on prod 2026-08-29: 3 pending codes, ALL THREE with zero surviving
    bills. `036ผ7000` has the full story in audit_log — INSERT of
    IV6901436-1|036ผ7000 (net ฿102) on 08-26, DELETE of the same row by
    `express_dbf:BSN5657` on 08-28. The other two are mis-keys for codes that
    really exist (999บ3400 / 999บ3300).

    The cost was not a cluttered page. `record_unmapped_bsn_codes_alert` counts
    the same rows and its message says the bills "ขายได้แต่ไม่ตัดสต็อก สต็อกจะ
    เพี้ยนขึ้นเรื่อยๆ" — false when there are no bills at all. It re-raised on
    every import and Put resolved it by hand twice.

WHAT IS DELIBERATELY NOT DONE
    The residue rows are NOT deleted and NOT marked is_ignored. `is_ignored=1`
    means "throw this line away on sight" (imports.py:223 drops the revenue and
    the stock), which is a different and much stronger statement. This is a
    read-side filter only; the row stays, and if the code ever comes back on a
    real bill it becomes pending again by itself.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import models
from models.stock_filters import NON_STOCK_BSN_CODES


def _placeholder(conn, code, created_at='2026-08-01 09:00:00'):
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id,"
        " is_ignored, created_at) VALUES (?, ?, NULL, 0, ?)",
        (code, 'ของทดสอบ ' + code, created_at))


def _sale(conn, code):
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, bsn_code, product_id)"
        " VALUES ('2026-08-20', ?, ?, NULL)", ('IV-' + code, code))


def _purchase(conn, code):
    conn.execute(
        "INSERT INTO purchase_transactions (date_iso, doc_no, bsn_code, product_id)"
        " VALUES ('2026-08-20', ?, ?, NULL)", ('PI-' + code, code))


def _credit_note(conn, code):
    conn.execute(
        "INSERT INTO credit_note_imports (doc_no, doc_base, date_iso, bsn_code)"
        " VALUES (?, ?, '2026-08-20', ?)", ('SR-' + code, 'SR-' + code, code))


def _pending_codes(conn=None):
    return {r['bsn_code'] for r in models.get_pending_mappings(conn=conn)}


# ── the filter itself ────────────────────────────────────────────────────────

def test_a_code_with_a_surviving_sale_is_still_pending(empty_db_conn):
    """CONTROL. Without this the whole file passes on a filter that hides
    everything, which is the exact failure the change could introduce."""
    _placeholder(empty_db_conn, 'LIVE1')
    _sale(empty_db_conn, 'LIVE1')
    empty_db_conn.commit()
    assert 'LIVE1' in _pending_codes(empty_db_conn)


def test_a_code_whose_bills_were_all_deleted_is_not_pending(empty_db_conn):
    _placeholder(empty_db_conn, 'GHOST1')
    empty_db_conn.commit()
    assert 'GHOST1' not in _pending_codes(empty_db_conn)


def test_a_purchase_line_alone_is_evidence(empty_db_conn):
    _placeholder(empty_db_conn, 'BUY1')
    _purchase(empty_db_conn, 'BUY1')
    empty_db_conn.commit()
    assert 'BUY1' in _pending_codes(empty_db_conn)


def test_a_credit_note_line_alone_is_evidence(empty_db_conn):
    """Asymmetric on purpose: counting it can only show a code that needs no
    action, while leaving it out could hide one that does."""
    _placeholder(empty_db_conn, 'CN1')
    _credit_note(empty_db_conn, 'CN1')
    empty_db_conn.commit()
    assert 'CN1' in _pending_codes(empty_db_conn)


def test_live_and_residue_are_separated_in_one_pass(empty_db_conn):
    _placeholder(empty_db_conn, 'LIVE1')
    _sale(empty_db_conn, 'LIVE1')
    _placeholder(empty_db_conn, 'GHOST1')
    empty_db_conn.commit()
    assert _pending_codes(empty_db_conn) == {'LIVE1'}
    assert {r['bsn_code'] for r in models.get_orphan_mappings(conn=empty_db_conn)} == {'GHOST1'}


def test_orphans_come_back_newest_first(empty_db_conn):
    """The section only ever grows, so a burst of same-day residue is the
    signal, not the total. Newest first is what makes the burst visible."""
    _placeholder(empty_db_conn, 'OLD1', '2026-07-30 17:00:48')
    _placeholder(empty_db_conn, 'NEW1', '2026-08-26 16:56:37')
    _placeholder(empty_db_conn, 'MID1', '2026-08-15 16:57:26')
    empty_db_conn.commit()
    got = [r['bsn_code'] for r in models.get_orphan_mappings(conn=empty_db_conn)]
    assert got == ['NEW1', 'MID1', 'OLD1'], got


def test_an_ignored_code_is_neither_pending_nor_residue(empty_db_conn):
    """ไม่นำเข้า is a decision that was made, not a row waiting for one."""
    _placeholder(empty_db_conn, 'IGN1')
    _sale(empty_db_conn, 'IGN1')
    empty_db_conn.execute(
        "UPDATE product_code_mapping SET is_ignored = 1 WHERE bsn_code = 'IGN1'")
    empty_db_conn.commit()
    assert 'IGN1' not in _pending_codes(empty_db_conn)
    assert 'IGN1' not in {r['bsn_code'] for r in models.get_orphan_mappings(conn=empty_db_conn)}


def test_a_code_whose_bills_are_all_linked_is_not_called_residue(empty_db_conn):
    """The section's caption has to stay true.

    Codex proposed (2026-08-29) that evidence should count only bills still
    unlinked, so a placeholder whose lines are all resolved would drop off the
    backlog. The narrower rule is defensible for the alert's wording, but it
    would put such a code under "ไม่มีบิลเหลือแล้ว" -- and its bills DO exist.
    Whatever a later refinement decides about hiding it, calling it residue is
    the one thing that must never happen.
    """
    conn = empty_db_conn
    conn.execute("INSERT INTO products (id, product_name, unit_type)"
                 " VALUES (901, 'ของทดสอบ', 'ตัว')")
    _placeholder(conn, 'LINKED1')
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, bsn_code, product_id)"
        " VALUES ('2026-08-20', 'IV-LINKED1', 'LINKED1', 901)")
    conn.commit()

    residue = {r['bsn_code'] for r in models.get_orphan_mappings(conn=conn)}
    assert 'LINKED1' not in residue, 'a code with surviving bills is not residue'
    # CONTROL: the fixture really does reach the residue path for a code that
    # has no bills, so the assertion above is not passing on an empty list.
    _placeholder(conn, 'GHOST1')
    conn.commit()
    assert 'GHOST1' in {r['bsn_code'] for r in models.get_orphan_mappings(conn=conn)}


# ── the independent-direction guard ──────────────────────────────────────────

def test_every_unlinked_ledger_code_is_reachable_from_the_page(empty_db_conn):
    """Derived from the BILLS, not from the mapping table.

    The filter's failure mode is classifying a live code as residue, which
    hides it from /mapping AND drops it from the alert at the same time — both
    signals go dark together because they share one definition. This assertion
    shares none of it: it asks the ledger which codes still cannot deduct
    stock, and demands every one of them be visible.
    """
    for code in ('LIVE1', 'BUY1'):
        _placeholder(empty_db_conn, code)
    _sale(empty_db_conn, 'LIVE1')
    _purchase(empty_db_conn, 'BUY1')
    _placeholder(empty_db_conn, 'GHOST1')          # residue, must NOT be required
    ns = sorted(NON_STOCK_BSN_CODES)[0]
    _placeholder(empty_db_conn, ns)                # ค่าขนส่ง/ส่วนลด, never a product
    _sale(empty_db_conn, ns)
    empty_db_conn.commit()

    unlinked = set()
    for table in ('sales_transactions', 'purchase_transactions'):
        unlinked |= {r[0] for r in empty_db_conn.execute(
            f"SELECT DISTINCT bsn_code FROM {table}"
            f" WHERE product_id IS NULL AND bsn_code IS NOT NULL")}
    unlinked -= set(NON_STOCK_BSN_CODES)

    assert unlinked == {'LIVE1', 'BUY1'}, unlinked          # control on the control
    assert unlinked <= _pending_codes(empty_db_conn)


# ── the alert reads the same definition ──────────────────────────────────────

def _open_alerts(conn):
    from models import system_alerts as sa
    return conn.execute(
        "SELECT id, message FROM system_alerts WHERE kind = ? AND resolved_at IS NULL",
        (sa.KIND_UNMAPPED_CODES,)).fetchall()


def test_the_alert_does_not_fire_for_residue_alone(empty_db_conn):
    _placeholder(empty_db_conn, 'GHOST1')
    _placeholder(empty_db_conn, 'GHOST2')
    empty_db_conn.commit()
    models.record_unmapped_bsn_codes_alert(
        models.get_pending_mappings(conn=empty_db_conn), conn=empty_db_conn)
    empty_db_conn.commit()
    assert _open_alerts(empty_db_conn) == []


def test_the_alert_still_fires_for_a_real_backlog(empty_db_conn):
    """CONTROL for the test above: the alert must not have simply stopped."""
    _placeholder(empty_db_conn, 'LIVE1', '2026-07-30 17:00:48')
    _sale(empty_db_conn, 'LIVE1')
    _placeholder(empty_db_conn, 'GHOST1', '2026-07-01 09:00:00')
    empty_db_conn.commit()
    models.record_unmapped_bsn_codes_alert(
        models.get_pending_mappings(conn=empty_db_conn), conn=empty_db_conn)
    empty_db_conn.commit()
    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1, [dict(r) for r in rows]
    assert '1' in rows[0]['message']
    # the residue must not drag the "oldest" date backwards either
    assert '2026-07-30' in rows[0]['message'], rows[0]['message']
    assert '2026-07-01' not in rows[0]['message']
