"""The four per-customer AR surfaces must show the CHASEABLE population.

`/ar`, `/cashflow` and `/call` already exclude all three of Put's rulings, via
`cashflow.BSN_AR_PREDICATE`. Four per-customer surfaces did not:

    models/payments.py::_unpaid_bills            -> /customer/code/<code>
                                                 -> /m/customer/<name>
    ar_followup.py::get_customer_ar_detail       -> /accounting/ar-followup/customer/<key>
    blueprints/accounting.py::express_ar_customer-> /express/ar/customer/<code>

so a bill that was written off, already paid (`is_anomalous`), or predates the
Sendy era read as chaseable debt one click from a list that correctly drops it.
The dunning detail page — the screen opened immediately before phoning someone —
applied none of the three.

Vocabulary (CONTEXT.md): **outstanding** = what the snapshot says is unpaid.
**chaseable** = outstanding minus ar_writeoffs, minus is_anomalous, minus
pre-2024. These surfaces are all chase-facing, so all four want chaseable.

⚠ The oracle here is `cashflow.BSN_AR_PREDICATE` ITSELF, imported. A test that
re-types the predicate's SQL cannot fail when production drifts away from it —
that is precisely how this defect survived (`test_ar_reconcile._canonical_total`
re-types it, and its own comment names the risk).

⚠ `tmp_db` clones the live dev DB WITH its data, so every row this test asserts
on is FORCED, never inherited: the test deletes its own keys first, then inserts
exactly the four rows it reasons about.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import models
import ar_followup


# One clean row that MUST survive every filter. Without it an over-aggressive
# predicate that returns nothing would pass every "the bad row is gone" check.
CONTROL_DOC = 'ZZIV-CLEAN'
CODE = 'ZZTEST1'
NAME = 'ทดสอบ ลูกหนี้ตัดหนี้'
ORPHAN_NAME = 'ทดสอบ ลูกหนี้ไร้รหัส'

# doc_no -> (outstanding, why it must or must not appear)
EXPECTED_GONE = {
    'ZZIV-WOFF': 'written off (ar_writeoffs)',
    'ZZIV-ANOM': 'is_anomalous=1 — ลูกหนี้จ่ายแล้ว',
    'ZZIV-OLD':  'pre-2024 legacy debt',
}


def _seed(db_path):
    """Force exactly four rows for our test customer at the latest BSN snapshot.

    Returns the snapshot date used. Inserts at the CURRENT latest snapshot so
    the production functions (which all pin MAX(snapshot_date_iso)) see them.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        snap = conn.execute(
            "SELECT MAX(snapshot_date_iso) d FROM express_ar_outstanding WHERE entity='BSN'"
        ).fetchone()['d']
        assert snap, 'live dev DB has no BSN AR snapshot — fixture cannot run'
        batch = conn.execute(
            "SELECT id FROM express_import_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert batch, 'no express_import_log row to hang the FK on'
        batch_id = batch['id']

        # Force, do not inherit.
        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code IN (?, '')",
                     (CODE,))
        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_name IN (?, ?)",
                     (NAME, ORPHAN_NAME))
        conn.execute("DELETE FROM ar_writeoffs WHERE doc_no LIKE 'ZZIV-%'")

        rows = [
            # doc_no,      date,         anomalous, outstanding, code, name
            (CONTROL_DOC,  '2025-03-01', 0, 1000.00, CODE, NAME),
            ('ZZIV-WOFF',  '2025-03-02', 0, 2000.00, CODE, NAME),
            ('ZZIV-ANOM',  '2025-03-03', 1, 3000.00, CODE, NAME),
            ('ZZIV-OLD',   '2023-12-31', 0, 4000.00, CODE, NAME),
        ]
        for doc, d, anom, out, code, name in rows:
            conn.execute("""
                INSERT INTO express_ar_outstanding
                    (batch_id, snapshot_date_iso, customer_code, customer_name,
                     doc_date_iso, doc_no, is_anomalous, bill_amount,
                     paid_amount, outstanding_amount, entity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'BSN')
            """, (batch_id, snap, code, name, d, doc, anom, out, out))

        # The orphan (no customer_code) exercises get_customer_ar_detail's
        # SECOND query branch — fixing only the code branch would leave this one
        # broken and no code-keyed test would notice.
        for doc, d, anom, out in [(CONTROL_DOC + '-O', '2025-03-01', 0, 1000.00),
                                  ('ZZIV-WOFF-O', '2025-03-02', 0, 2000.00),
                                  ('ZZIV-ANOM-O', '2025-03-03', 1, 3000.00),
                                  ('ZZIV-OLD-O',  '2023-12-31', 0, 4000.00)]:
            conn.execute("""
                INSERT INTO express_ar_outstanding
                    (batch_id, snapshot_date_iso, customer_code, customer_name,
                     doc_date_iso, doc_no, is_anomalous, bill_amount,
                     paid_amount, outstanding_amount, entity)
                VALUES (?, ?, '', ?, ?, ?, ?, ?, 0, ?, 'BSN')
            """, (batch_id, snap, ORPHAN_NAME, d, doc, anom, out, out))

        for doc in ('ZZIV-WOFF', 'ZZIV-WOFF-O'):
            conn.execute("""
                INSERT INTO ar_writeoffs
                    (doc_no, customer_code, customer_name, amount, type,
                     writeoff_date, reason, excludes_revenue)
                VALUES (?, ?, ?, 2000.00, 'expense', '2026-01-01',
                        'test fixture — chaseable population', 0)
            """, (doc, CODE, NAME))
        conn.commit()
        return snap
    finally:
        conn.close()


def _docs(rows, key='doc_base'):
    out = []
    for r in rows:
        try:
            out.append(r[key])
        except (KeyError, IndexError, TypeError):
            out.append(r['doc_no'])
    return out


# ── models/payments.py::_unpaid_bills — /customer/code/<code> + mobile ────────

def test_unpaid_bills_by_code_shows_only_chaseable(tmp_db):
    _seed(tmp_db)
    rows, snap = models.get_customer_unpaid_bills_by_code(CODE)

    docs = _docs(rows)
    # COUNT FIRST: an empty result would make every "not in" below vacuous.
    assert len(docs) == 1, f'expected only the control row, got {docs}'
    assert docs == [CONTROL_DOC]
    for doc, why in EXPECTED_GONE.items():
        assert doc not in docs, f'{doc} still chaseable — {why}'
    assert round(sum(float(r['total_net'] or 0) for r in rows), 2) == 1000.00


def test_unpaid_bills_by_name_shows_only_chaseable(tmp_db):
    """The mobile page keys by NAME, the desktop page by CODE — two entry
    points into the same helper. Both must return the chaseable population."""
    _seed(tmp_db)
    rows, snap = models.get_customer_unpaid_bills(NAME)

    docs = _docs(rows)
    assert len(docs) == 1, f'expected only the control row, got {docs}'
    assert docs == [CONTROL_DOC]
    assert round(sum(float(r['total_net'] or 0) for r in rows), 2) == 1000.00


# ── ar_followup.py::get_customer_ar_detail — the dunning detail page ──────────

def test_dunning_detail_by_code_shows_only_chaseable(tmp_db):
    _seed(tmp_db)
    rows = ar_followup.get_customer_ar_detail(CODE, db_path=tmp_db)

    docs = [r['doc_no'] for r in rows]
    assert len(docs) == 1, f'expected only the control row, got {docs}'
    assert docs == [CONTROL_DOC]
    for doc, why in EXPECTED_GONE.items():
        assert doc not in docs, f'{doc} shown before a phone call — {why}'


def test_dunning_detail_orphan_by_name_shows_only_chaseable(tmp_db):
    """get_customer_ar_detail has TWO query branches. This pins the orphan /
    walk-in branch (no customer_code), which a code-keyed test cannot reach."""
    _seed(tmp_db)
    rows = ar_followup.get_customer_ar_detail(ORPHAN_NAME, db_path=tmp_db)

    docs = [r['doc_no'] for r in rows]
    assert len(docs) == 1, f'expected only the control row, got {docs}'
    assert docs == [CONTROL_DOC + '-O']


# ── blueprints/accounting.py::express_ar_customer ─────────────────────────────

def test_express_ar_customer_page_shows_only_chaseable(tmp_db):
    """The chaseable TABLE and its totals hold only chaseable documents.

    ⚠ Widened 2026-09-09 (#470). This asserted `doc not in html`, which was
    right while nothing on the page mentioned a removed bill and became wrong
    the moment #468 shipped the disclosure: the page now renders exactly these
    documents in a separate "หนี้ที่ไม่นับว่าตามได้" section, on purpose. A
    guard that goes red when the thing it guards is finally fixed teaches people
    to delete guards, so what it pins is now WHERE each document appears —
    above the section marker is the chaseable list, below it is the disclosure.
    """
    _seed(tmp_db)
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')
    from app import app
    app.config['WTF_CSRF_ENABLED'] = False
    client = app.test_client()
    with client.session_transaction() as s:
        s['role'] = 'admin'; s['username'] = 'test'; s['user_id'] = 1

    html = client.get(f'/express/ar/customer/{CODE}').get_data(as_text=True)

    # Control first: the page must actually be rendering our customer at all,
    # or every absence assertion below is meaningless.
    assert CONTROL_DOC in html, 'control row missing — page did not render our data'

    marker = 'หนี้ที่ไม่นับว่าตามได้'
    assert marker in html, 'the excluded-docs section is gone — see #468 / #470'
    chaseable_part, excluded_part = html.split(marker, 1)
    # Second control: the split put the chaseable table on the side we think.
    assert CONTROL_DOC in chaseable_part, 'a chaseable bill fell below the section'

    for doc, why in EXPECTED_GONE.items():
        assert doc not in chaseable_part, f'{doc} rendered as chaseable — {why}'
        assert doc in excluded_part, (
            f'{doc} was removed from the total and never disclosed — {why}')


# ── the predicate is the oracle, and it is imported, never re-typed ───────────

def test_surfaces_use_the_imported_predicate_not_a_retyped_copy():
    """Guard the guard: if someone re-types the predicate into one of these
    modules instead of importing it, this goes red. Re-typed SQL is how the
    four surfaces drifted from /ar in the first place."""
    import cashflow
    import inspect

    assert 'ar_writeoffs' in cashflow.BSN_AR_PREDICATE
    assert 'is_anomalous' in cashflow.BSN_AR_PREDICATE
    assert '2024-01-01' in cashflow.BSN_AR_PREDICATE

    for mod in (models.payments if hasattr(models, 'payments') else None,
                ar_followup):
        if mod is None:
            continue
        src = inspect.getsource(mod)
        # The literal clause must not be hand-written in a consumer module.
        assert "doc_no NOT IN (SELECT doc_no FROM ar_writeoffs)" not in src, (
            f'{mod.__name__} re-types the write-off clause instead of importing '
            'BSN_AR_PREDICATE — that is the drift this whole file guards')
