"""Receipt status (สถานะรับชำระ) of a Sendy-ledger invoice — ADR 0024.

One row per receivable invoice with four statuses: paid, partial, unpaid and
written_off (ตัดหนี้แล้ว). Built on payments_alloc's amount engine, so a partial
receipt reads partial and a credit note that clears the bill reads paid.
"""
import random

import pytest

import receipt_status
from tests.test_payments_alloc import _ins_paid, _ins_receipt, _ins_sale, _ins_sr


def _write_off(conn, doc_no, excludes_revenue=0):
    conn.execute("""INSERT INTO ar_writeoffs
                      (doc_no, customer_name, amount, type, writeoff_date, excludes_revenue)
                    VALUES (?, 'x', 0, 'expense', '2026-08-01', ?)""",
                 (doc_no, excludes_revenue))


def _credit(conn, sr_no, ref_invoice, amount, date_iso='2026-07-10'):
    conn.execute("""INSERT INTO credit_note_amounts
                      (sr_doc_base, ref_invoice, credited_amount, sr_date_iso)
                    VALUES (?, ?, ?, ?)""", (sr_no, ref_invoice, amount, date_iso))


def _rows(conn, **kw):
    return {r['doc_base']: r for r in receipt_status.rows(conn=conn, **kw)}


def test_each_of_the_four_statuses(empty_db_conn):
    c = empty_db_conn
    for doc in ('IV-PAID', 'IV-PART', 'IV-UNPAID', 'IV-WO'):
        _ins_sale(c, doc, 'A', 'C-A', '2026-07-01', 1000.0)
    r = _ins_receipt(c, 'RE-1', 'A', '2026-07-05')
    _ins_paid(c, r, 'IV-PAID', 1000.0)
    _ins_paid(c, r, 'IV-PART', 400.0)
    _write_off(c, 'IV-WO')
    c.commit()

    rows = _rows(c)

    assert {d: rows[d]['status'] for d in rows} == {
        'IV-PAID': 'paid', 'IV-PART': 'partial',
        'IV-UNPAID': 'unpaid', 'IV-WO': 'written_off'}
    assert rows['IV-PART']['collected'] == pytest.approx(400.0)
    assert rows['IV-PART']['remainder'] == pytest.approx(600.0)
    assert rows['IV-UNPAID']['remainder'] == pytest.approx(1000.0)
    assert rows['IV-PAID']['last_payment_date'] == '2026-07-05'
    assert set(rows['IV-PAID']) >= {'doc_base', 'customer', 'customer_code', 'invoice_date',
                                    'billed', 'collected', 'remainder', 'status',
                                    'written_off', 'last_payment_date'}


def test_fully_received_write_off_stays_paid_but_is_flagged(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-WO-PAID', 'A', 'C-A', '2026-07-01', 1000.0)
    r = _ins_receipt(c, 'RE-1', 'A', '2026-07-30')
    _ins_paid(c, r, 'IV-WO-PAID', 1000.0)
    _write_off(c, 'IV-WO-PAID')
    c.commit()

    row = _rows(c)['IV-WO-PAID']

    assert row['status'] == 'paid'
    assert row['written_off'] is True


def test_part_paid_write_off_reads_written_off(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-WO-PART', 'A', 'C-A', '2026-07-01', 1000.0)
    r = _ins_receipt(c, 'RE-1', 'A', '2026-07-30')
    _ins_paid(c, r, 'IV-WO-PART', 250.0)
    _write_off(c, 'IV-WO-PART')
    c.commit()

    row = _rows(c)['IV-WO-PART']

    assert row['status'] == 'written_off'
    assert row['written_off'] is True
    assert row['remainder'] == pytest.approx(750.0)


def test_write_off_is_the_whole_table_not_the_revenue_flag(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-F0', 'A', 'C-A', '2026-07-01', 100.0)
    _ins_sale(c, 'IV-F1', 'A', 'C-A', '2026-07-01', 100.0)
    _write_off(c, 'IV-F0', excludes_revenue=0)
    _write_off(c, 'IV-F1', excludes_revenue=1)
    c.commit()

    rows = _rows(c)

    assert rows['IV-F0']['status'] == rows['IV-F1']['status'] == 'written_off'


def test_an_invoice_outside_ar_writeoffs_is_not_flagged(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-1', 'A', 'C-A', '2026-07-01', 100.0)
    c.commit()
    assert _rows(c)['IV-1']['written_off'] is False


def test_cancelled_receipt_is_not_a_payment(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-C', 'A', 'C-A', '2026-07-01', 1000.0)
    rc = _ins_receipt(c, 'RE-X', 'A', '2026-07-05', cancelled=1)
    _ins_paid(c, rc, 'IV-C', 1000.0)
    c.commit()

    row = _rows(c)['IV-C']

    assert row['status'] == 'unpaid'
    assert row['collected'] == 0.0
    assert row['last_payment_date'] is None


def test_null_amount_legacy_link_reads_paid(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-L', 'A', 'C-A', '2026-07-01', 1000.0)
    r = _ins_receipt(c, 'RE-L', 'A', '2026-07-05')
    _ins_paid(c, r, 'IV-L', None)
    c.commit()

    row = _rows(c)['IV-L']

    assert row['status'] == 'paid'
    assert row['remainder'] == 0.0


def test_credit_note_offsets_the_remainder(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-CN', 'A', 'C-A', '2026-07-01', 1000.0)
    _credit(c, 'SR-1', 'IV-CN', 300.0)
    c.commit()

    row = _rows(c)['IV-CN']

    assert row['status'] == 'unpaid'
    assert row['billed'] == pytest.approx(1000.0)
    assert row['remainder'] == pytest.approx(700.0)


def test_credit_note_that_clears_the_bill_reads_paid(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-FC', 'A', 'C-A', '2026-07-01', 80.0)
    _credit(c, 'SR-2', 'IV-FC', 80.0)
    c.commit()

    row = _rows(c)['IV-FC']

    assert row['status'] == 'paid'
    assert row['remainder'] == 0.0


def test_sr_fallback_credit_note_offsets_too(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-SRF', 'A', 'C-A', '2026-07-01', 1000.0)
    _ins_sr(c, 'SR-F', 'IV-SRF', 'A', 'C-A', '2026-07-03', 200.0)
    c.commit()

    rows = _rows(c)

    assert 'SR-F' not in rows, 'a credit note is never itself a receivable'
    assert rows['IV-SRF']['remainder'] == pytest.approx(800.0)


def test_zero_total_invoice_is_not_receivable(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-ZERO', 'A', 'C-A', '2026-07-01', 0.0)
    _ins_sale(c, 'IV-REAL', 'A', 'C-A', '2026-07-01', 10.0)
    c.commit()

    rows = _rows(c)

    assert 'IV-REAL' in rows, 'control'
    assert 'IV-ZERO' not in rows


def test_hs_cash_sale_is_never_receivable(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'HS-1', 'A', 'C-A', '2026-07-01', 500.0)
    _ins_sale(c, 'IV-1', 'A', 'C-A', '2026-07-01', 500.0)
    c.commit()

    rows = _rows(c)

    assert 'IV-1' in rows, 'control'
    assert 'HS-1' not in rows


def test_two_codes_sharing_one_bill_name(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-A1', 'ร้านเดียวกัน', 'C-1', '2026-07-01', 100.0)
    _ins_sale(c, 'IV-A2', 'ร้านเดียวกัน', 'C-2', '2026-07-02', 200.0)
    _ins_sale(c, 'IV-B', 'ร้านอื่น', 'C-3', '2026-07-02', 300.0)
    c.commit()

    assert set(_rows(c, customer_code='C-1')) == {'IV-A1'}
    assert set(_rows(c, customer_code='C-2')) == {'IV-A2'}
    assert set(_rows(c, customer='ร้านเดียวกัน')) == {'IV-A1', 'IV-A2'}


def test_as_of_ignores_later_receipts(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-T', 'A', 'C-A', '2026-07-01', 1000.0)
    r = _ins_receipt(c, 'RE-T', 'A', '2026-08-15')
    _ins_paid(c, r, 'IV-T', 1000.0)
    c.commit()

    assert _rows(c, as_of='2026-07-31')['IV-T']['status'] == 'unpaid'
    assert _rows(c)['IV-T']['status'] == 'paid'



def test_as_of_ignores_a_write_off_recorded_later(empty_db_conn):
    """IV6701775 was written off on 2026-06-05; as of May it was still unpaid."""
    c = empty_db_conn
    _ins_sale(c, 'IV-WO-LATE', 'A', 'C-A', '2026-07-01', 1000.0)
    _write_off(c, 'IV-WO-LATE')                      # writeoff_date 2026-08-01
    c.commit()

    before = _rows(c, as_of='2026-07-31')['IV-WO-LATE']
    assert (before['status'], before['written_off']) == ('unpaid', False)
    on_the_day = _rows(c, as_of='2026-08-01')['IV-WO-LATE']
    assert (on_the_day['status'], on_the_day['written_off']) == ('written_off', True)

def _seed_random(c, rng, n_invoices):
    """Returns the facts each invoice was built from, so the rules below are
    checked against what was inserted, not against either implementation."""
    amounts = [100.0, 200.0, 1070.0, 333.33]
    names = [('ร้าน ก', 'C-1'), ('ร้าน ก', 'C-2'), ('ร้าน ข', 'C-3')]
    facts = {}
    re_seq = 0
    for i in range(n_invoices):
        doc = f'IV-R{i:03d}'
        billed = rng.choice(amounts)
        name, code = rng.choice(names)
        _ins_sale(c, doc, name, code, rng.choice(['2026-07-01', '2026-07-02']), billed)
        f = {'billed': billed, 'active': [], 'cn': 0.0, 'written_off': False}
        for _ in range(rng.choice([0, 0, 1, 1, 2])):
            re_seq += 1
            cancelled = rng.random() < 0.2
            amount = rng.choice([None, billed, billed, round(billed / 2, 2), 0.0,
                                 round(billed + 10, 2)])
            r = _ins_receipt(c, f'RE-R{re_seq:04d}', name,
                             rng.choice(['2026-07-05', '2026-07-06']),
                             cancelled=1 if cancelled else 0)
            _ins_paid(c, r, doc, amount)
            if not cancelled:
                f['active'].append(amount)
        if rng.random() < 0.15:
            cn = rng.choice([billed, round(billed / 4, 2)])
            _credit(c, f'SR-R{i:03d}', doc, cn)
            f['cn'] = cn
        if rng.random() < 0.1:
            _write_off(c, doc)
            f['written_off'] = True
        facts[doc] = f
    c.commit()
    return facts


def _rule(f, trunk, head):
    real = [a for a in f['active'] if a is not None]
    net_owed = round(f['billed'] - f['cn'], 2)
    if real:
        collected = round(sum(real), 2)
    elif f['active']:
        collected = net_owed
    else:
        collected = 0.0
    if head == 'written_off' and f['written_off'] and collected < net_owed:
        return 'written off, not fully received'
    if trunk == 'paid' and head == 'partial' and 0 < collected < net_owed:
        return 'partial receipt'
    if trunk == 'paid' and head == 'unpaid' and real and collected <= 0:
        return 'active links carry no money'
    if trunk == 'unpaid' and head == 'paid' and f['cn'] and net_owed <= 0:
        return 'cleared by credit notes'
    return None


@pytest.mark.parametrize('seed', range(12))
def test_differential_against_frozen_trunk(empty_db_conn, seed):
    from tests._fixtures import payment_status_9e466e2 as trunk_mod

    facts = _seed_random(empty_db_conn, random.Random(seed), 60)

    trunk_rows, trunk_total = trunk_mod.get_payment_status(per_page=10 ** 6)
    trunk = {r['doc_base']: ('paid' if r['is_paid'] else 'unpaid', round(r['total_net'], 2))
             for r in trunk_rows}
    head = {r['doc_base']: r for r in receipt_status.rows()}

    assert set(head) == set(trunk) and len(head) == trunk_total == len(facts)
    named = 0
    for doc, f in facts.items():
        t_status, t_amount = trunk[doc]
        h = head[doc]
        assert h['billed'] == pytest.approx(t_amount, abs=0.005)
        assert h['written_off'] is f['written_off']
        clean = (not f['cn'] and not f['written_off']
                 and all(a is None or a == f['billed'] for a in f['active']))
        if clean:
            assert h['status'] == t_status, (doc, f)
        elif h['status'] != t_status:
            assert _rule(f, t_status, h['status']), (doc, f, t_status, h['status'])
            named += 1
    # The generator must actually produce disagreements, or this proves nothing.
    assert named > 0


@pytest.fixture
def four_statuses(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-PAID', 'A', 'C-A', '2026-07-01', 1000.0)
    _ins_sale(c, 'IV-PART', 'A', 'C-A', '2026-07-02', 1000.0)
    _ins_sale(c, 'IV-UNPAID', 'B', 'C-B', '2026-07-03', 500.0)
    _ins_sale(c, 'IV-CN', 'B', 'C-B', '2026-07-04', 1000.0)
    _ins_sale(c, 'IV-WO', 'B', 'C-B', '2026-07-05', 777.0)
    r = _ins_receipt(c, 'RE-1', 'A', '2026-07-10')
    _ins_paid(c, r, 'IV-PAID', 1000.0)
    _ins_paid(c, r, 'IV-PART', 400.0)
    _credit(c, 'SR-1', 'IV-CN', 300.0)
    _write_off(c, 'IV-WO')
    c.commit()
    return c


def test_invoice_list_filters_by_each_status(four_statuses):
    import models
    want = {'paid': ['IV-PAID'], 'partial': ['IV-PART'],
            'unpaid': ['IV-CN', 'IV-UNPAID'], 'written_off': ['IV-WO']}
    for status, docs in want.items():
        rows, total, _summary = models.get_payment_status(status=status)
        assert sorted(r['doc_base'] for r in rows) == docs, status
        assert total == len(docs)
    rows, total, _summary = models.get_payment_status()
    assert total == 5
    assert [r['doc_base'] for r in rows] == ['IV-WO', 'IV-CN', 'IV-UNPAID', 'IV-PART', 'IV-PAID']


def test_invoice_list_keeps_search_date_range_and_paging(four_statuses):
    import models
    rows, total, _summary = models.get_payment_status(search='part')
    assert [r['doc_base'] for r in rows] == ['IV-PART'] and total == 1
    rows, total, _summary = models.get_payment_status(search='B')
    assert total == 3, 'search matches the customer name too'
    rows, total, _summary = models.get_payment_status(date_from='2026-07-02', date_to='2026-07-04')
    assert sorted(r['doc_base'] for r in rows) == ['IV-CN', 'IV-PART', 'IV-UNPAID']
    rows, total, _summary = models.get_payment_status(page=2, per_page=2)
    assert total == 5 and [r['doc_base'] for r in rows] == ['IV-UNPAID', 'IV-PART']


def test_invoice_list_shows_the_newest_receipt(four_statuses):
    import models
    rows, _, _summary = models.get_payment_status(status='paid')
    assert rows[0]['re_no'] == 'RE-1' and rows[0]['paid_date'] == '2026-07-10'
    rows, _, _summary = models.get_payment_status(status='unpaid')
    assert all(r['re_no'] is None for r in rows)


def test_summary_counts_every_receivable_once(four_statuses):
    import models
    s = models.get_payment_status()[2]
    assert (s['total_bills'], s['paid_count'], s['partial_count'],
            s['unpaid_count'], s['written_off_count']) == (5, 1, 1, 2, 1)
    assert s['paid_count'] + s['partial_count'] + s['unpaid_count'] \
        + s['written_off_count'] == s['total_bills']
    assert s['paid_billed'] == pytest.approx(1000.0)
    assert s['partial_remainder'] == pytest.approx(600.0)
    assert s['unpaid_remainder'] == pytest.approx(500.0 + 700.0)
    assert s['written_off_billed'] == pytest.approx(777.0)


def test_reconcile_ledger_is_the_open_remainder_per_code(four_statuses):
    import models
    rec = models.get_ar_reconciliation()
    ledger = {r['customer_code']: r['ledger_amount'] for r in rec['rows']}
    assert ledger == {'C-A': pytest.approx(600.0), 'C-B': pytest.approx(1200.0)}
    assert rec['ledger_total'] == pytest.approx(1800.0)


def test_matcher_offers_remainders_and_never_a_write_off(four_statuses):
    import models
    hits = models.find_payment_candidates(600.0, tolerance=0)
    assert [(h['customer_code'], [b['doc_base'] for b in h['matched_bills']]) for h in hits] \
        == [('C-A', ['IV-PART'])]
    hits = models.find_payment_candidates(700.0, tolerance=0)
    assert [[b['doc_base'] for b in h['matched_bills']] for h in hits] == [['IV-CN']]
    assert models.find_payment_candidates(1000.0, tolerance=0) == [], \
        'IV-CN billed 1000 but owes 700; IV-PAID is paid'
    assert models.find_payment_candidates(777.0, tolerance=0) == []
    b = [h for h in models.find_payment_candidates(1200.0, tolerance=0)
         if h['customer_code'] == 'C-B'][0]
    assert b['total_unpaid_bills'] == 2 and b['total_outstanding'] == pytest.approx(1200.0)
    assert {x['vat_type'] for x in b['matched_bills']} == {1}



# An import can commit between two reads of one request. Each test injects that
# write at the seam right after the status rows are read, before the next read.

def _write_after_rows(monkeypatch, sql_statements):
    import database
    real = receipt_status.rows

    def rows_then_write(*args, **kwargs):
        out = real(*args, **kwargs)
        w = database.get_connection()
        for sql, params in sql_statements:
            w.execute(sql, params)
        w.commit()
        w.close()
        return out

    monkeypatch.setattr(receipt_status, 'rows', rows_then_write)


def test_matcher_survives_an_invoice_deleted_mid_request(empty_db_conn, monkeypatch):
    import models
    c = empty_db_conn
    _ins_sale(c, 'IV-RACE', 'A', 'C-A', '2026-07-01', 420.0)
    c.commit()
    _write_after_rows(monkeypatch, [
        ("DELETE FROM sales_transactions WHERE doc_base = ?", ('IV-RACE',))])

    hits = models.find_payment_candidates(420.0, tolerance=0)

    assert [[b['doc_base'] for b in h['matched_bills']] for h in hits] == [['IV-RACE']]


def test_invoice_list_never_shows_a_receipt_its_status_did_not_see(empty_db_conn, monkeypatch):
    import models
    c = empty_db_conn
    _ins_sale(c, 'IV-LATE', 'A', 'C-A', '2026-07-01', 500.0)
    c.commit()
    _write_after_rows(monkeypatch, [
        ("INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled)"
         " VALUES (9001, 'RE-LATE', '2026-07-09', 'A', 'S1', 0)", ()),
        ("INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount)"
         " VALUES (9001, 'IV-LATE', 'IV', 500.0)", ())])

    page_rows = models.get_payment_status()[0]

    assert len(page_rows) == 1
    assert (page_rows[0]['status'], page_rows[0]['re_no']) == ('unpaid', None)
