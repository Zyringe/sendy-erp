"""express_dbf_source — Phase 1 slice A (sales + purchase DBF adapters).

Pure dict-fixture tests: no DBF files needed. Fixtures use the field names
and traps documented in projects/express-integration/MAPPING.md (Phase 0,
verified 2026-07-08) — do not rediscover them:
  - sales/purchase scope = RECTYP IN ('3','1','5') (IV/RR, HS/HP cash, SR/GR
    credit-note lines); '9' (RE/PS) and '7' (OE) are OUT of scope.
  - SR (sales) / GR (purchase) lines use STCRD.TRNVAL for net, NOT NETVAL.
  - vat_type is doc-level (ARTRN/APTRN.FLGVAT), broadcast to every line.
  - sales doc_no carries the line suffix ("IV6900929-1"); purchase doc_no
    does not (line identity is (doc_no, bsn_code, line_seq)).
"""
import datetime
import os
import sqlite3

from express_dbf_source import build_invoice_refs, build_purchase_entries, build_sales_entries

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_124 = os.path.join(_REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")
_MIG_132 = os.path.join(_REPO, "data", "migrations", "132_express_invoice_refs.sql")


def _artrn(docnum, rectyp, *, cuscod='C001', flgvat=0, docdat=None, youref=None):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'CUSCOD': cuscod, 'FLGVAT': flgvat,
        'DOCDAT': docdat or datetime.date(2026, 4, 1), 'YOUREF': youref,
    }


def _aptrn(docnum, rectyp, *, supcod='S001', flgvat=0, docdat=None):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'SUPCOD': supcod, 'FLGVAT': flgvat,
        'DOCDAT': docdat or datetime.date(2026, 4, 1),
    }


def _stcrd(docnum, seqnum, *, stkcod='804', stkdes='name', qty=2.0, unit='ตัว',
           unitpr=45.0, disc='', trnval=90.0, netval=90.0):
    return {
        'DOCNUM': docnum, 'SEQNUM': seqnum, 'STKCOD': stkcod, 'STKDES': stkdes,
        'TRNQTY': qty, 'TQUCOD': unit, 'UNITPR': unitpr, 'DISC': disc,
        'TRNVAL': trnval, 'NETVAL': netval,
    }


# ── sales ─────────────────────────────────────────────────────────────────

def test_build_sales_entries_iv_line():
    """IV (RECTYP='3') line: net comes from NETVAL, doc_no carries the -N suffix."""
    artrn = [_artrn('IV6900929', '3', cuscod='C001')]
    stcrd = [_stcrd('IV6900929', 1, stkcod='804ข1108', qty=2.0, unitpr=45.0,
                     trnval=95.0, netval=62.0)]
    armas = [{'CUSCOD': 'C001', 'CUSNAM': 'ร้านทดสอบ'}]

    entries = build_sales_entries(artrn, stcrd, armas)

    assert len(entries) == 1
    e = entries[0]
    assert e['doc_no'] == 'IV6900929-1'
    assert e['product_code_raw'] == '804ข1108'
    assert e['qty'] == 2.0
    assert e['unit_price'] == 45.0
    assert e['total'] == 95.0
    assert e['net'] == 62.0, "IV net must be NETVAL, not TRNVAL"
    assert e['party'] == 'ร้านทดสอบ'
    assert e['party_code'] == 'C001'


def test_build_sales_entries_hs_cash_line_is_in_scope():
    """HS (RECTYP='1', cash sale) is IN scope, same net rule as IV."""
    artrn = [_artrn('HS6900001', '1')]
    stcrd = [_stcrd('HS6900001', 1, stkcod='045ล6155', qty=1.0, unit='ผง',
                     unitpr=300.0, trnval=300.0, netval=300.0)]
    armas = []

    entries = build_sales_entries(artrn, stcrd, armas)

    assert len(entries) == 1
    assert entries[0]['doc_no'] == 'HS6900001-1'
    assert entries[0]['net'] == 300.0


def test_build_sales_entries_sr_uses_trnval_not_netval():
    """TRAP: SR (RECTYP='5') net must be TRNVAL, not the VAT-stripped NETVAL."""
    artrn = [_artrn('SR6900009', '5')]
    stcrd = [_stcrd('SR6900009', 1, stkcod='935ก2000', qty=60.0, unit='อน',
                     unitpr=155.0, trnval=2340.00, netval=2293.20)]
    armas = []

    entries = build_sales_entries(artrn, stcrd, armas)

    assert len(entries) == 1
    e = entries[0]
    assert e['total'] == 2340.00
    assert e['net'] == 2340.00, "SR net must be TRNVAL (pre-discount), not NETVAL"
    assert e['net'] != 2293.20


def test_build_sales_entries_vat_type_broadcast_from_header():
    """vat_type is doc-level (FLGVAT) — every line of the doc gets the same value."""
    artrn = [_artrn('IV6900001', '3', flgvat=2)]
    stcrd = [_stcrd('IV6900001', 1), _stcrd('IV6900001', 2, stkcod='805')]

    entries = build_sales_entries(artrn, stcrd, [])

    assert len(entries) == 2
    assert all(e['vat_type'] == 2 for e in entries)


def test_build_sales_entries_excludes_out_of_scope_rectyp():
    """RECTYP='9' (RE, payment header) must not be pulled into sales entries."""
    artrn = [_artrn('RE6900001', '9')]
    stcrd = [_stcrd('RE6900001', 1)]

    entries = build_sales_entries(artrn, stcrd, [])

    assert entries == []


def test_build_sales_entries_falls_back_to_customer_code_when_name_missing():
    artrn = [_artrn('IV6900002', '3', cuscod='UNKNOWN')]
    stcrd = [_stcrd('IV6900002', 1)]

    entries = build_sales_entries(artrn, stcrd, [])

    assert entries[0]['party'] == 'UNKNOWN'


# ── purchase ─────────────────────────────────────────────────────────────

def test_build_purchase_entries_rr_line_doc_no_has_no_suffix():
    """RR (RECTYP='3') line: doc_no is the bare DOCNUM (no -N), net from NETVAL."""
    aptrn = [_aptrn('RR6900077', '3', supcod='S001')]
    stcrd = [_stcrd('RR6900077', 1, stkcod='035ป6107', qty=400.0, unitpr=4.80,
                     trnval=1920.00, netval=1920.00)]
    apmas = [{'SUPCOD': 'S001', 'SUPNAM': 'ซัพพลายเออร์ทดสอบ'}]

    entries = build_purchase_entries(aptrn, stcrd, apmas)

    assert len(entries) == 1
    e = entries[0]
    assert e['doc_no'] == 'RR6900077'
    assert e['line_seq'] == 1
    assert e['net'] == 1920.00
    assert e['party'] == 'ซัพพลายเออร์ทดสอบ'
    assert e['party_code'] == 'S001'


def test_build_purchase_entries_hp_cash_line_is_in_scope():
    aptrn = [_aptrn('HP6900001', '1')]
    stcrd = [_stcrd('HP6900001', 1, stkcod='035ป6107', qty=400.0, unitpr=4.80,
                     trnval=1920.00, netval=1920.00),
             _stcrd('HP6900001', 2, stkcod='035ป6108', qty=500.0, unitpr=5.80,
                     trnval=2900.00, netval=2900.00)]

    entries = build_purchase_entries(aptrn, stcrd, [])

    assert len(entries) == 2
    assert {e['line_seq'] for e in entries} == {1, 2}
    assert {e['doc_no'] for e in entries} == {'HP6900001'}


def test_build_purchase_entries_gr_uses_trnval_not_netval():
    """TRAP: GR (RECTYP='5') net must be TRNVAL, not NETVAL (VAT-type-2 case)."""
    aptrn = [_aptrn('GR6700021', '5', flgvat=2)]
    stcrd = [_stcrd('GR6700021', 1, trnval=390.00, netval=364.49)]

    entries = build_purchase_entries(aptrn, stcrd, [])

    assert len(entries) == 1
    assert entries[0]['net'] == 390.00, "GR net must be TRNVAL, not the VAT-stripped NETVAL"


def test_build_purchase_entries_excludes_oe_and_ps():
    """RECTYP='7' (OE, order) and RECTYP='9' (PS, payment) are out of scope."""
    aptrn = [_aptrn('OE6900001', '7'), _aptrn('PS6900001', '9')]
    stcrd = [_stcrd('OE6900001', 1), _stcrd('PS6900001', 1)]

    entries = build_purchase_entries(aptrn, stcrd, [])

    assert entries == []


# ── invoice refs (YOUREF / REMARK side table) ───────────────────────────

def test_build_invoice_refs_captures_youref_and_remark():
    artrn = [_artrn('IV6900929', '3', youref='วรันดา(L)')]
    artrnrm = [{'DOCNUM': 'IV6900929', 'REMARK': 'แถม'}]

    refs = build_invoice_refs(artrn, artrnrm)

    assert refs == [{'doc_base': 'IV6900929', 'youref': 'วรันดา(L)', 'remark': 'แถม'}]


def test_build_invoice_refs_skips_docs_with_neither_field():
    artrn = [_artrn('IV6900930', '3', youref=None)]
    artrnrm = []

    refs = build_invoice_refs(artrn, artrnrm)

    assert refs == []


def test_build_invoice_refs_excludes_out_of_scope_rectyp():
    artrn = [_artrn('RE6900001', '9', youref='someone')]
    artrnrm = []

    refs = build_invoice_refs(artrn, artrnrm)

    assert refs == []


# ── idempotency + wiring (through models.import_weekly / import_router) ──
#
# empty_db clones the LIVE schema (see conftest.py) — a dev machine whose
# live DB hasn't picked up mig 124 (bsn_unit) or 132 (express_invoice_refs,
# this PR's own migration) yet would otherwise fail these tests with "no
# such column/table". Apply them defensively, mirroring
# tests/test_import_weekly_idempotent.py's _ensure_bsn_unit pattern.

def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _ensure_migrations(c):
    cols = {r[1] for r in c.execute("PRAGMA table_info(product_code_mapping)")}
    if "bsn_unit" not in cols:
        with open(_MIG_124, encoding="utf-8") as f:
            c.executescript(f.read())
    tbl = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='express_invoice_refs'"
    ).fetchone()
    if not tbl:
        with open(_MIG_132, encoding="utf-8") as f:
            c.executescript(f.read())


def _seed_product(path, name, code, unit_type='ตัว'):
    c = _conn(path)
    _ensure_migrations(c)
    cur = c.execute(
        "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, 0)",
        (name, unit_type))
    pid = cur.lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES (?, ?, ?)", (code, name, pid))
    c.commit()
    c.close()
    return pid


def _stock(path, pid):
    c = _conn(path)
    row = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    c.close()
    return row[0] if row else None


def test_dbf_entries_import_twice_is_idempotent(empty_db):
    """Building entries from DBF fixtures and importing twice must converge
    to the same stock (no double-count) — mirrors
    test_import_weekly_idempotent.py's re-import-is-noop contract."""
    import models

    pid_sale = _seed_product(empty_db, 'Psale', '804ข1108')
    pid_purch = _seed_product(empty_db, 'Ppurch', '035ป6107')

    artrn = [_artrn('IV6900929', '3', cuscod='C001')]
    stcrd_sale = [_stcrd('IV6900929', 1, stkcod='804ข1108', qty=2.0, unitpr=45.0,
                          trnval=90.0, netval=90.0)]
    aptrn = [_aptrn('RR6900077', '3', supcod='S001')]
    stcrd_purch = [_stcrd('RR6900077', 1, stkcod='035ป6107', qty=400.0, unitpr=4.80,
                           trnval=1920.0, netval=1920.0)]

    s1 = models.import_weekly(build_sales_entries(artrn, stcrd_sale, []), 'sales', 'f1')
    p1 = models.import_weekly(build_purchase_entries(aptrn, stcrd_purch, []), 'purchase', 'f1')
    assert s1['imported'] == 1
    assert p1['imported'] == 1
    assert _stock(empty_db, pid_sale) == -2.0
    assert _stock(empty_db, pid_purch) == 400.0

    # Re-build + re-import the SAME DBF fixtures — must be a pure no-op.
    s2 = models.import_weekly(build_sales_entries(artrn, stcrd_sale, []), 'sales', 'f2')
    p2 = models.import_weekly(build_purchase_entries(aptrn, stcrd_purch, []), 'purchase', 'f2')
    assert s2['unchanged'] == 1 and s2['imported'] == 0
    assert p2['unchanged'] == 1 and p2['imported'] == 0
    assert _stock(empty_db, pid_sale) == -2.0, "re-import must not double-count stock"
    assert _stock(empty_db, pid_purch) == 400.0, "re-import must not double-count stock"


def test_commit_express_dbf_wires_sales_purchase_and_refs(empty_db, monkeypatch):
    """import_router.commit_express_dbf(): open_table → build_* → import_weekly
    → express_invoice_refs upsert, end to end. open_table is monkeypatched
    (no real DBF files needed) so this exercises the wiring, not dbfread."""
    import express_dbf_source as eds
    import import_router

    _seed_product(empty_db, 'Psale2', '804ข1108')
    _seed_product(empty_db, 'Ppurch2', '035ป6107')

    fake_tables = {
        'ARTRN': [_artrn('IV6900930', '3', cuscod='C001', youref='วรันดา(L)')],
        'APTRN': [_aptrn('RR6900078', '3', supcod='S001')],
        'STCRD': [
            _stcrd('IV6900930', 1, stkcod='804ข1108', qty=1.0, unitpr=45.0, trnval=45.0, netval=45.0),
            _stcrd('RR6900078', 1, stkcod='035ป6107', qty=10.0, unitpr=4.80, trnval=48.0, netval=48.0),
        ],
        'ARMAS': [],
        'APMAS': [],
        'ARTRNRM': [{'DOCNUM': 'IV6900930', 'REMARK': 'แถม'}],
    }
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: fake_tables[name])

    result = import_router.commit_express_dbf('/fake/dataset')

    assert result['sales']['imported'] == 1
    assert result['purchase']['imported'] == 1
    assert result['invoice_refs_upserted'] == 1

    c = _conn(empty_db)
    row = c.execute(
        "SELECT youref, remark FROM express_invoice_refs WHERE doc_base='IV6900930'"
    ).fetchone()
    c.close()
    assert row['youref'] == 'วรันดา(L)'
    assert row['remark'] == 'แถม'
