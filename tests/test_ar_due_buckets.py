"""Split collectable AR by WHEN it is actually chaseable, not just by age.

`ar_aging` buckets by how old a document is. That answers "how bad is the
book", not "who do I call today" -- an invoice on 30-day terms issued last week
is 7 days old and not yet owed, while one on cash terms from the same week is
overdue. The dunning list needs the second question.

Three buckets, from columns the Express DBF already carries:

  already_billed  bill_no is set -- an ใบวางบิล has gone out and the customer
                  is on their own payment run; chasing again is noise
  not_yet_due     due_date_iso is still in the future
  chase_now       everything else

⚠ POPULATION. These split the SAME rows `ar_aging` totals -- the canonical
BSN_AR_PREDICATE on the latest BSN snapshot -- so the three must reconcile to
that total to the satang. That is asserted here, because a dunning list that
quietly drops rows is worse than no dunning list.

⚠ NOT the whole debt. This is the novat book only. Measured 2026-08-20, 31
open invoices worth ฿144,593.16 exist ONLY in the xp5 VAT book and are invisible
here; 14 more (฿117,203.75) are the SAME sales invoiced in both books, which
Put ruled on 2026-08-20 are owed ONCE. Merging the two needs an invoice-level
identity map that does not exist (name, customer_code and doc_no all differ
across books), so this deliberately does not try.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import cashflow as cf


SNAP = '2026-08-20'


def _row(conn, doc_no, outstanding, *, due=None, bill_no=None,
         doc_date='2026-08-01', anomalous=0):
    conn.execute(
        "INSERT INTO express_ar_outstanding (batch_id, entity, snapshot_date_iso,"
        " customer_code, customer_name, doc_date_iso, doc_no, is_anomalous,"
        " bill_amount, paid_amount, outstanding_amount, due_date_iso, bill_no)"
        " VALUES (1, 'BSN', ?, 'C1', 'ลูกค้าทดสอบ', ?, ?, ?, ?, 0, ?, ?, ?)",
        (SNAP, doc_date, doc_no, anomalous, outstanding, outstanding,
         due or doc_date, bill_no))
    conn.commit()


def _clean(conn):
    """Force the state: tmp_db/empty_db carry real rows, and inheriting them
    would make every total below meaningless. Also seeds the batch the rows'
    FK points at (express_ar_outstanding.batch_id -> express_import_log.id)."""
    conn.execute("DELETE FROM express_ar_outstanding")
    conn.execute("DELETE FROM ar_writeoffs")
    conn.execute(
        "INSERT OR IGNORE INTO express_import_log (id, file_type,"
        " source_filename, record_count, line_count, status)"
        " VALUES (1, 'ar_snapshot', 'test', 0, 0, 'imported')")
    conn.commit()


def test_the_three_buckets_reconcile_to_ar_aging_to_the_satang(empty_db_conn):
    """The invariant that matters: splitting must not lose or invent money."""
    _clean(empty_db_conn)
    _row(empty_db_conn, 'IV1', 1000.00, due='2026-09-30')            # not yet due
    _row(empty_db_conn, 'IV2', 250.25,  due='2026-08-01')            # chase now
    _row(empty_db_conn, 'IV3', 99.75,   due='2026-08-01', bill_no='BL1')
    _row(empty_db_conn, 'IV4', 0.01,    due='2026-08-19')            # chase now

    res = cf.ar_due_buckets(as_of=SNAP, conn=empty_db_conn)
    aging = cf.ar_aging(conn=empty_db_conn)

    total = round(sum(b['amount'] for b in res['buckets']), 2)
    assert total == round(aging['total_outstanding'], 2), (res, aging)
    assert sum(b['count'] for b in res['buckets']) == 4


def test_each_bucket_gets_the_right_rows(empty_db_conn):
    _clean(empty_db_conn)
    _row(empty_db_conn, 'IV1', 1000.00, due='2026-09-30')
    _row(empty_db_conn, 'IV2', 250.25,  due='2026-08-01')
    _row(empty_db_conn, 'IV3', 99.75,   due='2026-08-01', bill_no='BL1')

    by = {b['key']: b for b in cf.ar_due_buckets(as_of=SNAP,
                                                 conn=empty_db_conn)['buckets']}
    assert by['not_yet_due']['amount'] == 1000.00
    assert by['chase_now']['amount'] == 250.25
    assert by['already_billed']['amount'] == 99.75


def test_billed_wins_over_overdue(empty_db_conn):
    """A billed invoice is on the customer's payment run even if its own due
    date has passed -- chasing it again is the noise this split removes."""
    _clean(empty_db_conn)
    _row(empty_db_conn, 'IV1', 500.00, due='2026-01-01', bill_no='BL9')
    by = {b['key']: b for b in cf.ar_due_buckets(as_of=SNAP,
                                                 conn=empty_db_conn)['buckets']}
    assert by['already_billed']['count'] == 1
    assert by['chase_now']['count'] == 0


def test_a_blank_bill_no_is_not_billed(empty_db_conn):
    """Express writes '' rather than NULL in places; '' must not read as billed."""
    _clean(empty_db_conn)
    _row(empty_db_conn, 'IV1', 500.00, due='2026-08-01', bill_no='')
    by = {b['key']: b for b in cf.ar_due_buckets(as_of=SNAP,
                                                 conn=empty_db_conn)['buckets']}
    assert by['chase_now']['count'] == 1
    assert by['already_billed']['count'] == 0


def test_the_canonical_filter_still_applies(empty_db_conn):
    """Same population as ar_aging: anomalous rows, pre-2024 debt and
    accountant write-offs are all excluded -- otherwise the reconcile above
    would pass while the dunning list showed rows nobody should chase."""
    _clean(empty_db_conn)
    _row(empty_db_conn, 'IV_OK',   100.00, due='2026-08-01')
    _row(empty_db_conn, 'IV_ANOM', 900.00, due='2026-08-01', anomalous=1)
    _row(empty_db_conn, 'IV_OLD',  900.00, due='2020-01-01', doc_date='2020-01-01')
    _row(empty_db_conn, 'IV_WO',   900.00, due='2026-08-01')
    empty_db_conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, amount, type, writeoff_date, reason,"
        " excludes_revenue) VALUES ('IV_WO', 900.00, 'expense', '2026-08-01',"
        " 'test', 0)")
    empty_db_conn.commit()

    res = cf.ar_due_buckets(as_of=SNAP, conn=empty_db_conn)
    assert round(sum(b['amount'] for b in res['buckets']), 2) == 100.00
    assert sum(b['count'] for b in res['buckets']) == 1


def test_empty_snapshot_returns_zeroed_buckets_not_an_error(empty_db_conn):
    _clean(empty_db_conn)
    res = cf.ar_due_buckets(as_of=SNAP, conn=empty_db_conn)
    assert [b['key'] for b in res['buckets']] == \
        ['chase_now', 'not_yet_due', 'already_billed']
    assert all(b['amount'] == 0 and b['count'] == 0 for b in res['buckets'])


def test_the_ar_page_renders_the_buckets(tmp_db):
    """A computed split nobody can see is not a feature. Assert the rendered
    LABELS and that the page still carries its existing AR total -- a template
    that raised would 500 long before pytest noticed the numbers."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'u'
        s['role'] = 'admin'
    r = c.get('/ar?tab=overview')
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'ตามหนี้ได้แค่ไหน' in body
    for label in ('ตามได้เลย', 'ยังไม่ถึงกำหนด', 'วางบิลแล้ว'):
        assert '>%s<' % label in body or label in body, label
    # the cross-book caveat must be visible, not just in a docstring
    assert 'ยังไม่รวมสมุด VAT' in body
