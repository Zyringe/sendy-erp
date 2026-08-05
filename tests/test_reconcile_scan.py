"""reconcile-scan — detect + confirm-apply for Sendy sales docs deleted at
the Express source (models/reconcile.py). See
projects/express-integration/reconcile-scan-plan.md (rev 8) for the full
contract; test names below map to plan §5's verification list.

All tests hit a real SQLite connection (empty_db_conn — the live local
schema, zero rows) per this repo's integration-test convention. No mocks on
the DB layer.
"""
import datetime
import json
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import models.reconcile as mr

TODAY = datetime.date(2026, 7, 20)
CUTOFF = TODAY - datetime.timedelta(days=60)          # 2026-05-21
IN_WINDOW_DATE = TODAY - datetime.timedelta(days=5)    # 2026-07-15
OLD_DATE = TODAY - datetime.timedelta(days=100)        # 2026-04-11 (outside window)


# ── Fixture helpers ──────────────────────────────────────────────────────────

def _seed_product(conn, name='สินค้าทดสอบ', unit_type='ตัว'):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, 0)",
        (name, unit_type))
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    return pid


def _insert_sale(conn, *, doc_no, date_iso, product_id, bsn_code='C1',
                  customer='ลูกค้าทดสอบ', qty=1.0, unit='ตัว', unit_price=100.0,
                  net=100.0, synced=1, ref_invoice=None, batch_id=None):
    doc_base = doc_no.rsplit('-', 1)[0] if '-' in doc_no else doc_no
    cur = conn.execute("""
        INSERT INTO sales_transactions
            (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
             product_name_raw, customer, customer_code, qty, unit, unit_price,
             vat_type, discount, total, net, synced_to_stock, ref_invoice)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,'',?,?,?,?)
    """, (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
          'ชื่อสินค้า', customer, 'C001', qty, unit, unit_price,
          net, net, synced, ref_invoice))
    return cur.lastrowid


def _insert_bsn_txn(conn, *, product_id, doc_no, note='BSN ขาย',
                     quantity_change=-1.0, created_at='2026-07-15 00:00:00'):
    conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, ?, ?, 'unit', ?, ?, ?)
    """, (product_id, 'OUT' if quantity_change < 0 else 'IN',
          quantity_change, doc_no, note, created_at))


def _hdr(docnum, rectyp='3', docdat=None):
    return {'DOCNUM': docnum, 'RECTYP': rectyp, 'DOCDAT': docdat}


def _entry(doc_no):
    """Minimal build_sales_entries()-shaped dict — scan_reconcile only reads
    e['doc_no']."""
    return {'doc_no': doc_no}


def _make_flag(conn, doc_base, cls='deleted', payload_rows=None, state='open',
               resolved_by=None, resolved_at=None, resolution_note=None,
               suppression_active=0):
    if payload_rows is None:
        payload_rows = mr._payload_for_doc(conn, doc_base)
    payload_json = json.dumps({'rows': payload_rows, 'evidence': {}}, sort_keys=True)
    cur = conn.execute(
        "INSERT INTO express_reconcile_flags "
        "(doc_base, class, first_payload_json, latest_payload_json, state, "
        " resolved_by, resolved_at, resolution_note, suppression_active) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_base, cls, payload_json, payload_json, state,
         resolved_by, resolved_at, resolution_note, suppression_active))
    conn.commit()
    return cur.lastrowid


def _flag_row(conn, flag_id):
    return conn.execute(
        "SELECT * FROM express_reconcile_flags WHERE id=?", (flag_id,)).fetchone()


def _events(conn, flag_id):
    return conn.execute(
        "SELECT * FROM express_reconcile_events WHERE flag_id=? ORDER BY id",
        (flag_id,)).fetchall()


# ── Detection: one test per class ───────────────────────────────────────────

def test_present_unchanged_doc_gets_no_flag(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000001-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    counts = mr.scan_reconcile([_entry('IV1000001-1')], [_hdr('IV1000001', docdat=IN_WINDOW_DATE)],
                               CUTOFF, conn=c)
    c.commit()

    assert sum(counts.values()) == 0
    assert c.execute("SELECT COUNT(*) FROM express_reconcile_flags").fetchone()[0] == 0


def test_doc_absent_from_artrn_entirely_is_deleted(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000002-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    counts = mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()

    assert counts['deleted'] == 1
    row = c.execute(
        "SELECT class, state FROM express_reconcile_flags WHERE doc_base='IV1000002'").fetchone()
    assert row['class'] == 'deleted' and row['state'] == 'open'


def test_header_out_of_scope_rectyp(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000003-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    # RECTYP '7' (OE order) — out of _SCOPE_RECTYP ('3','1','5')
    counts = mr.scan_reconcile([], [_hdr('IV1000003', rectyp='7', docdat=IN_WINDOW_DATE)], CUTOFF, conn=c)
    c.commit()

    assert counts['out_of_scope'] == 1
    row = c.execute("SELECT class FROM express_reconcile_flags WHERE doc_base='IV1000003'").fetchone()
    assert row['class'] == 'out_of_scope'


def test_malformed_docdat_is_parse_gap(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000004-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    counts = mr.scan_reconcile([], [_hdr('IV1000004', docdat=None)], CUTOFF, conn=c)
    c.commit()

    assert counts['parse_gap'] == 1
    row = c.execute("SELECT class FROM express_reconcile_flags WHERE doc_base='IV1000004'").fetchone()
    assert row['class'] == 'parse_gap'


def test_valid_date_now_outside_cutoff_is_date_moved(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000005-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    counts = mr.scan_reconcile([], [_hdr('IV1000005', docdat=OLD_DATE)], CUTOFF, conn=c)
    c.commit()

    assert counts['date_moved'] == 1
    row = c.execute("SELECT class FROM express_reconcile_flags WHERE doc_base='IV1000005'").fetchone()
    assert row['class'] == 'date_moved'


def test_header_in_scope_in_window_but_zero_built_lines_is_data_gap(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000006-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    # header exists, in-scope, in-window — but sales_entries has no entry for
    # it (STCRD had no matching line this run).
    counts = mr.scan_reconcile([], [_hdr('IV1000006', docdat=IN_WINDOW_DATE)], CUTOFF, conn=c)
    c.commit()

    assert counts['data_gap'] == 1
    row = c.execute("SELECT class FROM express_reconcile_flags WHERE doc_base='IV1000006'").fetchone()
    assert row['class'] == 'data_gap'


def test_duplicate_conflicting_headers_is_data_gap_even_when_present(empty_db_conn):
    """The builder's dict-comprehension last-one-wins must not silently pick
    a class for us — even when the doc IS present (entries got built from
    whichever header won), a dup with conflicting RECTYP/DOCDAT is data_gap."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000007-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    headers = [
        _hdr('IV1000007', rectyp='3', docdat=IN_WINDOW_DATE),
        _hdr('IV1000007', rectyp='7', docdat=IN_WINDOW_DATE),   # conflicting RECTYP
    ]
    counts = mr.scan_reconcile([_entry('IV1000007-1')], headers, CUTOFF, conn=c)
    c.commit()

    assert counts['data_gap'] == 1
    row = c.execute("SELECT class FROM express_reconcile_flags WHERE doc_base='IV1000007'").fetchone()
    assert row['class'] == 'data_gap'


def test_doc_older_than_window_is_ignored_entirely(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000008-1', date_iso=OLD_DATE.isoformat(), product_id=pid)
    c.commit()

    counts = mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()

    assert sum(counts.values()) == 0
    assert c.execute("SELECT COUNT(*) FROM express_reconcile_flags").fetchone()[0] == 0


def test_reappearing_doc_closes_open_flag(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000009-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    counts1 = mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()
    assert counts1['deleted'] == 1

    # Doc reappears in the next Express extract.
    counts2 = mr.scan_reconcile([_entry('IV1000009-1')], [_hdr('IV1000009', docdat=IN_WINDOW_DATE)],
                                CUTOFF, conn=c)
    c.commit()

    assert counts2['reappeared'] == 1
    row = c.execute(
        "SELECT state, resolved_by FROM express_reconcile_flags WHERE doc_base='IV1000009'"
    ).fetchone()
    assert row['state'] == 'reappeared' and row['resolved_by'] == 'system'
    # A NEW open flag may start later (partial unique index only blocks
    # 'open' rows) — nothing open right now.
    assert c.execute(
        "SELECT COUNT(*) FROM express_reconcile_flags WHERE doc_base='IV1000009' AND state='open'"
    ).fetchone()[0] == 0


def test_second_scan_refreshes_latest_payload_first_stays_byte_identical(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000010-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid, net=100.0)
    c.commit()

    mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()
    first = _flag_row(c, c.execute(
        "SELECT id FROM express_reconcile_flags WHERE doc_base='IV1000010'").fetchone()[0])
    first_payload = first['first_payload_json']
    flag_id = first['id']

    # Change nothing meaningful, scan again — refresh should be a pure re-stamp.
    mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()

    second = _flag_row(c, flag_id)
    assert second['first_payload_json'] == first_payload, "first_payload_json must stay byte-identical"
    # Still exactly one OPEN row for this doc_base (partial UNIQUE holds).
    assert c.execute(
        "SELECT COUNT(*) FROM express_reconcile_flags WHERE doc_base='IV1000010' AND state='open'"
    ).fetchone()[0] == 1


# ── Forensics ────────────────────────────────────────────────────────────────

def test_update_on_first_payload_json_raises(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000011-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000011')

    with pytest.raises(sqlite3.IntegrityError, match='first_payload_json'):
        c.execute("UPDATE express_reconcile_flags SET first_payload_json='{}' WHERE id=?", (flag_id,))
    c.rollback()


def test_events_table_rejects_update_and_delete_allows_insert(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000012-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000012')
    c.execute(
        "INSERT INTO express_reconcile_events (flag_id, to_state, note) VALUES (?, 'open', 'x')",
        (flag_id,))
    c.commit()
    ev_id = c.execute("SELECT id FROM express_reconcile_events WHERE flag_id=?", (flag_id,)).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        c.execute("UPDATE express_reconcile_events SET note='hacked' WHERE id=?", (ev_id,))
    c.rollback()

    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        c.execute("DELETE FROM express_reconcile_events WHERE id=?", (ev_id,))
    c.rollback()


def test_every_transition_writes_event_in_same_scan(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000013-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()

    mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()
    flag_id = c.execute(
        "SELECT id FROM express_reconcile_flags WHERE doc_base='IV1000013'").fetchone()[0]
    evs = _events(c, flag_id)
    assert len(evs) == 1
    assert evs[0]['to_state'] == 'open' and evs[0]['to_class'] == 'deleted'


def test_refused_apply_leaves_no_event_and_no_state_change(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000014-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000014', cls='out_of_scope')   # not apply-eligible

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    row = _flag_row(c, flag_id)
    assert row['state'] == 'open'
    assert len(_events(c, flag_id)) == 0
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000014'").fetchone()[0] == 1


# ── history_import ledger guard ─────────────────────────────────────────────

def test_history_import_compensator_leg_refuses_apply(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000015-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000015-1', note='BSN ขาย', quantity_change=-1.0)
    # The compensator leg _sync_bsn_to_stock pairs with every history_import OUT.
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000015-1',
                    note='ประวัติขาย (ไม่นับสต็อค): ชื่อสินค้า', quantity_change=1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000015')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'ledger แปลกปลอม' in result['error']
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000015'").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM transactions WHERE reference_no='IV1000015-1'").fetchone()[0] == 2
    row = _flag_row(c, flag_id)
    assert row['state'] == 'open'


# ── Duplicate canonical identity ────────────────────────────────────────────

def test_duplicate_canonical_identity_refuses_apply(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    # Two live rows sharing (doc_no, bsn_code) — CAS cannot tell which is which.
    _insert_sale(c, doc_no='IV1000016-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 bsn_code='DUP1', synced=0)
    _insert_sale(c, doc_no='IV1000016-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 bsn_code='DUP1', synced=0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000016')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'ซ้ำ' in result['error']
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000016'").fetchone()[0] == 2


# ── Apply class-gate ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('cls', ['out_of_scope', 'parse_gap', 'date_moved', 'data_gap'])
def test_apply_refuses_every_non_deleted_class(empty_db_conn, cls):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000017-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000017', cls=cls)

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'deleted' in result['error']
    row = _flag_row(c, flag_id)
    assert row['state'] == 'open' and row['class'] == cls
    assert len(_events(c, flag_id)) == 0
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000017'").fetchone()[0] == 1


def test_apply_deleted_class_removes_stock_ledger_and_sales_rows(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000018-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 qty=3.0, synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000018-1', note='BSN ขาย', quantity_change=-3.0)
    c.commit()

    stock_before = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0]

    flag_id = _make_flag(c, 'IV1000018')
    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result == {'ok': True}
    row = _flag_row(c, flag_id)
    assert row['state'] == 'applied' and row['resolved_by'] == 'tester'
    evs = _events(c, flag_id)
    assert len(evs) == 1 and evs[0]['to_state'] == 'applied'
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000018'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM transactions WHERE reference_no='IV1000018-1'").fetchone()[0] == 0
    # mig-080 trigger auto-reconciled stock_levels on the transactions DELETE.
    stock_after = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0]
    assert stock_after == stock_before + 3.0


# ── Platform refusal ─────────────────────────────────────────────────────────

def test_platform_customer_shopee_refuses_apply_nothing_mutated(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    cur = c.execute(
        "INSERT INTO platform_skus (platform, product_name, internal_product_id, qty_per_sale, stock) "
        "VALUES ('shopee', 'สินค้าทดสอบ', ?, 1, 10)", (pid,))
    sku_id = cur.lastrowid
    _insert_sale(c, doc_no='IV1000019-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 customer='หน้าร้านS', synced=1, qty=1.0)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000019-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000019')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'แพลตฟอร์ม' in result['error']
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000019'").fetchone()[0] == 1
    assert c.execute("SELECT stock FROM platform_skus WHERE id=?", (sku_id,)).fetchone()[0] == 10
    assert _flag_row(c, flag_id)['state'] == 'open'


def test_platform_customer_trailing_space_still_refuses(empty_db_conn):
    """The SAME normalization the sync's deduction lookup uses — a stored
    'หน้าร้านS ' (trailing space) must not bypass the guard."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000020-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 customer='หน้าร้านS ', synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000020-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000020')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'แพลตฟอร์ม' in result['error']


def test_platform_customer_closed_shop_b_applies_normally(empty_db_conn):
    """หน้าร้านB (the closed second Shopee shop) is deliberately ABSENT from
    PLATFORM_STOCK_DEDUCT_CUSTOMERS — its docs apply like any normal doc."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000021-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 customer='หน้าร้านB', synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000021-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000021')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result == {'ok': True}
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000021'").fetchone()[0] == 0


# ── Mapped/unmapped/missing-conversion coverage: skipped-sync lines ────────

def test_unsynced_line_has_no_ledger_row_apply_does_not_invent_one(empty_db_conn):
    """A line with no unit_conversions ratio never got synced (models/bsn_sync.
    _sync_bsn_to_stock skips it) — apply must delete the source row without
    trying to reverse a ledger movement that was never created."""
    c = empty_db_conn
    pid = _seed_product(c)
    synced_id = _insert_sale(c, doc_no='IV1000022-1', date_iso=IN_WINDOW_DATE.isoformat(),
                             product_id=pid, bsn_code='MAPPED', synced=1, qty=1.0)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000022-1', note='BSN ขาย', quantity_change=-1.0)
    unsynced_id = _insert_sale(c, doc_no='IV1000022-2', date_iso=IN_WINDOW_DATE.isoformat(),
                               product_id=pid, bsn_code='UNMAPPED', synced=0, qty=1.0)
    c.commit()

    flag_id = _make_flag(c, 'IV1000022')
    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result == {'ok': True}
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000022'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM transactions WHERE reference_no IN ('IV1000022-1','IV1000022-2')"
                     ).fetchone()[0] == 0


def test_unsynced_line_with_a_stray_ledger_row_refuses(empty_db_conn):
    """If an unsynced line somehow HAS a ledger row (shouldn't happen, but
    apply must not silently delete a real stock movement it can't explain)."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000023-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 synced=0)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000023-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000023')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'ledger แปลกปลอม' in result['error']


# ── ตรวจบิล cleanup ──────────────────────────────────────────────────────────

def test_apply_cleans_review_docs_in_same_transaction(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000024-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid, synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000024-1', note='BSN ขาย', quantity_change=-1.0)
    c.execute(
        "INSERT INTO txn_review_docs (doc_base, date_iso, line_count, flag_count) "
        "VALUES ('IV1000024', ?, 1, 1)", (IN_WINDOW_DATE.isoformat(),))
    c.execute(
        "INSERT INTO txn_review_flags (doc_base, doc_no, rule_code, severity, message_th) "
        "VALUES ('IV1000024', 'IV1000024-1', 'R1', 'high', 'test')")
    c.commit()
    flag_id = _make_flag(c, 'IV1000024')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result == {'ok': True}
    assert c.execute("SELECT COUNT(*) FROM txn_review_docs WHERE doc_base='IV1000024'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM txn_review_flags WHERE doc_base='IV1000024'").fetchone()[0] == 0


# ── Replay no-op ─────────────────────────────────────────────────────────────

def test_replay_on_already_applied_flag_is_friendly_noop(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000025-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid, synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000025-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000025')

    first = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)
    assert first == {'ok': True}

    second = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)
    assert second == {'ok': True, 'noop': True}
    # Still exactly one 'applied' event (the second call wrote nothing new).
    assert len(_events(c, flag_id)) == 1


# ── Post-scan drift → CAS refusal ───────────────────────────────────────────

def test_post_scan_drift_refuses_apply(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000026-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 net=100.0, synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000026-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000026')   # payload snapshot: net=100.0

    # A credit-note import (or any other post-scan write) changes ref_invoice.
    c.execute("UPDATE sales_transactions SET ref_invoice='SR9999999' WHERE doc_base='IV1000026'")
    c.commit()

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'สแกนใหม่' in result['error']
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000026'").fetchone()[0] == 1


# ── Concurrency seam BEFORE the first write ─────────────────────────────────

def test_apply_holds_lock_before_first_write(tmp_db, monkeypatch):
    """BEGIN IMMEDIATE must be the very first statement — inject the
    interleaving writer from a function called strictly BEFORE any mutation
    (_cas_compare), open a SECOND connection with a short timeout, and assert
    it is excluded. A seam placed after the first write would prove nothing
    (see .claude/rules/erp-engineering-discipline.md)."""
    import config
    conn0 = sqlite3.connect(config.DATABASE_PATH)
    conn0.row_factory = sqlite3.Row
    conn0.execute("PRAGMA foreign_keys = ON")
    pid = _seed_product(conn0)
    _insert_sale(conn0, doc_no='IV1000027-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid, synced=1)
    _insert_bsn_txn(conn0, product_id=pid, doc_no='IV1000027-1', note='BSN ขาย', quantity_change=-1.0)
    conn0.commit()
    flag_id = _make_flag(conn0, 'IV1000027')
    conn0.close()

    seen = {}

    real_cas_compare = mr._cas_compare

    def _spy(*args, **kwargs):
        probe = sqlite3.connect(config.DATABASE_PATH, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                probe.execute(
                    "UPDATE express_reconcile_flags SET resolved_by='intruder' WHERE id=?",
                    (flag_id,))
            seen['locked'] = True
        finally:
            probe.close()
        return real_cas_compare(*args, **kwargs)

    monkeypatch.setattr(mr, '_cas_compare', _spy)

    result = mr.apply_reconcile_flag(flag_id, 'tester')

    assert seen.get('locked') is True
    assert result == {'ok': True}


# ── Route: /reconcile (manager+ view, manager-whitelist POSTs, nav wiring) ──

def _login(client, role='admin', user_id=1):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['role'] = role


@pytest.fixture
def route_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


def _seed_flag_via_sqlite3(tmp_db_path, doc_no='IVRECROUTE001-1', customer='ลูกค้าทดสอบ'):
    c = sqlite3.connect(tmp_db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    pid = _seed_product(c, name='สินค้า route test')
    _insert_sale(c, doc_no=doc_no, date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 customer=customer, synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no=doc_no, note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, doc_no.rsplit('-', 1)[0])
    c.close()
    return flag_id


def test_reconcile_index_requires_manager(route_client, tmp_db):
    _login(route_client, role='staff')
    resp = route_client.get('/reconcile', follow_redirects=False)
    assert resp.status_code == 302   # bounced by _require_manager, not a 200 page


def test_reconcile_index_renders_for_manager(route_client, tmp_db):
    _seed_flag_via_sqlite3(tmp_db)
    _login(route_client, role='manager')
    resp = route_client.get('/reconcile')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'IVRECROUTE001' in html
    assert '>ลบจาก Express<' in html


def test_reconcile_nav_wiring_sidebar_and_drawer(route_client, tmp_db):
    from access_control import _ENDPOINT_MODULE
    assert _ENDPOINT_MODULE.get('reconcile.index') == 'data'

    _login(route_client, role='manager')
    html = route_client.get('/reconcile').get_data(as_text=True)
    assert html.count('href="/reconcile"') >= 2, \
        "reconcile link missing from a nav surface (sidebar or mobile drawer)"


def test_reconcile_apply_post_staff_forbidden(route_client, tmp_db):
    flag_id = _seed_flag_via_sqlite3(tmp_db, doc_no='IVRECROUTE002-1')
    _login(route_client, role='staff')
    resp = route_client.post(f'/reconcile/{flag_id}/apply', follow_redirects=False)
    assert resp.status_code == 302
    # Whitelist bounce (access_control) redirects to the role's home, not /reconcile.
    assert resp.headers.get('Location', '').rstrip('/') != f'/reconcile/{flag_id}/apply'

    import config
    c = sqlite3.connect(config.DATABASE_PATH)
    row = c.execute("SELECT state FROM express_reconcile_flags WHERE id=?", (flag_id,)).fetchone()
    c.close()
    assert row[0] == 'open', "staff POST must not have applied the flag"


def test_reconcile_apply_post_manager_end_to_end(route_client, tmp_db):
    flag_id = _seed_flag_via_sqlite3(tmp_db, doc_no='IVRECROUTE003-1')
    _login(route_client, role='manager')
    resp = route_client.post(f'/reconcile/{flag_id}/apply', data={}, follow_redirects=True)
    assert resp.status_code == 200

    import config
    c = sqlite3.connect(config.DATABASE_PATH)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT state FROM express_reconcile_flags WHERE id=?", (flag_id,)).fetchone()
    remaining = c.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IVRECROUTE003'").fetchone()[0]
    c.close()
    assert row['state'] == 'applied'
    assert remaining == 0


# ── get_reconcile_flag / list_resolved_reconcile_flags / dismiss+reopen ─────

def test_get_reconcile_flag_returns_payload_linked_records_and_events(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000028-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid, synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000028-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()
    flag_id = c.execute(
        "SELECT id FROM express_reconcile_flags WHERE doc_base='IV1000028'").fetchone()[0]

    detail = mr.get_reconcile_flag(flag_id, conn=c)

    assert detail['doc_base'] == 'IV1000028'
    assert detail['class'] == 'deleted'
    assert len(detail['latest_payload']['rows']) == 1
    assert set(detail['linked_records']) == {
        'paid_invoices', 'commission_payouts', 'credit_note_amounts',
        'credit_note_imports', 'marketplace_order_invoice',
        'marketplace_amount_review', 'express_invoice_refs', 'sr_ref_rows',
    }
    assert len(detail['events']) == 1


def test_get_reconcile_flag_unknown_id_returns_none(empty_db_conn):
    assert mr.get_reconcile_flag(999999, conn=empty_db_conn) is None


def test_dismiss_then_list_resolved_then_reopen_clears_suppression(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000029-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000029', cls='out_of_scope')

    result = mr.dismiss_reconcile_flag(flag_id, 'putty', 'ตั้งใจไว้ก่อน รอทีมยืนยัน', conn=c)
    c.commit()
    assert result == {'ok': True}

    resolved = mr.list_resolved_reconcile_flags(conn=c)
    assert len(resolved) == 1
    assert resolved[0]['state'] == 'dismissed' and resolved[0]['suppression_active'] == 1

    # Suppressed — a re-scan with the SAME evidence (still out_of_scope) must
    # NOT open a new flag for the same (doc_base, class).
    oos_hdr = [_hdr('IV1000029', rectyp='7', docdat=IN_WINDOW_DATE)]
    counts = mr.scan_reconcile([], oos_hdr, CUTOFF, conn=c)
    c.commit()
    assert counts['out_of_scope'] == 0
    assert c.execute(
        "SELECT COUNT(*) FROM express_reconcile_flags WHERE doc_base='IV1000029' AND state='open'"
    ).fetchone()[0] == 0

    reopen_result = mr.reopen_reconcile_flag(flag_id, 'putty', conn=c)
    c.commit()
    assert reopen_result == {'ok': True}
    assert _flag_row(c, flag_id)['suppression_active'] == 0

    # Now a re-scan may re-flag it.
    counts2 = mr.scan_reconcile([], oos_hdr, CUTOFF, conn=c)
    c.commit()
    assert counts2['out_of_scope'] == 1


def test_reappearance_clears_suppression_on_dismissed_flag(empty_db_conn):
    """Plan §2 epoch-end way 2: a doc reappearing in the file is new
    evidence — suppression on its dismissed flags must clear automatically,
    with an audited event, so a LATER genuine deletion is not silently
    swallowed by stale suppression."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000031-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000031', cls='out_of_scope')
    mr.dismiss_reconcile_flag(flag_id, 'putty', 'เก็บไว้ก่อน', conn=c)
    c.commit()
    assert _flag_row(c, flag_id)['suppression_active'] == 1
    events_before = len(_events(c, flag_id))

    # Doc reappears (present in the file again).
    mr.scan_reconcile([_entry('IV1000031-1')], [_hdr('IV1000031', docdat=IN_WINDOW_DATE)],
                      CUTOFF, conn=c)
    c.commit()

    row = _flag_row(c, flag_id)
    assert row['suppression_active'] == 0
    assert row['state'] == 'dismissed'   # the row itself is untouched, only the flag clears
    events_after = _events(c, flag_id)
    assert len(events_after) == events_before + 1
    assert events_after[-1]['actor'] == 'system'
    assert events_after[-1]['from_state'] == 'dismissed' and events_after[-1]['to_state'] == 'dismissed'


def test_reappearance_then_fresh_disappearance_opens_a_new_flag(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000032-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000032', cls='out_of_scope')
    mr.dismiss_reconcile_flag(flag_id, 'putty', 'เก็บไว้ก่อน', conn=c)
    c.commit()

    # Reappears, clearing suppression.
    mr.scan_reconcile([_entry('IV1000032-1')], [_hdr('IV1000032', docdat=IN_WINDOW_DATE)],
                      CUTOFF, conn=c)
    c.commit()

    # Disappears again — with suppression cleared, this must open a FRESH
    # 'deleted' flag (the old dismissed row stays dismissed/closed).
    counts = mr.scan_reconcile([], [], CUTOFF, conn=c)
    c.commit()

    assert counts['deleted'] == 1
    new_row = c.execute(
        "SELECT id, class, state FROM express_reconcile_flags "
        "WHERE doc_base='IV1000032' AND state='open'").fetchone()
    assert new_row is not None
    assert new_row['class'] == 'deleted'
    assert new_row['id'] != flag_id


def test_dismiss_then_repeated_scans_with_doc_still_absent_no_churn(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000033-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000033', cls='out_of_scope')
    mr.dismiss_reconcile_flag(flag_id, 'putty', 'เก็บไว้ก่อน', conn=c)
    c.commit()

    oos_hdr = [_hdr('IV1000033', rectyp='7', docdat=IN_WINDOW_DATE)]
    events_before = len(_events(c, flag_id))
    for _ in range(3):
        counts = mr.scan_reconcile([], oos_hdr, CUTOFF, conn=c)
        c.commit()
        assert counts['out_of_scope'] == 0

    assert c.execute(
        "SELECT COUNT(*) FROM express_reconcile_flags WHERE doc_base='IV1000033'"
    ).fetchone()[0] == 1   # still just the one dismissed row — no new flag
    assert len(_events(c, flag_id)) == events_before   # no churn


def test_dismiss_requires_a_note(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000030-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid)
    c.commit()
    flag_id = _make_flag(c, 'IV1000030', cls='data_gap')

    result = mr.dismiss_reconcile_flag(flag_id, 'putty', '   ', conn=c)

    assert result['ok'] is False
    assert _flag_row(c, flag_id)['state'] == 'open'


# ── FIX 2: CAS must compare row id (plan §2c "exhaustive: id, ...") ────────

def test_row_deleted_and_reinserted_with_new_id_refuses_apply(empty_db_conn):
    """A row deleted+reinserted with IDENTICAL field values but a NEW id
    (outside the DBF import path — e.g. a manual data-quality fix) must fail
    CAS, not silently pass because every OTHER field still matches. Before
    FIX 2, apply would delete the ledger by doc_no (unaffected by row id)
    but delete 0 sales_transactions rows (stale id), leaving the flag marked
    'applied' while the doc's row is still live."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000034-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000034-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000034')   # payload snapshot captures the ORIGINAL row id

    # Delete + reinsert with identical field values -> same everything except id.
    c.execute("DELETE FROM sales_transactions WHERE doc_base='IV1000034'")
    _insert_sale(c, doc_no='IV1000034-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 synced=1)
    c.commit()

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'สแกนใหม่' in result['error']
    # Zero mutation: the reinserted row is still there, ledger untouched.
    assert c.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV1000034'").fetchone()[0] == 1
    assert c.execute(
        "SELECT COUNT(*) FROM transactions WHERE reference_no='IV1000034-1'").fetchone()[0] == 1
    assert _flag_row(c, flag_id)['state'] == 'open'


# ── FIX 3: is_platform_doc() drives BOTH the UI button state and the guard ──

def test_is_platform_doc_shopee_lazada_true_closed_shop_b_false():
    assert mr.is_platform_doc([{'customer': 'หน้าร้านS'}]) is True
    assert mr.is_platform_doc([{'customer': 'หน้าร้านS '}]) is True   # trailing space
    assert mr.is_platform_doc([{'customer': 'หน้าร้านL'}]) is True
    assert mr.is_platform_doc([{'customer': 'หน้าร้านB'}]) is False   # deliberately exempt
    assert mr.is_platform_doc([{'customer': 'ลูกค้าทั่วไป'}]) is False
    assert mr.is_platform_doc([]) is False


def test_list_open_flags_marks_platform_blocked(empty_db_conn):
    c = empty_db_conn
    pid_s = _seed_product(c)
    _insert_sale(c, doc_no='IV1000035-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid_s,
                 customer='หน้าร้านS', synced=1)
    c.commit()
    flag_s = _make_flag(c, 'IV1000035')

    pid_ws = _seed_product(c)
    _insert_sale(c, doc_no='IV1000036-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid_ws,
                 customer='หน้าร้านS ', synced=1)
    c.commit()
    flag_s_ws = _make_flag(c, 'IV1000036')

    pid_b = _seed_product(c)
    _insert_sale(c, doc_no='IV1000037-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid_b,
                 customer='หน้าร้านB', synced=1)
    c.commit()
    flag_b = _make_flag(c, 'IV1000037')

    flags = {f['id']: f for f in mr.list_open_reconcile_flags(conn=c)}
    assert flags[flag_s]['platform_blocked'] is True
    assert flags[flag_s_ws]['platform_blocked'] is True
    assert flags[flag_b]['platform_blocked'] is False


def test_reconcile_page_shows_disabled_button_for_platform_blocked_flag(route_client, tmp_db):
    flag_id = _seed_flag_via_sqlite3(tmp_db, doc_no='IVRECROUTE004-1', customer='หน้าร้านS')
    _login(route_client, role='manager')
    html = route_client.get('/reconcile').get_data(as_text=True)
    assert 'แพลตฟอร์ม' in html
    assert f'action="/reconcile/{flag_id}/apply"' not in html
    assert 'disabled' in html


def test_reconcile_page_shows_active_apply_for_non_platform_deleted_flag(route_client, tmp_db):
    flag_id = _seed_flag_via_sqlite3(tmp_db, doc_no='IVRECROUTE005-1', customer='ลูกค้าทั่วไป')
    _login(route_client, role='manager')
    html = route_client.get('/reconcile').get_data(as_text=True)
    assert f'action="/reconcile/{flag_id}/apply"' in html


# ── FIX 4 (documented, not fixed): 2+ lines sharing one literal doc_no ─────

def test_two_lines_sharing_one_doc_no_always_refuses_ledger_check(empty_db_conn):
    """Pin the documented limitation in _ledger_check's docstring: a real
    Express data shape (two STCRD lines reusing one SEQNUM — seen live on
    IV6900582-1) writes two 'BSN ขาย' transactions rows under the identical
    reference_no. The check cannot disambiguate which belongs to which
    payload line, so it conservatively refuses BOTH — safe direction, never
    a wrong delete, but such a doc can never auto-apply."""
    c = empty_db_conn
    pid1 = _seed_product(c, name='สินค้า A')
    pid2 = _seed_product(c, name='สินค้า B')
    shared_doc_no = 'IV6900582-1'
    _insert_sale(c, doc_no=shared_doc_no, date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid1,
                 bsn_code='CODE-A', synced=1)
    _insert_sale(c, doc_no=shared_doc_no, date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid2,
                 bsn_code='CODE-B', synced=1)
    _insert_bsn_txn(c, product_id=pid1, doc_no=shared_doc_no, note='BSN ขาย', quantity_change=-1.0)
    _insert_bsn_txn(c, product_id=pid2, doc_no=shared_doc_no, note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV6900582')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result['ok'] is False
    assert 'ledger แปลกปลอม' in result['error']
    assert c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV6900582'"
                     ).fetchone()[0] == 2


# ── FIX 5: apply records a linked-records summary in resolution_note ───────

def test_apply_records_linked_records_summary_in_resolution_note(empty_db_conn):
    """Plan §4: the linked-records panel is 'shown BEFORE apply, recorded
    in the resolution note'."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000038-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000038-1', note='BSN ขาย', quantity_change=-1.0)
    cur = c.execute(
        "INSERT INTO received_payments (re_no, date_iso, customer) VALUES (?, ?, ?)",
        ('RE0001TEST', IN_WINDOW_DATE.isoformat(), 'ลูกค้าทดสอบ'))
    re_id = cur.lastrowid
    c.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?, ?, 'IV', 100.0)",
        (re_id, 'IV1000038'))
    c.commit()
    flag_id = _make_flag(c, 'IV1000038')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result == {'ok': True}
    row = _flag_row(c, flag_id)
    assert row['resolution_note'] is not None
    assert 'paid_invoices=1' in row['resolution_note']
    evs = _events(c, flag_id)
    assert 'paid_invoices=1' in evs[-1]['note']


def test_apply_resolution_note_says_no_linked_records_when_none(empty_db_conn):
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000039-1', date_iso=IN_WINDOW_DATE.isoformat(), product_id=pid,
                 synced=1)
    _insert_bsn_txn(c, product_id=pid, doc_no='IV1000039-1', note='BSN ขาย', quantity_change=-1.0)
    c.commit()
    flag_id = _make_flag(c, 'IV1000039')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result == {'ok': True}
    row = _flag_row(c, flag_id)
    assert 'ไม่มีข้อมูลอ้างอิง' in row['resolution_note']


# ── P0 (Codex NO-GO, round 4): ledger-check must verify product_id/txn_type
# /exact-note/signed-magnitude before trusting a row, mirroring bsn_sync.py's
# writer rule exactly (~215-233), and delete by the exact verified ids ─────

def _assert_refused_zero_mutation(c, flag_id, doc_base, doc_no, result):
    assert result['ok'] is False
    assert c.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE doc_base=?", (doc_base,)
    ).fetchone()[0] == 1
    assert c.execute(
        "SELECT COUNT(*) FROM transactions WHERE reference_no=?", (doc_no,)
    ).fetchone()[0] == 1
    assert _flag_row(c, flag_id)['state'] == 'open'
    assert len(_events(c, flag_id)) == 0


def test_ledger_check_refuses_wrong_product_id(empty_db_conn):
    """A corrupt/stale row that shares reference_no+note but points at the
    WRONG product must never be trusted — deleting it would fire the
    mig-080 trigger against the wrong product's stock."""
    c = empty_db_conn
    pid_correct = _seed_product(c, name='สินค้าถูก')
    pid_wrong = _seed_product(c, name='สินค้าผิด')
    _insert_sale(c, doc_no='IV1000040-1', date_iso=IN_WINDOW_DATE.isoformat(),
                 product_id=pid_correct, synced=1, qty=1.0)
    _insert_bsn_txn(c, product_id=pid_wrong, doc_no='IV1000040-1', note='BSN ขาย',
                    quantity_change=-1.0)   # right note/txn_type/sign, WRONG product
    c.commit()
    flag_id = _make_flag(c, 'IV1000040')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    _assert_refused_zero_mutation(c, flag_id, 'IV1000040', 'IV1000040-1', result)


def test_ledger_check_refuses_wrong_sign(empty_db_conn):
    """txn_type label says OUT (correct for a normal IV line) but the actual
    signed quantity_change is POSITIVE — internally inconsistent. The sign
    check must catch this independently of the txn_type check (constructed
    via raw SQL, not the _insert_bsn_txn helper, which would tie txn_type to
    the sign and hide this exact mismatch)."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000041-1', date_iso=IN_WINDOW_DATE.isoformat(),
                 product_id=pid, synced=1, qty=1.0)
    c.execute("""
        INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,
                                  reference_no, note, created_at)
        VALUES (?, 'OUT', 1.0, 'unit', 'IV1000041-1', 'BSN ขาย', '2026-07-15 00:00:00')
    """, (pid,))
    c.commit()
    flag_id = _make_flag(c, 'IV1000041')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    _assert_refused_zero_mutation(c, flag_id, 'IV1000041', 'IV1000041-1', result)


def test_ledger_check_refuses_wrong_note_on_sr_line(empty_db_conn):
    """A plain 'BSN ขาย' row must NOT satisfy an SR payload line, which
    requires the return-specific 'BSN ขาย-คืน' note — even though txn_type
    (IN) and sign (+) happen to be correct for a return."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='SR6900099-1', date_iso=IN_WINDOW_DATE.isoformat(),
                 product_id=pid, synced=1, qty=1.0)
    c.execute("""
        INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,
                                  reference_no, note, created_at)
        VALUES (?, 'IN', 1.0, 'unit', 'SR6900099-1', 'BSN ขาย', '2026-07-15 00:00:00')
    """, (pid,))
    c.commit()
    flag_id = _make_flag(c, 'SR6900099')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    _assert_refused_zero_mutation(c, flag_id, 'SR6900099', 'SR6900099-1', result)


def test_ledger_check_refuses_wrong_txn_type(empty_db_conn):
    """txn_type says IN (wrong — a normal IV line must post OUT) but the
    quantity_change sign (-1) happens to be correct for OUT. The txn_type
    check must catch this independently of the sign check."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='IV1000042-1', date_iso=IN_WINDOW_DATE.isoformat(),
                 product_id=pid, synced=1, qty=1.0)
    c.execute("""
        INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,
                                  reference_no, note, created_at)
        VALUES (?, 'IN', -1.0, 'unit', 'IV1000042-1', 'BSN ขาย', '2026-07-15 00:00:00')
    """, (pid,))
    c.commit()
    flag_id = _make_flag(c, 'IV1000042')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    _assert_refused_zero_mutation(c, flag_id, 'IV1000042', 'IV1000042-1', result)


def test_apply_sr_line_correct_shape_applies_stock_moves_down(empty_db_conn):
    """Positive control: a correctly-shaped SR line (IN/+qty/'BSN ขาย-คืน')
    applies cleanly. Deleting an IN ledger row means the mig-080 trigger
    reverses it — stock goes DOWN, not up. Asserted explicitly, not just
    'changed', per the review ask."""
    c = empty_db_conn
    pid = _seed_product(c)
    _insert_sale(c, doc_no='SR6900100-1', date_iso=IN_WINDOW_DATE.isoformat(),
                 product_id=pid, synced=1, qty=2.0)
    _insert_bsn_txn(c, product_id=pid, doc_no='SR6900100-1', note='BSN ขาย-คืน',
                    quantity_change=2.0)
    c.commit()
    stock_before = c.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0]
    flag_id = _make_flag(c, 'SR6900100')

    result = mr.apply_reconcile_flag(flag_id, 'tester', conn=c)

    assert result == {'ok': True}
    stock_after = c.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0]
    assert stock_after == stock_before - 2.0
    assert c.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE doc_base='SR6900100'"
    ).fetchone()[0] == 0
    assert c.execute(
        "SELECT COUNT(*) FROM transactions WHERE reference_no='SR6900100-1'"
    ).fetchone()[0] == 0
