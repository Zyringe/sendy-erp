"""TDD tests for inventory_app/ar_followup.py — AR follow-up workspace logic.

Synthetic data only (empty_db_conn schema clone). Pattern mirrors
test_cashflow.py and test_payments_alloc.py.

Logic under test
────────────────
customer_ranking(conn):
  - Aggregates payments_alloc.invoice_settlement by customer.
  - Returns rows with outstanding > 0 sorted by outstanding DESC.
  - Each row carries: customer, customer_code, invoice_count, outstanding,
    oldest_age_days, age_buckets {0-30,31-60,61-90,90+}, last_log (date/result).

log_outreach(conn, ...):
  - Inserts into ar_followup_log; rejects bad channel/result enums.

get_customer_followups(conn, customer):
  - Returns chronological list (log_date DESC) for one customer.

get_customer_ar_detail(conn, customer):
  - Returns list of outstanding invoices for one customer with age.
"""
import pytest
from datetime import date, timedelta

import ar_followup as arf


# ── synthetic data helpers ──────────────────────────────────────────────────

def _ins_sale(conn, doc_base, customer, customer_code, date_iso, net,
              line=1, vat_type=1):
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{doc_base}-{line}", doc_base, customer, customer_code,
         1, 'ตัว', net, vat_type, net, net),
    )


def _ins_receipt(conn, re_no, customer, date_iso, cancelled=0, total=None):
    cur = conn.execute(
        """INSERT INTO received_payments
           (re_no, date_iso, customer, salesperson, cancelled, total)
           VALUES (?,?,?,?,?,?)""",
        (re_no, date_iso, customer, 'S1', cancelled, total),
    )
    return cur.lastrowid


def _ins_paid(conn, re_id, iv_no, amount):
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?,?,?)",
        (re_id, iv_no, amount),
    )


# ── customer_ranking ────────────────────────────────────────────────────────

def test_ranking_sorts_by_outstanding_desc(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A',  'CA', today, 1000)
    _ins_sale(c, 'IV02', 'B',  'CB', today, 5000)
    _ins_sale(c, 'IV03', 'C',  'CC', today, 3000)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    customers = [r['customer'] for r in rows]
    assert customers == ['B', 'C', 'A']
    assert rows[0]['outstanding'] == pytest.approx(5000)


def test_ranking_excludes_fully_paid(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A', 'CA', today, 1000)
    _ins_sale(c, 'IV02', 'B', 'CB', today, 2000)
    rid = _ins_receipt(c, 'RE01', 'A', today, total=1000)
    _ins_paid(c, rid, 'IV01', 1000)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert [r['customer'] for r in rows] == ['B']


def test_ranking_buckets_by_age(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    _ins_sale(c, 'IV01', 'A', 'CA', (today - timedelta(days=10)).isoformat(), 100)   # 0-30
    _ins_sale(c, 'IV02', 'A', 'CA', (today - timedelta(days=45)).isoformat(), 200)   # 31-60
    _ins_sale(c, 'IV03', 'A', 'CA', (today - timedelta(days=75)).isoformat(), 300)   # 61-90
    _ins_sale(c, 'IV04', 'A', 'CA', (today - timedelta(days=180)).isoformat(), 400)  # 90+
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


def test_ranking_vat_type_2_adds_vat(empty_db_conn):
    """vat_type=2 (แยก VAT) — billed must be net*1.07 per codebase idiom."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A', 'CA', today, 1000, vat_type=2)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert rows[0]['outstanding'] == pytest.approx(1070.00, abs=0.01)


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
    today = date.today()
    _ins_sale(c, 'IV01', 'A', 'CA', (today - timedelta(days=5)).isoformat(), 1000)
    _ins_sale(c, 'IV02', 'A', 'CA', (today - timedelta(days=200)).isoformat(), 2000)
    _ins_sale(c, 'IV03', 'B', 'CB', today.isoformat(), 500)
    c.commit()

    rows = arf.get_customer_ar_detail(conn=c, customer='A')
    docs = sorted(r['doc_base'] for r in rows)
    assert docs == ['IV01', 'IV02']
    # age should be present
    iv02 = next(r for r in rows if r['doc_base'] == 'IV02')
    assert iv02['age_days'] == 200


# ── ranking joins last_log ──────────────────────────────────────────────────

def test_ranking_includes_last_log(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A', 'CA', today, 1000)
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=today,
                     channel='phone', result='promised',
                     next_action_date=today, created_by='admin')
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert rows[0]['last_log_date'] == today
    assert rows[0]['last_log_result'] == 'promised'
