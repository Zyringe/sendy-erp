"""F9 detection — Express DBF documents the 60-day import window never sees.

`commit_express_dbf(since_days=60)` scopes the TRANSACTIONAL builders to
headers with DOCDAT >= cutoff (express_dbf_source._in_window). A document
dated before that cutoff is dropped from sales, purchases, invoice_refs,
payments in/out and both credit-note builders — and, because every later run
uses a cutoff that has moved FORWARD, it is dropped again every day after.
It never enters Sendy at all.

That is correct for ordinary history (the book goes back to 2009 and the
snapshots are windowless by design, so no BALANCE is lost). It is NOT correct
for a document Express gains *now* carrying an old DOCDAT — a backdated
invoice, a late-keyed receipt. Those are silently invisible forever.

This is detection only: report which documents are out of window and whether
Sendy holds them, so the evidence exists before anyone argues about widening
`since_days`. Nothing here changes the window.

Fixture conventions match tests/test_express_dbf_source.py — see
projects/express-integration/MAPPING.md for the field traps, do not
rediscover them:
  - windowed scope is ARTRN/APTRN RECTYP IN ('3','1','5') for the ledger plus
    '9' (RE/PS) for payments; '7' (OE) is never windowed and never reported.
  - _in_window is `DOCDAT >= cutoff`, so a doc dated exactly ON the cutoff is
    IN and must not be reported.
  - a missing/malformed DOCDAT is treated as OUT of a windowed run, so it is
    dropped too and must be reported.
  - ARTRN/APTRN can carry more than one header per DOCNUM (see
    express_dbf_source._reject_duplicate) — one document, not two.
  - header money fields are carried RAW. RE/PS headers legitimately hold 0
    (MAPPING trap #4); this module must not invent an amount for them.
"""
import datetime

import pytest

from express_dbf_source import build_out_of_window_docs


CUTOFF = datetime.date(2026, 6, 1)


# Explicit sentinel, not None: `docdat=None` is a REAL fixture value here (a
# header with no usable DOCDAT is one of the losses under test), so it must not
# collapse into the default the way an `or`/`is not None` default would.
_UNSET = object()


def _artrn(docnum, rectyp, *, docdat=_UNSET, netamt=0.0, rcvamt=0.0, remamt=0.0,
           docstat='N'):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp,
        'DOCDAT': datetime.date(2026, 6, 15) if docdat is _UNSET else docdat,
        'NETAMT': netamt, 'RCVAMT': rcvamt, 'REMAMT': remamt, 'DOCSTAT': docstat,
    }


def _aptrn(docnum, rectyp, *, docdat=_UNSET, netamt=0.0, rcvamt=0.0, remamt=0.0,
           docstat='N'):
    return _artrn(docnum, rectyp, docdat=docdat, netamt=netamt,
                  rcvamt=rcvamt, remamt=remamt, docstat=docstat)


def _by_doc(rows):
    return {r['doc_no']: r for r in rows}


# ── what the window drops ────────────────────────────────────────────────────

def test_reports_doc_before_cutoff_and_not_one_on_or_after():
    """CONTROL + subject in one run: the in-window doc must be absent, which
    is what proves the reported one was selected by the cutoff and not just by
    existing."""
    rows = build_out_of_window_docs(
        [_artrn('IV_OLD', '3', docdat=datetime.date(2026, 5, 31)),
         _artrn('IV_NEW', '3', docdat=datetime.date(2026, 6, 15))],
        [], cutoff=CUTOFF)

    assert sorted(r['doc_no'] for r in rows) == ['IV_OLD']


def test_cutoff_boundary_is_inclusive_so_a_doc_dated_on_it_is_not_reported():
    """_in_window keeps DOCDAT >= cutoff. Reporting the boundary doc would be
    a false positive on every single daily run."""
    rows = build_out_of_window_docs(
        [_artrn('IV_EDGE', '3', docdat=CUTOFF)], [], cutoff=CUTOFF)

    assert rows == []


def test_missing_docdat_is_reported_because_a_windowed_run_drops_it():
    rows = build_out_of_window_docs(
        [_artrn('IV_NODATE', '3', docdat=None)], [], cutoff=CUTOFF)

    assert [(r['doc_no'], r['doc_date_iso']) for r in rows] == [('IV_NODATE', None)]


# ── scope: only the RECTYPs the window actually filters ─────────────────────

@pytest.mark.parametrize('rectyp', ['3', '1', '5', '9'])
def test_every_windowed_rectyp_is_reported(rectyp):
    """'3'/'1'/'5' = the ledger builders' scope, '9' = payments in/out. All
    four are cutoff-filtered in commit_express_dbf, so all four can be lost."""
    rows = build_out_of_window_docs(
        [_artrn(f'D_{rectyp}', rectyp, docdat=datetime.date(2026, 1, 1))],
        [], cutoff=CUTOFF)

    assert [r['rectyp'] for r in rows] == [rectyp]


def test_out_of_scope_rectyp_is_not_reported():
    """OE ('7') is not built by any windowed builder, so an old one is not a
    loss — reporting it would inflate the finding with rows nothing wanted."""
    rows = build_out_of_window_docs(
        [_artrn('OE_OLD', '7', docdat=datetime.date(2026, 1, 1)),
         _artrn('IV_OLD', '3', docdat=datetime.date(2026, 1, 1))],
        [], cutoff=CUTOFF)

    assert [r['doc_no'] for r in rows] == ['IV_OLD']


def test_both_books_sides_are_reported_and_tagged():
    rows = build_out_of_window_docs(
        [_artrn('IV_OLD', '3', docdat=datetime.date(2026, 1, 1))],
        [_aptrn('RR_OLD', '3', docdat=datetime.date(2026, 1, 1))],
        cutoff=CUTOFF)

    assert _by_doc(rows)['IV_OLD']['source'] == 'ARTRN'
    assert _by_doc(rows)['RR_OLD']['source'] == 'APTRN'


# ── does Sendy already hold it? ─────────────────────────────────────────────

def test_known_docs_are_marked_in_sendy_and_unknown_ones_are_not():
    """The actionable finding is out-of-window AND absent from Sendy. A doc an
    earlier full-history import already landed is out of window but not lost."""
    rows = build_out_of_window_docs(
        [_artrn('IV_HELD', '3', docdat=datetime.date(2026, 1, 1)),
         _artrn('IV_LOST', '3', docdat=datetime.date(2026, 1, 1))],
        [], cutoff=CUTOFF, known_ar_docs={'IV_HELD'})

    assert _by_doc(rows)['IV_HELD']['in_sendy'] is True
    assert _by_doc(rows)['IV_LOST']['in_sendy'] is False


def test_the_two_known_sets_do_not_leak_across_book_sides():
    """An AP doc number that collides with an AR one must not mark the AR
    document held — that would silently delete a finding. Fails in the
    dangerous direction, so it is pinned rather than left to prefixes."""
    rows = build_out_of_window_docs(
        [_artrn('X1', '3', docdat=datetime.date(2026, 1, 1))],
        [_aptrn('X1', '3', docdat=datetime.date(2026, 1, 1))],
        cutoff=CUTOFF, known_ap_docs={'X1'})

    held = {r['source']: r['in_sendy'] for r in rows}
    assert held == {'ARTRN': False, 'APTRN': True}


def test_known_docs_defaults_to_nothing_known():
    rows = build_out_of_window_docs(
        [_artrn('IV_OLD', '3', docdat=datetime.date(2026, 1, 1))],
        [], cutoff=CUTOFF)

    assert rows[0]['in_sendy'] is False


# ── duplicate headers are one document ──────────────────────────────────────

def test_duplicate_headers_collapse_to_one_record_carrying_the_count():
    """ARTRN legitimately holds more than one header per DOCNUM. Emitting two
    rows would double-count the finding the same way it would double-count a
    balance (see express_dbf_source._reject_duplicate)."""
    rows = build_out_of_window_docs(
        [_artrn('IV_DUP', '3', docdat=datetime.date(2026, 1, 1), netamt=100.0),
         _artrn('IV_DUP', '3', docdat=datetime.date(2026, 1, 1), netamt=100.0)],
        [], cutoff=CUTOFF)

    assert len(rows) == 1
    assert rows[0]['header_count'] == 2


def test_a_single_header_reports_count_one():
    rows = build_out_of_window_docs(
        [_artrn('IV_ONE', '3', docdat=datetime.date(2026, 1, 1))],
        [], cutoff=CUTOFF)

    assert rows[0]['header_count'] == 1


def test_same_docnum_on_both_sides_stays_two_documents():
    """DOCNUM is unique per book side, not globally — collapsing across
    ARTRN/APTRN would hide one of them."""
    rows = build_out_of_window_docs(
        [_artrn('X1', '3', docdat=datetime.date(2026, 1, 1))],
        [_aptrn('X1', '3', docdat=datetime.date(2026, 1, 1))],
        cutoff=CUTOFF)

    assert sorted(r['source'] for r in rows) == ['APTRN', 'ARTRN']


# ── money fields stay raw ───────────────────────────────────────────────────

def test_header_money_fields_are_carried_verbatim():
    """No derived 'amount'. NETAMT is the bill on IV/RR/SR/HS but is 0 on
    RE/PS by design, and inventing a number for those is exactly the
    field-name reinterpretation MAPPING.md forbids."""
    rows = build_out_of_window_docs(
        [_artrn('IV_M', '3', docdat=datetime.date(2026, 1, 1),
                netamt=1234.56, rcvamt=1000.0, remamt=234.56)],
        [], cutoff=CUTOFF)

    assert rows[0]['netamt'] == 1234.56
    assert rows[0]['rcvamt'] == 1000.0
    assert rows[0]['remamt'] == 234.56
    assert 'amount' not in rows[0]


def test_docstat_is_carried_raw_and_not_translated():
    """'C' agrees with is_void on the AP side but NOT on the AR side (49 DBF
    'C' vs 2 Sendy cancelled, MAPPING.md §3). Carrying the letter keeps the
    report honest; emitting a cancelled=True would be a guess."""
    rows = build_out_of_window_docs(
        [_artrn('IV_C', '3', docdat=datetime.date(2026, 1, 1), docstat='C')],
        [], cutoff=CUTOFF)

    assert rows[0]['docstat'] == 'C'
    assert 'cancelled' not in rows[0]
    assert 'is_void' not in rows[0]


def test_receipt_header_zero_money_is_reported_as_zero_not_guessed():
    rows = build_out_of_window_docs(
        [_artrn('RE_OLD', '9', docdat=datetime.date(2026, 1, 1))],
        [], cutoff=CUTOFF)

    assert (rows[0]['netamt'], rows[0]['rcvamt'], rows[0]['remamt']) == (0.0, 0.0, 0.0)


# ── a windowless run drops nothing, so asking is a caller bug ───────────────

def test_cutoff_none_raises_rather_than_reporting_an_empty_all_clear():
    """cutoff=None is the manual full-history override: it drops nothing, so
    an empty list would read as 'nothing is being lost' when the question was
    never asked. Same stance as the snapshot builders, which refuse a cutoff."""
    with pytest.raises(ValueError):
        build_out_of_window_docs([_artrn('IV', '3')], [], cutoff=None)


# ── determinism ─────────────────────────────────────────────────────────────

def test_output_order_is_stable_across_runs():
    """The report is committed as evidence and re-run to compare; row order
    drifting would make every diff noise."""
    artrn = [_artrn('IV_B', '3', docdat=datetime.date(2026, 2, 1)),
             _artrn('IV_A', '3', docdat=datetime.date(2026, 1, 1))]
    aptrn = [_aptrn('RR_A', '3', docdat=datetime.date(2026, 1, 15))]

    first = build_out_of_window_docs(artrn, aptrn, cutoff=CUTOFF)
    second = build_out_of_window_docs(list(reversed(artrn)), aptrn, cutoff=CUTOFF)

    assert first == second
