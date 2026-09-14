"""TDD for #497 — the shared win-back computation (`winback.compute_winback`).

Replaces call_card.py's own `_compute_winback`: name-keyed, no evidence
filter (freebies / credit notes / not-a-sale documents all counted as
purchases). This module is population-agnostic — callers pass a WHERE +
params scope (the shape `models.customers._customer_sales_scope` builds);
these tests exercise the function directly against a synthetic customer's
sales_transactions rows, independent of how any caller builds that scope
(see test_497_customer_page_winback_notes.py for the call-card /
customer-page wiring).

Seam: pure computation over sales_transactions + price_lookup.evidence_filter.
Prior art for fixtures: tests/test_493_slice2_product_card.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import datetime as dt
import statistics

import pytest

SENDAI_BRAND_ID = 3  # เซ็นได — verified against the live dev DB (own-brand)

TEST_CODE = 'TEST4970'
TEST_NAME = 'ลูกค้าทดสอบ 497 วินแบ็ค'

_pid_counter = [497000]


def _mk_product(conn, name='สินค้าทดสอบวินแบ็ค', unit_type='ตัว', base=100.0, cost=60.0):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES (?,?,?,?,?,1)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, SENDAI_BRAND_ID),
    )
    conn.commit()
    return cur.lastrowid


def _mk_customer(conn, code=TEST_CODE, name=TEST_NAME):
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        (code, name),
    )
    conn.commit()


def _clear_customer(conn, code=TEST_CODE, name=TEST_NAME):
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ? OR customer = ?",
                 (code, name))
    conn.execute("DELETE FROM ar_writeoffs WHERE customer_code = ?", (code,))
    conn.commit()


def _line(conn, *, doc_base, suffix, pid, date_iso, qty, unit_price, net,
          vat_type=0, unit='ตัว', ref_invoice=None, total=None,
          customer_code=TEST_CODE, customer_name=TEST_NAME):
    doc_no = f"{doc_base}-{suffix}"
    if total is None:
        total = net
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net, ref_invoice) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, doc_no, doc_base, pid, customer_name, customer_code,
         qty, unit, unit_price, vat_type, total, net, ref_invoice),
    )
    conn.commit()


def _writeoff(conn, doc_no, code=TEST_CODE, excludes_revenue=1):
    conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, writeoff_date, "
        "excludes_revenue) VALUES (?,?,0,'writeback','2026-01-01',?)",
        (doc_no, code, excludes_revenue),
    )
    conn.commit()


@pytest.fixture
def cust(tmp_db_conn):
    _mk_customer(tmp_db_conn)
    _clear_customer(tmp_db_conn)
    pid = _mk_product(tmp_db_conn)
    yield tmp_db_conn, pid
    _clear_customer(tmp_db_conn)


def _scope():
    """The (WHERE, params) shape models.customers._customer_sales_scope
    builds for a code-keyed, unbounded-date scope — built by hand here so
    this file tests winback.py in isolation, without importing models."""
    return 'customer_code = ?', [TEST_CODE]


# ── Population: evidence_filter must apply ──────────────────────────────────

def test_two_paid_invoices_never_flagged_despite_other_document_types(cust):
    """The issue's own acceptance case: a product judged on paid invoices
    only, even though its history ALSO contains a freebie-only invoice, a
    credit note dated after the last invoice, and a not-a-sale (written-off)
    document. With only 2 REAL paid invoices, it must never be flagged —
    even though the raw row count (5) and raw distinct-date count (5) would
    both clear the >=3 threshold if evidence_filter were not applied."""
    conn, pid = cust
    _line(conn, doc_base='IV49700', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100)
    _line(conn, doc_base='IV49701', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=100, net=100)
    # Freebie-only invoice — qty>0 but net=0, must not count as a purchase date.
    _line(conn, doc_base='IV49702', suffix=1, pid=pid, date_iso='2026-03-01',
          qty=1, unit_price=0, net=0)
    # Credit note dated AFTER the last real invoice.
    _line(conn, doc_base='SR49703', suffix=1, pid=pid, date_iso='2026-04-01',
          qty=1, unit_price=100, net=100, ref_invoice='IV49701')
    # Not-a-sale document (written off, excludes_revenue=1).
    _line(conn, doc_base='IV49704', suffix=1, pid=pid, date_iso='2026-05-01',
          qty=1, unit_price=100, net=100)
    _writeoff(conn, 'IV49704')

    import winback
    where, params = _scope()
    rows = winback.compute_winback(conn, where, params, today=dt.date(2026, 9, 15))
    assert rows == []


def test_flags_a_product_with_three_evidenced_purchases_and_a_stale_gap(cust):
    conn, pid = cust
    dates = ['2024-01-01', '2024-02-12', '2024-03-26']
    for i, d in enumerate(dates):
        _line(conn, doc_base=f'IV4971{i}', suffix=1, pid=pid, date_iso=d,
              qty=1, unit_price=100, net=100)

    import winback
    where, params = _scope()
    today = dt.date(2026, 1, 10)
    rows = winback.compute_winback(conn, where, params, today=today)
    assert len(rows) == 1
    row = rows[0]

    date_objs = [dt.date.fromisoformat(d) for d in dates]
    gaps = [(date_objs[i + 1] - date_objs[i]).days for i in range(len(date_objs) - 1)]
    expected_median = int(statistics.median(gaps))
    expected_days_since = (today - date_objs[-1]).days

    assert row['product_id'] == pid
    assert row['unit'] == 'ตัว'
    assert row['last_buy'] == dates[-1]
    assert row['median_gap_days'] == expected_median
    assert row['days_since'] == expected_days_since


def test_three_invoices_on_two_dates_is_eligible_and_flags_when_lapsed(cust):
    """The real shape found on prod (01อ06 / pid 398 / แผ่น): eligibility is
    ครั้งที่ซื้อ (distinct INVOICES) >= 3, matching the product card's own
    `COUNT(DISTINCT s.doc_base)` — NOT distinct dates. Two invoices dated the
    same day are still two separate ครั้ง, so 3 invoices spread over only 2
    dates must be eligible (not blocked at the raw 2-invoices-one-day
    scenario this test replaces)."""
    conn, pid = cust
    _line(conn, doc_base='IV49720', suffix=1, pid=pid, date_iso='2024-01-01',
          qty=1, unit_price=100, net=100)
    _line(conn, doc_base='IV49721', suffix=1, pid=pid, date_iso='2024-01-01',
          qty=1, unit_price=100, net=100)
    _line(conn, doc_base='IV49722', suffix=1, pid=pid, date_iso='2024-06-01',
          qty=1, unit_price=100, net=100)

    import winback
    where, params = _scope()
    # 3 ครั้ง (doc_bases), 2 distinct dates -> one gap of 152 days
    # (2024-01-01 to 2024-06-01). Far enough past that gap -> flagged.
    rows = winback.compute_winback(conn, where, params, today=dt.date(2026, 1, 1))
    assert len(rows) == 1
    assert rows[0]['product_id'] == pid
    assert rows[0]['median_gap_days'] == 152


def test_three_invoices_on_one_date_is_eligible_but_never_flagged_no_gap(cust):
    """3 ครั้ง clears the eligibility threshold, but all three share ONE
    date — there are no two distinct dates to compute a gap from, so this
    must never flag regardless of how long ago that date was."""
    conn, pid = cust
    for i in range(3):
        _line(conn, doc_base=f'IV4973{i}', suffix=1, pid=pid, date_iso='2020-01-01',
              qty=1, unit_price=100, net=100)

    import winback
    where, params = _scope()
    rows = winback.compute_winback(conn, where, params, today=dt.date(2026, 1, 1))
    assert rows == []


def test_two_invoices_on_two_dates_is_not_eligible(cust):
    """Below the >=3 ครั้ง threshold, however many distinct dates it spans."""
    conn, pid = cust
    _line(conn, doc_base='IV49740', suffix=1, pid=pid, date_iso='2020-01-01',
          qty=1, unit_price=100, net=100)
    _line(conn, doc_base='IV49741', suffix=1, pid=pid, date_iso='2020-06-01',
          qty=1, unit_price=100, net=100)

    import winback
    where, params = _scope()
    rows = winback.compute_winback(conn, where, params, today=dt.date(2026, 1, 1))
    assert rows == []


def test_not_flagged_when_days_since_is_within_the_median_gap(cust):
    conn, pid = cust
    dates = ['2026-01-01', '2026-02-01', '2026-03-01']  # ~31-day gaps
    for i, d in enumerate(dates):
        _line(conn, doc_base=f'IV4973{i}', suffix=1, pid=pid, date_iso=d,
              qty=1, unit_price=100, net=100)

    import winback
    where, params = _scope()
    # 10 days after the last purchase — well inside a ~31-day cadence.
    rows = winback.compute_winback(conn, where, params, today=dt.date(2026, 3, 11))
    assert rows == []


def test_rows_with_no_product_id_are_skipped(cust):
    conn, pid = cust
    for i, d in enumerate(['2024-01-01', '2024-02-01', '2024-03-01']):
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, "
            "customer, customer_code, qty, unit, unit_price, vat_type, total, net) "
            "VALUES (?,?,?,NULL,?,?,1,'ตัว',100,0,100,100)",
            (d, f'IVNOPID{i}-1', f'IVNOPID{i}', TEST_NAME, TEST_CODE),
        )
    conn.commit()

    import winback
    where, params = _scope()
    rows = winback.compute_winback(conn, where, params, today=dt.date(2026, 1, 1))
    assert rows == []


def test_sorted_longest_lapse_first(cust):
    conn, pid = cust
    pid2 = _mk_product(conn, name='สินค้าทดสอบวินแบ็คสอง')
    # pid: last purchase long ago -> big days_since.
    for i, d in enumerate(['2020-01-01', '2020-02-01', '2020-03-01']):
        _line(conn, doc_base=f'IV4974{i}', suffix=1, pid=pid, date_iso=d,
              qty=1, unit_price=100, net=100)
    # pid2: same cadence, but a more recent last purchase -> smaller days_since.
    for i, d in enumerate(['2025-01-01', '2025-02-01', '2025-03-01']):
        _line(conn, doc_base=f'IV4975{i}', suffix=1, pid=pid2, date_iso=d,
              qty=1, unit_price=100, net=100)

    import winback
    where, params = _scope()
    rows = winback.compute_winback(conn, where, params, today=dt.date(2026, 1, 1))
    assert [r['product_id'] for r in rows] == [pid, pid2]
