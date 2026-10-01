"""`ar_statement` is the one reader of chaseable AR (ADR 0023).

Every row asserted here is FORCED into a copy of the dev DB at its latest BSN
snapshot, never inherited: the seed deletes its own keys first.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from datetime import date, timedelta

import ar_statement


CODE = 'ZZSTM1'
NAME = 'ทดสอบ ใบแจ้งยอด'
PAD_CODE = 'ZZSTM2'
BLANK_NAME = 'ทดสอบ ไม่มีรหัส'

# doc_no -> (days before the snapshot, is_anomalous, outstanding, in ar_writeoffs)
CHASEABLE = {
    'ZZSTM-A': (10, 0, 1000.00, False),
    'ZZSTM-B': (45, 0, 250.50, False),
    'ZZSTM-CR': (5, 0, -120.00, False),     # credit row: chaseable, not a bill
}
EXCLUDED = {
    'ZZSTM-WO': (20, 0, 700.00, True),
    'ZZSTM-RE': (15, 1, 300.00, False),
}
LEGACY_DOC = 'ZZSTM-OLD'                    # pre-2024 legacy


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        snap = conn.execute("SELECT MAX(snapshot_date_iso) d FROM express_ar_outstanding"
                            " WHERE entity='BSN'").fetchone()['d']
        assert snap, 'dev DB has no BSN AR snapshot'
        batch_id = conn.execute(
            "SELECT id FROM express_import_log ORDER BY id DESC LIMIT 1").fetchone()['id']
        conn.execute("DELETE FROM express_ar_outstanding WHERE TRIM(customer_code) IN (?, ?)"
                     " OR customer_name = ?", (CODE, PAD_CODE, BLANK_NAME))
        conn.execute("DELETE FROM ar_writeoffs WHERE doc_no LIKE 'ZZSTM-%'")
        snap_d = date.fromisoformat(snap)

        def ins(code, doc, doc_date, anom, out, name=NAME):
            conn.execute("""
                INSERT INTO express_ar_outstanding
                    (batch_id, snapshot_date_iso, customer_code, customer_name,
                     doc_date_iso, doc_no, is_anomalous, bill_amount,
                     paid_amount, outstanding_amount, entity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'BSN')
            """, (batch_id, snap, code, name, doc_date, doc, anom, out, out))

        for doc, (days, anom, out, wo) in {**CHASEABLE, **EXCLUDED}.items():
            ins(CODE, doc, (snap_d - timedelta(days=days)).isoformat(), anom, out)
            if wo:
                conn.execute("""
                    INSERT INTO ar_writeoffs (doc_no, customer_code, customer_name, amount,
                                              type, writeoff_date, reason, excludes_revenue)
                    VALUES (?, ?, ?, ?, 'expense', '2026-01-01', 'ทดสอบ', 0)
                """, (doc, CODE, NAME, out))
        ins(CODE, LEGACY_DOC, '2023-06-30', 0, 4000.00)
        ins(PAD_CODE + ' ', 'ZZSTM-PAD', (snap_d - timedelta(days=3)).isoformat(), 0, 640.00)
        ins('', 'ZZSTM-BLANK', (snap_d - timedelta(days=3)).isoformat(), 0, 90.00,
            name=BLANK_NAME)
        conn.commit()
        return snap
    finally:
        conn.close()


def _snapshot_rows(db_path, code):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT doc_no FROM express_ar_outstanding WHERE entity='BSN'"
            " AND snapshot_date_iso=(SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding"
            " WHERE entity='BSN') AND TRIM(customer_code)=?", (code,))}
    finally:
        conn.close()


def test_chaseable_and_excluded_partition_the_snapshot_rows(tmp_db):
    _seed(tmp_db)
    st = ar_statement.customer_statement(CODE, db_path=tmp_db)
    chase = {r['doc_no'] for r in st['chaseable']}
    excl = {r['doc_no'] for r in st['excluded']}
    assert chase == set(CHASEABLE)
    assert excl == set(EXCLUDED) | {LEGACY_DOC}
    assert not chase & excl
    assert chase | excl == _snapshot_rows(tmp_db, CODE)
    reasons = {r['doc_no']: r['excluded_by'] for r in st['excluded']}
    assert reasons == {'ZZSTM-WO': 'writeoff', 'ZZSTM-RE': 're', LEGACY_DOC: 'legacy'}
    assert st['total'] == round(sum(v[2] for v in CHASEABLE.values()), 2) == 1130.50


def test_bills_keep_the_outstanding_above_zero_choice_as_a_named_field(tmp_db):
    _seed(tmp_db)
    st = ar_statement.customer_statement(CODE, db_path=tmp_db)
    assert len(st['chaseable']) == 3
    assert {r['doc_no'] for r in st['bills']} == {'ZZSTM-A', 'ZZSTM-B'}
    # The credit row is chaseable and counts toward the total, but is not a bill.
    assert 'ZZSTM-CR' in {r['doc_no'] for r in st['chaseable']}


def test_code_matches_the_trimmed_snapshot_code(tmp_db):
    _seed(tmp_db)
    st = ar_statement.customer_statement(PAD_CODE, db_path=tmp_db)
    assert [r['doc_no'] for r in st['chaseable']] == ['ZZSTM-PAD']
    assert st['total'] == 640.00
    assert ar_statement.customer_statement(' ' + PAD_CODE + ' ', db_path=tmp_db)['total'] == 640.00


def test_freshness_at_the_threshold_with_a_supplied_as_of(tmp_db):
    snap = _seed(tmp_db)
    d = date.fromisoformat(snap)

    def at(days):
        return ar_statement.freshness(as_of=(d + timedelta(days=days)).isoformat(),
                                      db_path=tmp_db)

    same, edge, past, future = at(0), at(ar_statement.AR_SNAPSHOT_STALE_AFTER_DAYS), \
        at(ar_statement.AR_SNAPSHOT_STALE_AFTER_DAYS + 1), at(-1)
    assert (same['age_days'], same['is_stale']) == (0, False)
    assert (edge['age_days'], edge['is_stale']) == (1, False)
    assert (past['age_days'], past['is_stale']) == (2, True)
    assert (future['age_days'], future['is_stale']) == (-1, True)
    assert same['as_of'] == snap == same['snapshot_date']
    # The threshold the banner prints is the oldest age still read as fresh.
    assert same['stale_after_days'] == edge['age_days']


def test_freshness_with_no_snapshot_is_stale(empty_db):
    f = ar_statement.freshness(as_of='2026-10-02', db_path=empty_db)
    assert f == {'as_of': '2026-10-02', 'snapshot_date': None, 'age_days': None,
                 'is_stale': True, 'stale_after_days': 1}


def test_age_days_count_from_the_snapshot_date(tmp_db):
    _seed(tmp_db)
    st = ar_statement.customer_statement(CODE, db_path=tmp_db)
    ages = {r['doc_no']: r['age_days'] for r in st['chaseable']}
    assert ages == {doc: v[0] for doc, v in CHASEABLE.items()}


def test_totals_equal_the_sum_of_statements(tmp_db):
    _seed(tmp_db)
    totals = ar_statement.customer_totals(db_path=tmp_db)
    coded = [t for t in totals if t['customer_code']]
    assert len(coded) >= 3
    by_code = {t['customer_code']: t for t in coded}
    assert by_code[CODE]['outstanding'] == 1130.50
    assert by_code[PAD_CODE]['outstanding'] == 640.00
    for t in coded:
        st = ar_statement.customer_statement(t['customer_code'], db_path=tmp_db)
        assert t['outstanding'] == st['total'], t['customer_code']
        assert t['invoice_count'] == len(st['chaseable']), t['customer_code']
    # A blank-code row keeps its own group under its name, with no code.
    blank = [t for t in totals if t['customer'] == BLANK_NAME]
    assert [(b['customer_code'], b['outstanding']) for b in blank] == [(None, 90.00)]


def test_unknown_or_blank_code_is_an_empty_statement(tmp_db):
    _seed(tmp_db)
    for code in ('ZZSTM-NOPE', '', '   '):
        st = ar_statement.customer_statement(code, db_path=tmp_db)
        assert (st['chaseable'], st['excluded'], st['total']) == ([], [], 0)
