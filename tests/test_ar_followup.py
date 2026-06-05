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
    As of the 2026-06-05 snapshot with the 58 write-off decisions loaded:
    = 34 customers / ฿496,018.48.

    ทรงพลเทรดดิ้ง is entirely 2014 legacy → it drops out of the collectable
    ranking and is tracked in the not-collectable list instead (net ฿164,322.73,
    NOT ฿284,863.10 — credit-note netting guard).
    NB: LIVE-DATA anchor — recompute against the live DB on the next import or
    write-off (don't guess)."""
    import cashflow as cf
    rows = arf.customer_ranking(conn=tmp_db_conn)
    total = round(sum(r['outstanding'] for r in rows), 2)
    assert len(rows) == 34, f"Expected 34 collectable net-positive customers, got {len(rows)}"
    assert total == pytest.approx(496018.48, abs=0.01), \
        f"Expected canonical collectable total ฿496,018.48, got {total}"
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
    """Sum of per-customer invoice_count = 110 (collectable snapshot rows of
    net-positive customers, after excluding RE + pre-2024 legacy + write-offs).
    NB: LIVE-DATA anchor — recompute against the live DB on the next import or
    write-off (don't guess)."""
    rows = arf.customer_ranking(conn=tmp_db_conn)
    total_invoices = sum(r['invoice_count'] for r in rows)
    assert total_invoices == 110, \
        f"Expected invoice_count sum=110, got {total_invoices}"


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
