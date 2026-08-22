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

from express_dbf_source import build_out_of_window_docs, build_receipt_values


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


def test_conflicting_duplicate_headers_are_refused(sort_order_probe=None):
    """Two headers for one DOCNUM that DISAGREE. Keeping whichever arrived
    first makes the report depend on DBF row order, which contradicts the
    determinism this file promises — and silently drops the other row's value.
    Refuse, the same stance express_dbf_source._reject_duplicate takes on the
    snapshot side (Codex review, 2026-08-22)."""
    with pytest.raises(ValueError):
        build_out_of_window_docs(
            [_artrn('IV_X', '3', docdat=datetime.date(2026, 1, 1), netamt=100.0),
             _artrn('IV_X', '3', docdat=datetime.date(2026, 1, 1), netamt=250.0)],
            [], cutoff=CUTOFF)


@pytest.mark.parametrize('field,value', [
    ('docdat', datetime.date(2026, 2, 2)),
    ('netamt', 999.0),
    ('rcvamt', 999.0),
    ('remamt', 999.0),
    ('docstat', 'C'),
])
def test_a_disagreement_in_any_reported_field_is_a_conflict(field, value):
    """Every field the report publishes has to participate, or a duplicate can
    still change the output silently through whichever one was left out."""
    base = dict(docdat=datetime.date(2026, 1, 1), netamt=100.0, rcvamt=1.0,
                remamt=2.0, docstat='N')
    other = {**base, field: value}
    with pytest.raises(ValueError):
        build_out_of_window_docs([_artrn('IV_X', '3', **base),
                                  _artrn('IV_X', '3', **other)], [], cutoff=CUTOFF)


def test_a_conflict_on_the_OTHER_side_is_refused_too():
    with pytest.raises(ValueError):
        build_out_of_window_docs(
            [], [_aptrn('RR_X', '3', docdat=datetime.date(2026, 1, 1), netamt=1.0),
                 _aptrn('RR_X', '3', docdat=datetime.date(2026, 1, 1), netamt=2.0)],
            cutoff=CUTOFF)


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


# ── valuing RE/PS from their LINES, per book side ───────────────────────────
#
# A receipt header cannot be trusted for its own amount (MAPPING trap #4 holds
# for RCVAMT but NOT for NETAMT, which is non-zero on 28,501 of 28,818 RE
# headers and ties to the line sum only 95.01% of the time). So the report
# values RE/PS from ARRCPIT/APRCPIT via the production builders — and the
# lookup has to keep the two book sides apart for exactly the reason
# build_out_of_window_docs does (Codex review round 2, 2026-08-22).

def _arrcpit(rcpnum, docnum, rcvamt, rectyp='3'):
    return {'RCPNUM': rcpnum, 'DOCNUM': docnum, 'RECTYP': rectyp, 'RCVAMT': rcvamt}


def _aprcpit(rcpnum, docnum, payamt, rectyp='3'):
    return {'RCPNUM': rcpnum, 'DOCNUM': docnum, 'RECTYP': rectyp, 'PAYAMT': payamt}


def test_receipt_values_are_keyed_by_book_side_not_doc_number_alone():
    """An RE and a PS that share a DOCNUM must each keep their OWN amount. A
    flat doc_no key lets whichever side is read second overwrite the other, and
    both rows in the report then publish that one amount."""
    values = build_receipt_values(
        artrn_rows=[_artrn('X9', '9')], arrcpit_rows=[_arrcpit('X9', 'IV1', 500.0)],
        armas_rows=[],
        aptrn_rows=[_aptrn('X9', '9', rcvamt=800.0)],
        aprcpit_rows=[_aprcpit('X9', 'RR1', 800.0)], apmas_rows=[])

    assert values[('ARTRN', 'X9')] == 500.0
    assert values[('APTRN', 'X9')] == 800.0


def test_an_RE_is_valued_from_its_LINES_not_its_header():
    """NETAMT on an RE header looks like a receipt total and is not one."""
    values = build_receipt_values(
        artrn_rows=[_artrn('RE1', '9', netamt=99999.0)],
        arrcpit_rows=[_arrcpit('RE1', 'IV1', 300.0), _arrcpit('RE1', 'IV2', 250.5)],
        armas_rows=[], aptrn_rows=[], aprcpit_rows=[], apmas_rows=[])

    assert values[('ARTRN', 'RE1')] == 550.5


def test_a_PS_is_valued_from_its_header_RCVAMT_not_PAYAMT():
    """The two sides do NOT work the same way, and assuming they do is
    MAPPING trap #5: `build_payments_out_records` reads the PS header's RCVAMT
    because PAYAMT diverges arbitrarily, sometimes by exactly 2x."""
    values = build_receipt_values(
        artrn_rows=[], arrcpit_rows=[], armas_rows=[],
        aptrn_rows=[_aptrn('PS1', '9', rcvamt=1200.0)],
        aprcpit_rows=[_aprcpit('PS1', 'RR1', 2400.0)], apmas_rows=[])

    assert values[('APTRN', 'PS1')] == 1200.0
