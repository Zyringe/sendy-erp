"""Tests for the Express↔Sendy document-drift detector (plan §5, step 2).

WRITTEN RED, BEFORE THE IMPLEMENTATION. Every case here pins a clause of
`projects/express-integration/plan-express-drift-detector-2026-08-24.md`, and
every case that could pass without the detector doing any work carries its own
anti-vacuity mechanism — because five of this project's tests already turned out
to be incapable of failing, and none of those was caught by reading the test.

The two mechanisms used throughout, per plan §5:

  CONTROL         a document that must NOT be reported sits in the same fixture
                  as one that must, and both assertions run. A detector that
                  reports everything, or nothing, fails one of them.
  PAIRED MUTATION after asserting "this document is clean", change ONE field of
                  it and assert it becomes a finding. Without this, an
                  implementation that enumerates documents and returns no
                  findings — never hashing anything — passes every test here.
                  (Codex M5 raised exactly that; `compared_doc_nos` alone proves
                  enumeration, not comparison.)
"""
import datetime
import sqlite3

import pytest

import express_dbf_source as eds
from models import system_alerts


# ── fixture builders ─────────────────────────────────────────────────────────
D = datetime.date(2026, 1, 15)


def hdr(doc, *, rectyp='3', date=D, flgvat='1', party=None, discamt=0.0,
        docstat='N', sales=True):
    """An ARTRN/APTRN header row. ⚠ RECTYP / FLGVAT / DOCSTAT are STRINGS here
    on purpose: that is how they come out of the DBF, and comparing `'2'` to
    `2` is the bug that made an earlier scan report 1,088 phantom VAT errors.

    ⚠ `party` defaults per SIDE. It used to default to 'C001' for both, so an
    APTRN header carried a CUSTOMER code while put_purchase wrote 'S001' — every
    purchase fixture then disagreed on party_code and the CONTROL document in
    test 9 could never be clean. Found by the detector reporting it, which is
    the fixture being wrong and the code being right."""
    party = party if party is not None else ('C001' if sales else 'S001')
    return {'DOCNUM': doc, 'RECTYP': rectyp, 'DOCDAT': date, 'FLGVAT': flgvat,
            ('CUSCOD' if sales else 'SUPCOD'): party,
            'DISCAMT': discamt, 'DOCSTAT': docstat}


def line(doc, *, seq=1, code='A001', name='สินค้า A', qty=1.0, unit='ตว',
         price=10.0, total=None, net=None, disc=''):
    """A STCRD line. `unit` defaults to Express's 2-char code so the comparison
    has to run normalize_unit() to agree with Sendy's stored full-Thai word."""
    total = round(qty * price, 2) if total is None else total
    return {'DOCNUM': doc, 'SEQNUM': str(seq), 'STKCOD': code, 'STKDES': name,
            'TRNQTY': qty, 'TQUCOD': unit, 'UNITPR': price, 'TRNVAL': total,
            'NETVAL': total if net is None else net, 'DISC': disc}


def put_sales(conn, doc, *, seq=1, date='2026-01-15', party='C001', code='A001',
              name='สินค้า A', qty=1.0, unit='ตัว', price=10.0, total=None,
              net=None, disc='', vat_type=1):
    total = round(qty * price, 2) if total is None else total
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, customer_code,"
        " bsn_code, product_name_raw, qty, unit, unit_price, vat_type, discount,"
        " total, net) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date, f'{doc}-{seq}', doc, party, code, name, qty, unit, price,
         vat_type, disc, total, total if net is None else net))


def put_purchase(conn, doc, *, seq=1, date='2026-01-15', party='S001', code='A001',
                 name='สินค้า A', qty=1.0, unit='ตัว', price=10.0, total=None,
                 net=None, disc='', vat_type=1):
    total = round(qty * price, 2) if total is None else total
    conn.execute(
        "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, supplier_code,"
        " bsn_code, product_name_raw, qty, unit, unit_price, vat_type, discount,"
        " total, net, line_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date, doc, doc, party, code, name, qty, unit, price, vat_type, disc,
         total, total if net is None else net, seq))


def run(conn, *, artrn=(), aptrn=(), stcrd=(), armas=None, apmas=None, **kw):
    return eds.detect_document_drift(
        list(artrn), list(aptrn), list(stcrd),
        list(armas if armas is not None else [{'CUSCOD': 'C001', 'CUSNAM': 'ลูกค้า ก'}]),
        list(apmas if apmas is not None else [{'SUPCOD': 'S001', 'SUPNAM': 'ผู้ขาย ก'}]),
        conn, **kw)


def docs(findings):
    return {f['doc_no'] for f in findings}


# ── 1. a line Sendy holds that Express does not ──────────────────────────────
def test_1_sendy_holds_a_line_express_does_not(empty_db_conn):
    c = empty_db_conn
    put_sales(c, 'IV0001', seq=1)
    put_sales(c, 'IV0001', seq=2, code='B002', name='สินค้า B', qty=3.0, price=5.0)
    put_sales(c, 'IV0002')                                   # CONTROL: matches
    r = run(c, artrn=[hdr('IV0001'), hdr('IV0002')],
            stcrd=[line('IV0001'), line('IV0002')])

    assert docs(r.findings) == {'IV0001'}                    # the stale line is seen
    assert 'IV0002' in r.compared_doc_nos                    # …and the twin was
    assert 'IV0002' not in docs(r.findings)                  #    compared, not skipped


# ── 2. one SKU swapped for another, every total unchanged ────────────────────
def test_2_sku_swapped_with_identical_money(empty_db_conn):
    """The case that kills a count+sum detector: line count, total and net all
    agree, and the document is still wrong."""
    c = empty_db_conn
    put_sales(c, 'IV0001', code='B002')
    put_sales(c, 'IV0002')                                   # CONTROL
    r = run(c, artrn=[hdr('IV0001'), hdr('IV0002')],
            stcrd=[line('IV0001', code='A001'), line('IV0002')])

    assert docs(r.findings) == {'IV0001'}


# ── 3. two lines moved against each other, document total unchanged ──────────
def test_3_two_lines_cancel_out(empty_db_conn):
    c = empty_db_conn
    put_sales(c, 'IV0001', seq=1, code='A001', qty=5.0, price=10.0)
    put_sales(c, 'IV0001', seq=2, code='B002', qty=5.0, price=10.0)
    put_sales(c, 'IV0002')                                   # CONTROL
    r = run(c, artrn=[hdr('IV0001'), hdr('IV0002')],
            stcrd=[line('IV0001', seq=1, code='A001', qty=2.0, price=10.0),
                   line('IV0001', seq=2, code='B002', qty=8.0, price=10.0),
                   line('IV0002')])

    assert docs(r.findings) == {'IV0001'}


# ── 4. qty and unit price moved, net unchanged ───────────────────────────────
def test_4_qty_and_price_move_but_net_holds(empty_db_conn):
    c = empty_db_conn
    put_sales(c, 'IV0001', qty=2.0, price=50.0)              # 2 x 50 = 100
    put_sales(c, 'IV0002')                                   # CONTROL
    r = run(c, artrn=[hdr('IV0001'), hdr('IV0002')],
            stcrd=[line('IV0001', qty=1.0, price=100.0),     # 1 x 100 = 100
                   line('IV0002')])

    assert docs(r.findings) == {'IV0001'}


# ── 5. everything agrees — and the detector really did compare ───────────────
def test_5_clean_run_reports_nothing_and_still_compared(empty_db_conn):
    """⛔ The assertion `findings == []` is TRUE for an implementation that does
    nothing at all. So the population is counted from the FIXTURE (not from the
    detector's own output), and the test ends by mutating one field of one
    document and demanding that document turn into a finding."""
    c = empty_db_conn
    expected = {'IV0001', 'IV0002', 'IV0003'}                # counted here, by hand
    for d in sorted(expected):
        put_sales(c, d)
    headers = [hdr(d) for d in sorted(expected)]
    lines = [line(d) for d in sorted(expected)]

    r = run(c, artrn=headers, stcrd=lines)
    assert r.compared_doc_nos == expected
    assert r.findings == []

    # PAIRED MUTATION — one satang on one line of one document.
    lines[1] = line('IV0002', price=10.01)
    r2 = run(c, artrn=headers, stcrd=lines)
    assert docs(r2.findings) == {'IV0002'}, \
        'the detector enumerates documents but does not compare them'


# ── 6. cancelled at source: report the RAW status, never a translation ───────
def test_6_docstat_c_is_reported_raw(empty_db_conn):
    """Plan §7.1: AR `DOCSTAT='C'` semantics are an OPEN question — the code that
    owns them says so itself. Reporting it as 'ยกเลิก' would be this detector
    deciding a question nobody has answered."""
    c = empty_db_conn
    put_sales(c, 'IV0001')
    put_sales(c, 'IV0002')                                   # CONTROL
    r = run(c, artrn=[hdr('IV0001', docstat='C'), hdr('IV0002')],
            stcrd=[line('IV0001'), line('IV0002')])

    assert docs(r.findings) == {'IV0001'}
    f = next(x for x in r.findings if x['doc_no'] == 'IV0001')
    assert f['kind'] == 'source_status'
    assert f['docstat'] == 'C'                               # the RAW value
    assert f['message'], 'an empty message would pass the next assertion for free'
    assert 'ยกเลิก' not in f['message']


# ── 7. an empty Express side must raise, never return "all clear" ────────────
def test_7_empty_express_side_raises(empty_db_conn):
    """⚠ One `pytest.raises` covering four guards proves ONE of them works and
    says nothing about the other three. Measured: with only the both-headers-
    empty case here, deleting the STCRD guard turned NO test red. Each guard
    gets inputs only IT can reject."""
    c = empty_db_conn
    put_sales(c, 'IV0001')
    with pytest.raises(eds.DriftInputError):                  # no headers at all
        run(c, artrn=[], aptrn=[], stcrd=[line('IV0001')])
    with pytest.raises(eds.DriftInputError):                  # headers, no lines
        run(c, artrn=[hdr('IV0001')], stcrd=[])
    with pytest.raises(eds.DriftInputError):                  # ARTRN without ARMAS
        run(c, artrn=[hdr('IV0001')], stcrd=[line('IV0001')], armas=[])
    with pytest.raises(eds.DriftInputError):                  # APTRN without APMAS
        run(c, aptrn=[hdr('IV0001', sales=False)], stcrd=[line('IV0001')], apmas=[])

    # CONTROL: the same shape WITHOUT the defect does not raise, so the four
    # above cannot be passing because every call raises.
    assert run(c, artrn=[hdr('IV0001')], stcrd=[line('IV0001')]).compared_doc_nos \
        == {'IV0001'}


# ── 8. DBF numeric fields arrive as strings ──────────────────────────────────
def test_8_string_and_int_header_fields_are_the_same_value(empty_db_conn):
    """Paired fixture, not a code mutation: `'2'` and `2` must reach the same
    verdict, AND a genuinely different value must reach a different one — so
    "they agree" cannot be explained by the field never being compared."""
    c = empty_db_conn
    put_sales(c, 'IV0001', vat_type=2)

    as_str = run(c, artrn=[hdr('IV0001', flgvat='2')], stcrd=[line('IV0001')])
    as_int = run(c, artrn=[hdr('IV0001', flgvat=2)], stcrd=[line('IV0001')])
    assert as_str.compared_doc_nos == as_int.compared_doc_nos == {'IV0001'}
    assert as_str.findings == as_int.findings == []

    # CONTROL: the field IS compared — a real difference is seen.
    other = run(c, artrn=[hdr('IV0001', flgvat='1')], stcrd=[line('IV0001')])
    assert docs(other.findings) == {'IV0001'}


# ── 9. a purchase edited after it fell out of the import window ──────────────
def test_9_purchase_edited_after_falling_out_of_window(empty_db_conn):
    """The 60-day window is why drift becomes permanent: a document Express
    edits after it ages out is never re-read. Measured 2026-08-25 — IV6900969
    aged out six days before anyone looked."""
    c = empty_db_conn
    put_purchase(c, 'RR0001', qty=10.0, price=20.0)          # Sendy: 10 x 20
    put_purchase(c, 'RR0002')                                # CONTROL
    r = run(c, aptrn=[hdr('RR0001', sales=False), hdr('RR0002', sales=False)],
            stcrd=[line('RR0001', qty=10.0, price=25.0),     # Express: 10 x 25
                   line('RR0002')])

    assert docs(r.findings) == {'RR0001'}
    assert 'RR0002' in r.compared_doc_nos


# ── 10. Express holds a document Sendy does not — F9's job, not ours ─────────
def test_10_express_only_document_is_not_reported(empty_db_conn):
    c = empty_db_conn
    put_sales(c, 'IV0002')
    r = run(c, artrn=[hdr('IV0001'), hdr('IV0002')],
            stcrd=[line('IV0001'), line('IV0002')])

    # exact set, not "IV0002 was compared" — that would also pass if the
    # detector had silently compared IV0001 against nothing
    assert r.compared_doc_nos == {'IV0002'}
    assert r.findings == []
    # …and IV0001 was never fingerprinted at all. Without this the scoping to
    # the documents Sendy holds could be deleted and no test would notice —
    # measured. It is worth 5.3x on the real dataset (Sendy holds ~9.9k of
    # Express's ~67k), so it is a property, not an accident.
    assert r.counters['express_eligible'] == 1
    assert r.counters['express_headers'] == 2      # CONTROL: Express really had 2


# ── 11. the same document twice must not stack alerts ────────────────────────
def test_11_repeat_finding_holds_one_alert(empty_db, empty_db_conn, monkeypatch):
    import config, database
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    finding = {'doc_no': 'IV0001', 'kind': 'content', 'fields': ['net'],
               'docstat': 'N', 'message': 'ทดสอบ'}

    system_alerts.record_express_doc_drift_alerts([finding], dataset_label='BSN5657')
    system_alerts.record_express_doc_drift_alerts([finding], dataset_label='BSN5657')

    rows = sqlite3.connect(str(empty_db)).execute(
        "SELECT dedupe_key FROM system_alerts WHERE kind = ?",
        (system_alerts.KIND_EXPRESS_DOC_DRIFT,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'IV0001'


# ── 12. a VAT-exception document is COMPARED, not filtered away ──────────────
def test_12_vat_exception_document_is_compared_not_skipped(empty_db_conn):
    """Plan §2 decided not to touch billing logic for these. That decision must
    not quietly become "and also stop looking at them"."""
    c = empty_db_conn
    put_sales(c, 'IV0001', vat_type=2, price=100.0)
    r = run(c, artrn=[hdr('IV0001', flgvat='2')],
            stcrd=[line('IV0001', price=100.0)])
    assert 'IV0001' in r.compared_doc_nos                    # it went through
    assert r.findings == []                                  # …and it agreed

    # PAIRED MUTATION — one satang proves the agreement was measured.
    r2 = run(c, artrn=[hdr('IV0001', flgvat='2')],
             stcrd=[line('IV0001', price=100.0, net=99.99)])
    assert docs(r2.findings) == {'IV0001'}


# ── 13-14. the baseline: silent when known, loud when it moves ───────────────
# Not in the plan's list of 12, which predates §4.4 having a measured baseline.
# 108 documents ride on this behaviour, so it gets tests.
def test_13_baselined_document_is_silent_until_its_fingerprint_moves(empty_db_conn):
    c = empty_db_conn
    put_sales(c, 'IV0001', total=4.0, disc='1404.00', net=1400.0)
    first = run(c, artrn=[hdr('IV0001', discamt=4.0)],
                stcrd=[line('IV0001', total=1404.0, net=1400.0)])
    assert docs(first.findings) == {'IV0001'}                # unknown → reported

    known = {'IV0001': {'fingerprint': first.findings[0]['fingerprint'],
                        'reason': 'legacy text-parse column swap; net identical'}}
    quiet = run(c, artrn=[hdr('IV0001', discamt=4.0)],
                stcrd=[line('IV0001', total=1404.0, net=1400.0)], baseline=known)
    assert quiet.findings == []
    assert 'IV0001' in quiet.compared_doc_nos                # silent ≠ skipped

    # …and the SAME baseline must not cover a document that changed again.
    moved = run(c, artrn=[hdr('IV0001', discamt=4.0)],
                stcrd=[line('IV0001', total=1404.0, net=1399.0)], baseline=known)
    assert docs(moved.findings) == {'IV0001'}


def test_14_baseline_entry_without_a_reason_is_refused(empty_db_conn):
    """§4.4: a baseline entry with no reason is closing your eyes, written as
    code. The refusal belongs in the code, not in a review convention."""
    c = empty_db_conn
    put_sales(c, 'IV0001')
    with pytest.raises(eds.DriftInputError):
        run(c, artrn=[hdr('IV0001')], stcrd=[line('IV0001')],
            baseline={'IV0001': {'fingerprint': 'abc', 'reason': '   '}})
    # …and an entry with a reason but NO fingerprint is refused too: it would
    # silence IV0001 for ever, however far it drifted afterwards. Measured:
    # without this case the fingerprint check could be deleted silently.
    with pytest.raises(eds.DriftInputError):
        run(c, artrn=[hdr('IV0001')], stcrd=[line('IV0001')],
            baseline={'IV0001': {'reason': 'legacy column swap, net identical'}})
    # CONTROL: a complete entry is accepted, so the two above are not passing
    # because every baseline raises.
    assert run(c, artrn=[hdr('IV0001')], stcrd=[line('IV0001')],
               baseline={'IV0001': {'fingerprint': 'abc',
                                    'reason': 'legacy column swap, net identical'}}
               ).compared_doc_nos == {'IV0001'}


# ── 15. freshness is indeterminate → no "deleted at source" claim ────────────
def test_15_unknown_export_time_forbids_a_deleted_at_source_finding(empty_db_conn):
    """§4.5: `export_at` falls back to "today" when the zip carries no readable
    DBF timestamp. Deciding a document was deleted at source on a fallback value
    is how a detector invents an incident."""
    c = empty_db_conn
    put_sales(c, 'IV0001', date='2024-03-04')                # Express has no such doc
    put_sales(c, 'IV0002')                                   # CONTROL
    put_sales(c, 'IV0000', date='2021-05-06')                # CONTROL: before the era
    r = run(c, artrn=[hdr('IV0002')], stcrd=[line('IV0002')], export_at=None)

    assert [f for f in r.findings if f['kind'] == 'deleted_at_source'] == []
    assert r.counters['freshness'] == 'indeterminate'
    assert 'IV0002' in r.compared_doc_nos                    # CONTROL still ran

    # With an authoritative export time, the same document IS a finding.
    r2 = run(c, artrn=[hdr('IV0002')], stcrd=[line('IV0002')],
             export_at=datetime.date(2026, 6, 30))
    assert docs([f for f in r2.findings if f['kind'] == 'deleted_at_source']) == {'IV0001'}
    # …and a pre-2024 document is not swept in with it. Sendy holds years of
    # history this dataset's scope never covered; calling those "deleted at
    # source" would bury the one real finding under them.
    assert 'IV0000' not in r2.compared_doc_nos
    assert r2.counters['sendy_eligible'] == 2

    # A document NEWER than the export is not a finding either — the dataset
    # simply does not reach it yet.
    r3 = run(c, artrn=[hdr('IV0002')], stcrd=[line('IV0002')],
             export_at=datetime.date(2024, 3, 3))
    assert [f for f in r3.findings if f['kind'] == 'deleted_at_source'] == []
    assert r3.counters['sendy_only_newer_than_export'] == 1

    # …and neither is one dated the SAME DAY as the export. An export taken at
    # some hour of 2024-03-04 does not prove a document dated 2024-03-04 is
    # missing at source; it may have been keyed after the export ran.
    r4 = run(c, artrn=[hdr('IV0002')], stcrd=[line('IV0002')],
             export_at=datetime.datetime(2024, 3, 4, 8, 32))
    assert [f for f in r4.findings if f['kind'] == 'deleted_at_source'] == []
    assert r4.counters['sendy_only_newer_than_export'] == 1



# ── 16. the population is checked against the ledgers, not against itself ────
def test_16_a_document_number_in_both_books_refuses_a_verdict(empty_db_conn):
    """`compared + sendy_only == sendy_eligible` is set algebra: true whatever
    the data is. The real check is a second query over the ledgers, and this is
    the case it exists for — one doc_no in BOTH books would merge two documents'
    lines into one fingerprint and report drift for ever, silently."""
    c = empty_db_conn
    put_sales(c, 'IV0001')
    # CONTROL: the same fixture without the collision produces a verdict.
    assert run(c, artrn=[hdr('IV0001')], stcrd=[line('IV0001')]).compared_doc_nos \
        == {'IV0001'}

    put_purchase(c, 'IV0001')                                # same number, other book
    with pytest.raises(eds.DriftInputError) as e:
        run(c, artrn=[hdr('IV0001')], stcrd=[line('IV0001')])
    assert 'both books' in str(e.value) or 'dropped' in str(e.value)
