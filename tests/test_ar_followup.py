"""TDD tests for inventory_app/ar_followup.py — AR follow-up workspace logic.

Synthetic data only (empty_db_conn schema clone) for unit tests.
Integration tests use tmp_db_conn against a copy of the live DB to verify
the Express BSN snapshot totals (72 customers / 200 docs / ฿1,299,335.94).

AR SOURCE (2026-05-29): customer_ranking and get_customer_ar_detail now
source from express_ar_outstanding WHERE entity='BSN' at the latest snapshot.
Outreach log CRUD and list_overdue_followups are unchanged.
"""
import pytest
from datetime import date, timedelta

import ar_followup as arf


# ── synthetic data helpers ──────────────────────────────────────────────────

def _ins_express(conn, doc_no, customer_code, customer_name, doc_date_iso,
                 outstanding, bill_amount=None, paid_amount=None,
                 snapshot='2026-05-29', entity='BSN', batch_id=None):
    """Insert one row into express_ar_outstanding.

    Inserts a parent express_import_log row when batch_id is not supplied,
    re-using the most recent one for this snapshot+entity if it exists.
    """
    if bill_amount is None:
        bill_amount = outstanding
    if paid_amount is None:
        paid_amount = 0.0
    if batch_id is None:
        # Re-use existing log row for this snapshot/entity when present so we
        # don't create a new parent per call. Insert one on first call.
        row = conn.execute("""
            SELECT id FROM express_import_log
            WHERE file_type='ar_snapshot' AND snapshot_date_iso=?
            LIMIT 1
        """, (snapshot,)).fetchone()
        if row:
            batch_id = row[0]
        else:
            cur = conn.execute("""
                INSERT INTO express_import_log
                  (file_type, source_filename, record_count, line_count,
                   snapshot_date_iso, status)
                VALUES ('ar_snapshot', 'test_synthetic.csv', 0, 0, ?, 'imported')
            """, (snapshot,))
            batch_id = cur.lastrowid
    conn.execute("""
        INSERT INTO express_ar_outstanding
          (batch_id, snapshot_date_iso, customer_code, customer_name,
           doc_no, doc_date_iso, bill_amount, paid_amount,
           outstanding_amount, entity)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (batch_id, snapshot, customer_code, customer_name,
          doc_no, doc_date_iso, bill_amount, paid_amount,
          outstanding, entity))


def _ins_receipt(conn, re_no, customer, date_iso, cancelled=0, total=None):
    cur = conn.execute(
        """INSERT INTO received_payments
           (re_no, date_iso, customer, salesperson, cancelled, total)
           VALUES (?,?,?,?,?,?)""",
        (re_no, date_iso, customer, 'S1', cancelled, total),
    )
    return cur.lastrowid


# ── customer_ranking — Express snapshot source ───────────────────────────────

def test_ranking_sorts_by_outstanding_desc(empty_db_conn):
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'A', '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CB', 'B', '2026-04-01', 5000)
    _ins_express(c, 'IV03', 'CC', 'C', '2026-04-01', 3000)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    customers = [r['customer'] for r in rows]
    assert customers == ['B', 'C', 'A']
    assert rows[0]['outstanding'] == pytest.approx(5000)


def test_ranking_excludes_zero_outstanding(empty_db_conn):
    """Rows with outstanding_amount = 0 must not appear."""
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'A', '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CB', 'B', '2026-04-01', 0)   # fully paid
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert [r['customer'] for r in rows] == ['A']


def test_ranking_buckets_by_age(empty_db_conn):
    """Age = days from doc_date_iso to snapshot_date_iso (not to today)."""
    c = empty_db_conn
    snap = '2026-05-29'
    # Snapshot is 2026-05-29; doc dates chosen for exact bucket membership
    _ins_express(c, 'IV01', 'CA', 'A', '2026-05-19', 100, snapshot=snap)  # 10d → 0-30
    _ins_express(c, 'IV02', 'CA', 'A', '2026-04-14', 200, snapshot=snap)  # 45d → 31-60
    _ins_express(c, 'IV03', 'CA', 'A', '2026-03-25', 300, snapshot=snap)  # 65d → 61-90
    _ins_express(c, 'IV04', 'CA', 'A', '2025-11-30', 400, snapshot=snap)  # 180d → 90+
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    r = rows[0]
    assert r['outstanding'] == pytest.approx(1000)
    assert r['invoice_count'] == 4
    assert r['oldest_age_days'] == 180
    assert r['age_buckets']['0-30'] == pytest.approx(100)
    assert r['age_buckets']['31-60'] == pytest.approx(200)
    assert r['age_buckets']['61-90'] == pytest.approx(300)
    assert r['age_buckets']['90+'] == pytest.approx(400)


def test_ranking_empty_when_no_snapshot(empty_db_conn):
    """No express_ar_outstanding rows → returns empty list (no crash)."""
    c = empty_db_conn
    rows = arf.customer_ranking(conn=c)
    assert rows == []


def test_ranking_ignores_non_bsn_entity(empty_db_conn):
    """SD rows must not pollute BSN ranking."""
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'A', '2026-04-01', 1000, entity='BSN')
    _ins_express(c, 'SD01', 'SX', 'X', '2026-04-01', 9999, entity='SD')
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['customer_code'] == 'CA'


def test_ranking_aggregates_same_customer_code(empty_db_conn):
    """Multiple invoices for the same customer_code roll up to one row."""
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'ลูกค้า A', '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CA', 'ลูกค้า A', '2026-03-01', 2000)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['outstanding'] == pytest.approx(3000)
    assert rows[0]['invoice_count'] == 2


def test_ranking_latest_snapshot_only(empty_db_conn):
    """Only the latest snapshot_date_iso rows are used."""
    c = empty_db_conn
    # Old snapshot — should be ignored
    _ins_express(c, 'IV01', 'CA', 'A', '2026-03-01', 5000, snapshot='2026-04-30')
    # New snapshot — should be used
    _ins_express(c, 'IV01', 'CA', 'A', '2026-03-01', 1000, snapshot='2026-05-29')
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['outstanding'] == pytest.approx(1000)


# ── log_outreach ────────────────────────────────────────────────────────────

def test_log_outreach_inserts_row(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    log_id = arf.log_outreach(
        conn=c, customer='A', customer_code='CA', log_date=today,
        channel='phone', contact_person='คุณสมศักดิ์', result='promised',
        promised_amount=5000.00, promised_date=today,
        next_action_date=(date.today() + timedelta(days=7)).isoformat(),
        notes='นัดจ่าย', created_by='admin',
    )
    c.commit()
    row = c.execute(
        "SELECT * FROM ar_followup_log WHERE id=?", (log_id,)
    ).fetchone()
    assert row['customer'] == 'A'
    assert row['channel'] == 'phone'
    assert row['result'] == 'promised'
    assert row['promised_amount'] == pytest.approx(5000)


def test_log_outreach_rejects_bad_channel(empty_db_conn):
    import sqlite3 as _sq
    c = empty_db_conn
    with pytest.raises(_sq.IntegrityError):
        arf.log_outreach(
            conn=c, customer='A', customer_code='CA',
            log_date=date.today().isoformat(),
            channel='telegram', result='promised', created_by='admin',
        )


def test_log_outreach_rejects_bad_result(empty_db_conn):
    import sqlite3 as _sq
    c = empty_db_conn
    with pytest.raises(_sq.IntegrityError):
        arf.log_outreach(
            conn=c, customer='A', customer_code='CA',
            log_date=date.today().isoformat(),
            channel='phone', result='maybe', created_by='admin',
        )


# ── get_customer_followups ──────────────────────────────────────────────────

def test_get_followups_returns_newest_first(empty_db_conn):
    c = empty_db_conn
    old = (date.today() - timedelta(days=10)).isoformat()
    new = date.today().isoformat()
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=old,
                     channel='phone', result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=new,
                     channel='line', result='promised', created_by='admin')
    c.commit()

    rows = arf.get_customer_followups(conn=c, customer='A')
    assert len(rows) == 2
    assert rows[0]['log_date'] == new
    assert rows[0]['channel'] == 'line'


def test_get_followups_isolates_per_customer(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=today,
                     channel='phone', result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='B', customer_code='CB', log_date=today,
                     channel='line', result='promised', created_by='admin')
    c.commit()

    assert len(arf.get_customer_followups(conn=c, customer='A')) == 1
    assert len(arf.get_customer_followups(conn=c, customer='B')) == 1


# ── get_customer_ar_detail ──────────────────────────────────────────────────

def test_get_customer_ar_detail_lists_outstanding(empty_db_conn):
    c = empty_db_conn
    snap = '2026-05-29'
    _ins_express(c, 'IV01', 'CA', 'A', '2026-05-24', 1000, snapshot=snap)   # 5d
    _ins_express(c, 'IV02', 'CA', 'A', '2025-11-10', 2000, snapshot=snap)   # 200d
    _ins_express(c, 'IV03', 'CB', 'B', '2026-05-29', 500,  snapshot=snap)   # 0d
    c.commit()

    rows = arf.get_customer_ar_detail(conn=c, customer='CA')
    docs = sorted(r['doc_no'] for r in rows)
    assert docs == ['IV01', 'IV02']
    # IV02 is older — age should be ~200
    iv02 = next(r for r in rows if r['doc_no'] == 'IV02')
    assert iv02['age_days'] == (date.fromisoformat(snap) - date(2025, 11, 10)).days


def test_get_customer_ar_detail_sorted_oldest_first(empty_db_conn):
    """Rows are returned with oldest (largest age_days) first."""
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'A', '2026-05-20', 100)   # newer
    _ins_express(c, 'IV02', 'CA', 'A', '2026-01-01', 200)   # older
    c.commit()

    rows = arf.get_customer_ar_detail(conn=c, customer='CA')
    assert rows[0]['doc_no'] == 'IV02'


# ── ranking joins last_log ──────────────────────────────────────────────────

def test_ranking_includes_last_log(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_express(c, 'IV01', 'CA', 'A', '2026-04-01', 1000)
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=today,
                     channel='phone', result='promised',
                     next_action_date=today, created_by='admin')
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert rows[0]['last_log_date'] == today
    assert rows[0]['last_log_result'] == 'promised'


# ── customer_ranking aggregation by customer_code ───────────────────────────

def test_ranking_aggregates_by_customer_code_when_names_differ(empty_db_conn):
    """Same customer_code, two name spellings → ONE row."""
    c = empty_db_conn
    # Express snapshot stores one name per row; both have same code
    _ins_express(c, 'IV01', 'CA', 'ลูกค้า A',  '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CA', 'ลูกค้า A ', '2026-04-01', 2000)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['outstanding'] == pytest.approx(3000)
    assert rows[0]['customer_code'] == 'CA'
    assert rows[0]['invoice_count'] == 2


def test_ranking_last_log_aggregates_by_group(empty_db_conn):
    """Ranking's last_log uses the newest log across all name variants."""
    c = empty_db_conn
    today = date.today()
    _ins_express(c, 'IV01', 'CA', 'ลูกค้า A',  '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CA', 'ลูกค้า A ', '2026-04-01', 2000)
    arf.log_outreach(conn=c, customer='ลูกค้า A',  customer_code='CA',
                     log_date=(today - timedelta(days=5)).isoformat(),
                     channel='phone', result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='ลูกค้า A ', customer_code='CA',
                     log_date=today.isoformat(),
                     channel='line',  result='promised',  created_by='admin')
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['last_log_date']   == today.isoformat()
    assert rows[0]['last_log_result'] == 'promised'


# ── overdue (supersession + terminal-state awareness) ───────────────────────

def test_overdue_excludes_superseded_by_later_log(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=5)).isoformat(),
                     channel='phone', result='no_answer',
                     next_action_date=(today + timedelta(days=7)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_excludes_terminal_paid_full(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=10)).isoformat(),
                     channel='phone', result='paid_full',
                     next_action_date=(today - timedelta(days=5)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_excludes_terminal_closed(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=10)).isoformat(),
                     channel='phone', result='closed',
                     next_action_date=(today - timedelta(days=5)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_includes_only_unresolved_past_due(empty_db_conn):
    c = empty_db_conn
    today  = date.today()
    past   = (today - timedelta(days=5)).isoformat()
    future = (today + timedelta(days=5)).isoformat()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=past, channel='phone', result='promised',
                     next_action_date=past,   created_by='admin')
    arf.log_outreach(conn=c, customer='B', customer_code='CB',
                     log_date=past, channel='phone', result='paid_full',
                     next_action_date=past,   created_by='admin')
    arf.log_outreach(conn=c, customer='C', customer_code='CC',
                     log_date=past, channel='phone', result='promised',
                     next_action_date=future, created_by='admin')
    c.commit()

    overdue = arf.list_overdue_followups(conn=c, as_of=today.isoformat())
    assert [r['customer'] for r in overdue] == ['A']


def test_overdue_falls_back_to_prior_when_latest_has_null(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=5)).isoformat(),
                     channel='phone', result='no_answer',
                     next_action_date=None, created_by='admin')
    c.commit()

    overdue = arf.list_overdue_followups(conn=c, as_of=today.isoformat())
    customers = [r['customer'] for r in overdue]
    assert customers == ['A']
    assert overdue[0]['next_action_date'] == (today - timedelta(days=10)).isoformat()


def test_overdue_excludes_when_latest_terminal_even_with_prior_action_date(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=2)).isoformat(),
                     channel='visit', result='paid_full',
                     next_action_date=None, created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_uses_latest_action_date_when_present(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=2)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today + timedelta(days=7)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


# ── _resolve_target ──────────────────────────────────────────────────────────

def test_resolve_target_accepts_customer_code(empty_db_conn):
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'ลูกค้า A',  '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CA', 'ลูกค้า A ', '2026-04-01', 2000)
    c.commit()

    code, names = arf._resolve_target(c, 'CA')
    assert code == 'CA'
    assert 'ลูกค้า A' in names
    assert 'ลูกค้า A ' in names


def test_resolve_target_falls_back_to_name(empty_db_conn):
    """Walk-in with no code still resolves by name."""
    c = empty_db_conn
    _ins_express(c, 'IV01', '', 'หน้าร้าน', '2026-04-01', 100)
    c.commit()

    code, names = arf._resolve_target(c, 'หน้าร้าน')
    assert code is None
    assert 'หน้าร้าน' in names


def test_resolve_target_code_lookup_finds_all_invoices(empty_db_conn):
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'ลูกค้า A',  '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CA', 'ลูกค้า A ', '2026-04-01', 2000)
    c.commit()

    rows = arf.get_customer_ar_detail(customer='CA', conn=c)
    assert sorted(r['doc_no'] for r in rows) == ['IV01', 'IV02']


def test_resolve_target_code_lookup_finds_all_followups(empty_db_conn):
    c = empty_db_conn
    _ins_express(c, 'IV01', 'CA', 'ลูกค้า A',  '2026-04-01', 1000)
    _ins_express(c, 'IV02', 'CA', 'ลูกค้า A ', '2026-04-01', 2000)
    arf.log_outreach(conn=c, customer='ลูกค้า A',  customer_code='CA',
                     log_date=date.today().isoformat(), channel='phone',
                     result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='ลูกค้า A ', customer_code='CA',
                     log_date=date.today().isoformat(), channel='line',
                     result='promised', created_by='admin')
    c.commit()

    rows = arf.get_customer_followups(customer='CA', conn=c)
    assert len(rows) == 2


# ── integration tests: Express BSN snapshot totals (live DB copy) ────────────

def test_bsn_snapshot_totals(tmp_db_conn):
    """Express BSN snapshot at 2026-06-05: 169 total rows, 67 customers, net
    ฿1,103,016.68 (includes negative-balance rows for credit/overpaid accounts).

    This is the RAW import-level snapshot — NOT affected by write-offs
    (ar_writeoffs only excludes docs from the *collectable* figure; it does not
    delete express_ar_outstanding rows). So these are pure import counts.
    NB: LIVE-DATA anchor — recompute against the live DB on the next ลูกหนี้คงค้าง
    import (don't guess).
    """
    c = tmp_db_conn
    row = c.execute("""
        SELECT COUNT(*) AS doc_count,
               COUNT(DISTINCT customer_code) AS cust_count,
               ROUND(SUM(outstanding_amount), 2) AS total_outstanding
        FROM express_ar_outstanding
        WHERE entity = 'BSN'
          AND snapshot_date_iso = (
            SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding WHERE entity='BSN'
          )
    """).fetchone()
    assert row['doc_count'] == 169, f"Expected 169 total rows, got {row['doc_count']}"
    assert row['cust_count'] == 67, f"Expected 67 customers, got {row['cust_count']}"
    assert row['total_outstanding'] == pytest.approx(1103016.68, abs=0.01), \
        f"Expected net ฿1,103,016.68, got {row['total_outstanding']}"


def test_customer_ranking_live_bsn(tmp_db_conn):
    """customer_ranking rolls up CANONICAL collectable AR per customer (latest
    snapshot, EXCLUDING RE + pre-2024 legacy + write-offs), net-positive only.
    As of the 2026-06-05 snapshot, after write-off decisions incl. the
    2026-06-24 วรสวัสดิ์ giveaway (IV6900401/402/403 = −฿164,911.39):
    = 34 customers / ฿331,107.09.

    ทรงพลเทรดดิ้ง is entirely 2014 legacy → it drops out of the collectable
    ranking and is tracked in the not-collectable list instead (net ฿164,322.73,
    NOT ฿284,863.10 — credit-note netting guard).
    NB: LIVE-DATA anchor — recompute against the live DB on the next import,
    write-off, or payment (don't guess)."""
    import cashflow as cf
    rows = arf.customer_ranking(conn=tmp_db_conn)
    total = round(sum(r['outstanding'] for r in rows), 2)
    assert len(rows) == 34, f"Expected 34 collectable net-positive customers, got {len(rows)}"
    assert total == pytest.approx(331107.09, abs=0.01), \
        f"Expected canonical collectable total ฿331,107.09, got {total}"
    # Verify sorted DESC
    for i in range(len(rows) - 1):
        assert rows[i]['outstanding'] >= rows[i+1]['outstanding']
    # ทรงพล (94ท06) is pre-2024 legacy → excluded from collectable ranking …
    assert not any((r['customer_code'] or '') == '94ท06' for r in rows), \
        "ทรงพล (all 2014 legacy) must be excluded from collectable ranking"
    # … but tracked in the not-collectable list, netted (credit notes applied).
    exc = cf.bsn_ar_excluded_by_customer(conn=tmp_db_conn)
    songphon = next((r for r in exc if (r['customer_code'] or '') == '94ท06'), None)
    assert songphon is not None and songphon['has_legacy'] == 1, "ทรงพล must appear as legacy"
    assert songphon['outstanding'] == pytest.approx(164322.73, abs=0.01), \
        f"ทรงพล must net to ฿164,322.73, got {songphon['outstanding']} (credit notes ignored?)"


def test_customer_ranking_invoice_count(tmp_db_conn):
    """Sum of per-customer invoice_count = 107 (collectable snapshot rows of
    net-positive customers, after excluding RE + pre-2024 legacy + write-offs;
    the 2026-06-24 วรสวัสดิ์ giveaway removed IV6900401/402/403 → 110→107).
    NB: LIVE-DATA anchor — recompute against the live DB on the next import,
    write-off, or payment (don't guess)."""
    rows = arf.customer_ranking(conn=tmp_db_conn)
    total_invoices = sum(r['invoice_count'] for r in rows)
    assert total_invoices == 107, \
        f"Expected invoice_count sum=107, got {total_invoices}"


def test_get_customer_ar_detail_live(tmp_db_conn):
    """Per-customer detail returns outstanding docs; spot-check that first
    customer by outstanding has matching total."""
    rows_ranking = arf.customer_ranking(conn=tmp_db_conn)
    assert rows_ranking, "Ranking must not be empty"
    top = rows_ranking[0]
    code = top['customer_code']

    detail = arf.get_customer_ar_detail(customer=code, conn=tmp_db_conn)
    assert len(detail) > 0, f"Detail for {code} must not be empty"
    detail_total = round(sum(d['outstanding'] for d in detail), 2)
    assert detail_total == pytest.approx(top['outstanding'], abs=0.01), \
        f"Detail total {detail_total} != ranking outstanding {top['outstanding']}"


# ── route-level: AR snapshot staleness banner (Finding 1) ────────────────────

STALE_AR_WARNING = 'ข้อมูลลูกหนี้เก่าเกิน 1 วัน'


def _admin_client(tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = 'admin'
    return c


def _force_bsn_snapshot_date(tmp_db, snap_date):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    changed = conn.execute(
        "UPDATE express_ar_outstanding SET snapshot_date_iso = ? WHERE entity = 'BSN'",
        (snap_date,),
    ).rowcount
    conn.commit()
    conn.close()
    assert changed > 0, 'live-clone fixture has no BSN AR rows to date-stamp'


def _a_live_customer_code(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT customer_code FROM express_ar_outstanding"
        " WHERE entity='BSN' AND TRIM(COALESCE(customer_code,'')) != ''"
        " LIMIT 1"
    ).fetchone()
    conn.close()
    assert row, 'live-clone fixture has no BSN AR rows to route to'
    return row[0]


def test_followup_detail_warns_when_snapshot_is_stale(tmp_db):
    code = _a_live_customer_code(tmp_db)
    _force_bsn_snapshot_date(tmp_db, '2020-01-02')
    r = _admin_client(tmp_db).get(f'/accounting/ar-followup/customer/{code}')
    assert r.status_code == 200
    body = r.data.decode()
    assert STALE_AR_WARNING in body
    assert '2020-01-02' in body


def test_followup_detail_does_not_warn_when_snapshot_is_fresh(tmp_db):
    """Control: the detail banner is conditional, not decoration."""
    code = _a_live_customer_code(tmp_db)
    _force_bsn_snapshot_date(tmp_db, date.today().isoformat())
    r = _admin_client(tmp_db).get(f'/accounting/ar-followup/customer/{code}')
    assert r.status_code == 200
    assert STALE_AR_WARNING not in r.data.decode()


# ═══════════════════════════════════════════════════════════════════════════
# Finding 5 — AR follow-up history is SOFT-deleted and attributed
# (mirrors the call-card pattern in call_card.py::soft_delete_log)
# ═══════════════════════════════════════════════════════════════════════════

def _log(conn, customer, code=None, log_date='2026-08-01', result='promised',
         next_action_date=None, created_by='admin'):
    return arf.log_outreach(customer=customer, customer_code=code,
                            log_date=log_date, channel='phone', result=result,
                            next_action_date=next_action_date,
                            created_by=created_by, conn=conn)


def test_delete_outreach_stamps_actor_and_time(empty_db_conn):
    c = empty_db_conn
    log_id = _log(c, 'ร้านทดสอบ', 'C-D1')
    c.commit()

    assert arf.delete_outreach(log_id, deleted_by='siang', conn=c) is True
    c.commit()

    row = c.execute("SELECT * FROM ar_followup_log WHERE id=?", (log_id,)).fetchone()
    assert row is not None, 'the physical row must survive — this is a SOFT delete'
    assert row['deleted_at'] is not None
    assert row['deleted_by'] == 'siang'


def test_delete_outreach_is_idempotent_and_reports_it(empty_db_conn):
    c = empty_db_conn
    log_id = _log(c, 'ร้านทดสอบ', 'C-D2')
    c.commit()

    assert arf.delete_outreach(log_id, deleted_by='siang', conn=c) is True
    assert arf.delete_outreach(log_id, deleted_by='siang', conn=c) is False, \
        'a second delete changed nothing and must say so'
    assert arf.delete_outreach(999999, deleted_by='siang', conn=c) is False


def test_deleted_log_disappears_from_history(empty_db_conn):
    c = empty_db_conn
    keep = _log(c, 'ร้านทดสอบ', 'C-D3', log_date='2026-08-01')
    drop = _log(c, 'ร้านทดสอบ', 'C-D3', log_date='2026-08-02')
    c.commit()
    assert len(arf.get_customer_followups('C-D3', conn=c)) == 2   # control

    arf.delete_outreach(drop, deleted_by='siang', conn=c)
    c.commit()

    ids = [r['id'] for r in arf.get_customer_followups('C-D3', conn=c)]
    assert ids == [keep]


def test_deleted_log_disappears_from_ranking(empty_db_conn):
    c = empty_db_conn
    _ins_express(c, 'IV-D4', 'C-D4', 'ร้านทดสอบ', '2026-05-01', 5000)
    older = _log(c, 'ร้านทดสอบ', 'C-D4', log_date='2026-07-01', result='no_answer')
    newer = _log(c, 'ร้านทดสอบ', 'C-D4', log_date='2026-07-20', result='promised')
    c.commit()
    ranked = {r['customer_code']: r for r in arf.customer_ranking(conn=c)}
    assert ranked['C-D4']['last_log_date'] == '2026-07-20'        # control

    arf.delete_outreach(newer, deleted_by='siang', conn=c)
    c.commit()

    ranked = {r['customer_code']: r for r in arf.customer_ranking(conn=c)}
    assert ranked['C-D4']['last_log_date'] == '2026-07-01', \
        'ranking still reports a deleted follow-up as the latest contact'
    assert ranked['C-D4']['last_log_result'] == 'no_answer'
    assert older  # referenced


def test_deleted_log_disappears_from_overdue(empty_db_conn):
    c = empty_db_conn
    log_id = _log(c, 'ร้านทดสอบ', 'C-D5', log_date='2026-07-01',
                  result='promised', next_action_date='2026-07-10')
    c.commit()
    assert len(arf.list_overdue_followups(as_of='2026-08-01', conn=c)) == 1   # control

    arf.delete_outreach(log_id, deleted_by='siang', conn=c)
    c.commit()

    assert arf.list_overdue_followups(as_of='2026-08-01', conn=c) == []


def test_deleted_log_does_not_resurrect_a_closed_account_as_overdue(empty_db_conn):
    """The overdue query has TWO subqueries (latest_with_action + latest_overall);
    filtering only one of them would let a deleted terminal log stop hiding a
    stale plan, or vice versa."""
    c = empty_db_conn
    _log(c, 'ร้านทดสอบ', 'C-D6', log_date='2026-07-01',
         result='promised', next_action_date='2026-07-10')
    terminal = _log(c, 'ร้านทดสอบ', 'C-D6', log_date='2026-07-20', result='paid_full')
    c.commit()
    assert arf.list_overdue_followups(as_of='2026-08-01', conn=c) == []       # control

    arf.delete_outreach(terminal, deleted_by='siang', conn=c)
    c.commit()

    overdue = arf.list_overdue_followups(as_of='2026-08-01', conn=c)
    assert len(overdue) == 1, 'deleting the terminal log must re-expose the open plan'


# ── route level ─────────────────────────────────────────────────────────────

def _seed_log_in(tmp_db, customer='ร้านเทสรูท', code='C-RT1'):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute(
        """INSERT INTO ar_followup_log
             (customer, customer_code, log_date, channel, result, created_by)
           VALUES (?,?,?,'phone','promised','admin')""",
        (customer, code, '2026-08-01'))
    conn.commit()
    log_id = cur.lastrowid
    conn.close()
    return log_id


def test_delete_route_records_the_session_username(tmp_db):
    import sqlite3
    log_id = _seed_log_in(tmp_db)
    c = _admin_client(tmp_db)
    r = c.post(f'/accounting/ar-followup/log/{log_id}/delete',
               data={'customer_key': 'C-RT1'}, follow_redirects=False)
    assert r.status_code == 302

    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT deleted_at, deleted_by FROM ar_followup_log WHERE id=?",
                       (log_id,)).fetchone()
    conn.close()
    assert row[0] is not None
    assert row[1] == 'admin', 'actor must be the session USERNAME, not display text'


def test_delete_route_does_not_flash_success_when_nothing_changed(tmp_db):
    log_id = _seed_log_in(tmp_db, code='C-RT2')
    c = _admin_client(tmp_db)
    # follow_redirects so the FIRST delete's success flash is consumed here —
    # an unconsumed flash would render on the next page and make the assertion
    # below fail for the wrong reason.
    first = c.post(f'/accounting/ar-followup/log/{log_id}/delete',
                   data={'customer_key': 'C-RT2'}, follow_redirects=True)
    assert 'ลบรายการแล้ว' in first.get_data(as_text=True)   # control

    r = c.post(f'/accounting/ar-followup/log/{log_id}/delete',
               data={'customer_key': 'C-RT2'}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert 'ลบรายการแล้ว' not in body, 'flashed success for a no-op delete'
    assert 'ถูกลบไปแล้ว' in body or 'ไม่พบรายการ' in body


# ═══════════════════════════════════════════════════════════════════════════
# Task 5 — follow-up identity resolved SERVER-SIDE; invalid input refused
# ═══════════════════════════════════════════════════════════════════════════

def test_resolve_customer_target_prefers_the_master_name(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO customers (code, name) VALUES ('C-R1','ชื่อใน master')")
    _ins_express(c, 'IV-R1', 'C-R1', 'ชื่อเก่าใน snapshot', '2026-05-01', 100)
    c.commit()

    got = arf.resolve_customer_target('C-R1', conn=c)

    assert got == {'customer_code': 'C-R1', 'customer': 'ชื่อใน master'}


def test_resolve_customer_target_falls_back_to_snapshot_then_log(empty_db_conn):
    c = empty_db_conn
    _ins_express(c, 'IV-R2', 'C-R2', 'ชื่อใน snapshot', '2026-05-01', 100)
    c.commit()
    assert arf.resolve_customer_target('C-R2', conn=c)['customer'] == 'ชื่อใน snapshot'

    _log(c, 'ชื่อในล็อก', 'C-R3')
    c.commit()
    assert arf.resolve_customer_target('C-R3', conn=c) == {
        'customer_code': 'C-R3', 'customer': 'ชื่อในล็อก'}


def test_resolve_customer_target_returns_none_for_an_unknown_key(empty_db_conn):
    assert arf.resolve_customer_target('NOPE-404', conn=empty_db_conn) is None
    assert arf.resolve_customer_target('', conn=empty_db_conn) is None


def _seed_two_customers(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM customers WHERE code IN ('C-KEYA','C-KEYB')")
    conn.execute("INSERT INTO customers (code, name) VALUES ('C-KEYA','ลูกค้า A')")
    conn.execute("INSERT INTO customers (code, name) VALUES ('C-KEYB','ลูกค้า B')")
    conn.execute("DELETE FROM ar_followup_log WHERE customer_code IN ('C-KEYA','C-KEYB')")
    conn.commit()
    conn.close()


def _logs_for(tmp_db, code):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT customer, customer_code, promised_amount, log_date"
        "  FROM ar_followup_log WHERE customer_code=?", (code,)).fetchall()
    conn.close()
    return rows


def _post_log(tmp_db, **form):
    data = {'customer_key': 'C-KEYA', 'log_date': '2026-08-10',
            'channel': 'phone', 'result': 'promised'}
    data.update(form)
    return _admin_client(tmp_db).post('/accounting/ar-followup/log/new',
                                      data=data, follow_redirects=False)


def test_forged_customer_fields_cannot_reattach_history(tmp_db):
    _seed_two_customers(tmp_db)
    r = _post_log(tmp_db, customer='ลูกค้า B', customer_code='C-KEYB')
    assert r.status_code == 302

    assert _logs_for(tmp_db, 'C-KEYB') == [], 'forged code attached history to the wrong customer'
    rows = _logs_for(tmp_db, 'C-KEYA')
    assert len(rows) == 1
    assert rows[0][0] == 'ลูกค้า A', 'stored name came from the form, not the server'


def test_unknown_customer_key_refuses_without_inserting(tmp_db):
    import sqlite3
    _seed_two_customers(tmp_db)
    conn = sqlite3.connect(tmp_db)
    before = conn.execute("SELECT COUNT(*) FROM ar_followup_log").fetchone()[0]
    conn.close()

    r = _admin_client(tmp_db).post('/accounting/ar-followup/log/new', data={
        'customer_key': 'NOPE-404', 'log_date': '2026-08-10',
        'channel': 'phone', 'result': 'promised'}, follow_redirects=False)
    assert r.status_code == 302

    conn = sqlite3.connect(tmp_db)
    after = conn.execute("SELECT COUNT(*) FROM ar_followup_log").fetchone()[0]
    conn.close()
    assert after == before


@pytest.mark.parametrize('bad', ['abc', '-1', '1,0,0,0.5.5'])
def test_invalid_promised_amount_refuses_without_inserting(tmp_db, bad):
    _seed_two_customers(tmp_db)
    _post_log(tmp_db, promised_amount=bad)
    assert _logs_for(tmp_db, 'C-KEYA') == [], f'{bad!r} was accepted'


def test_comma_formatted_promised_amount_is_stored(tmp_db):
    """Control: the money guard must not reject the normal input shape."""
    _seed_two_customers(tmp_db)
    _post_log(tmp_db, promised_amount='12,500.50')
    rows = _logs_for(tmp_db, 'C-KEYA')
    assert len(rows) == 1 and rows[0][2] == pytest.approx(12500.50)


@pytest.mark.parametrize('field', ['log_date', 'promised_date', 'next_action_date'])
def test_malformed_dates_refuse_without_inserting(tmp_db, field):
    _seed_two_customers(tmp_db)
    _post_log(tmp_db, **{field: '31/08/2569'})
    assert _logs_for(tmp_db, 'C-KEYA') == [], f'{field} accepted a malformed date'


def test_resolve_customer_target_accepts_a_code_whose_name_is_blank(empty_db_conn):
    """Regression, found by the running-app check: 038ก01 has real AR rows whose
    customer_name is EMPTY and no master row, so _resolve_target's name union
    (`customer_name != ''`) finds nothing. Refusing it made a genuine customer
    un-loggable. Put ruled every Express code is legitimate."""
    c = empty_db_conn
    _ins_express(c, 'IV-BLANK', '038ก01', '', '2026-05-01', 500)
    c.commit()

    got = arf.resolve_customer_target('038ก01', conn=c)

    assert got == {'customer_code': '038ก01', 'customer': '038ก01'}, \
        'a real but nameless customer code must still resolve'
    # control: an invented code with no evidence anywhere is still refused
    assert arf.resolve_customer_target('NO-SUCH-CODE', conn=c) is None
