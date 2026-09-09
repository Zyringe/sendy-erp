"""The documents REMOVED from the chaseable total must stay visible, per customer.

Phase 1 (#465) made the four per-customer AR surfaces read the **chaseable**
population, so their totals finally agree with `/ar`. The bills that were removed
then became invisible: a person on the phone who remembers an invoice has no way
to see what happened to it.

This file pins the seam that gives them back — the per-document complement of
`cashflow.BSN_AR_PREDICATE`, scoped to one customer:

    cashflow.bsn_ar_excluded_docs_by_code(code)  -> (rows, snapshot_date)
    cashflow.bsn_ar_excluded_docs(name)          -> (rows, snapshot_date)

Vocabulary (CONTEXT.md): **outstanding** = what the snapshot says is unpaid.
**chaseable** = outstanding minus ar_writeoffs, minus is_anomalous, minus
pre-2024. These functions return exactly `outstanding − chaseable`.

⚠ The load-bearing test here is the PARTITION: chaseable ∪ excluded must equal
every row the customer has in the latest snapshot, with no overlap. Anything
weaker lets a document fall out of both lists and become invisible — which is
the failure this whole ticket exists to prevent, in mirror image.

⚠ The chaseable side of that partition comes from `ar_followup.get_customer_ar_detail`
(an existing helper that applies the imported predicate and nothing else), NOT
from a re-typed query, and NOT from `models.get_customer_unpaid_bills*` — those
carry an extra `outstanding_amount > 0` clause on purpose (ADR 0012) and so are
a deliberately smaller set than the predicate alone.

⚠ `tmp_db` clones the live dev DB WITH its data, so every row asserted on here is
FORCED, never inherited: the fixture deletes its own keys first, then inserts
exactly the rows it reasons about.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import cashflow
import ar_followup
import models


CODE = 'ZZEXCL1'
NAME = 'ทดสอบ ลูกหนี้ที่ไม่ตาม'
ORPHAN_NAME = 'ทดสอบ ลูกหนี้ที่ไม่ตาม ไร้รหัส'

# The row that must survive the predicate — it is chaseable, so it must NEVER
# appear in the excluded list. Without it, a function that returned every row
# would pass every "the excluded row is present" assertion below.
CONTROL_DOC = 'ZZEX-CLEAN'

# A second customer whose every bill is chaseable — nothing to disclose.
CLEAN_CODE = 'ZZEXCL2'
CLEAN_NAME = 'ทดสอบ ลูกหนี้สะอาด'

# A customer whose EVERY bill is excluded. `/express/ar/customer/<code>` told
# this person "ไม่พบลูกหนี้รหัส X" and redirected — 33 of the 60 customers in the
# prod snapshot (2026-09-08) are in exactly this state.
ONLY_EXCL_CODE = 'ZZEXCL3'
ONLY_EXCL_NAME = 'ทดสอบ ลูกหนี้ที่ตัดหมดแล้ว'

# Stored with a TRAILING SPACE, reached from the URL without one. The two lists
# on one page must key identically; before #470/#471 the chaseable side matched
# `customer_code = ?` while the excluded wrapper TRIMs, so this customer landed
# on one list and not the other.
PAD_CODE = 'ZZEXCL4'
PAD_NAME = 'ทดสอบ รหัสมีช่องว่างท้าย'

# A CHASEABLE credit row (outstanding < 0). `_unpaid_bills` drops it on purpose
# (ADR 0012 — a per-customer bill list should not render a credit) while the
# excluded helper does not filter on the amount at all. That one clause is the
# whole legitimate gap between the two, and it needs a customer that actually
# HAS such a row: without one the `> 0` filter never fires, and a test claiming
# to pin the gap passes with the clause deleted.
CREDIT_CODE = 'ZZEXCL5'
CREDIT_NAME = 'ทดสอบ ลูกหนี้มีใบลดหนี้'

# doc_no -> the bucket it must land in, and why
EXPECTED = {
    'ZZEX-WOFF':  'writeoff',   # 2025, in ar_writeoffs, type=expense
    'ZZEX-WBACK': 'writeoff',   # 2025, in ar_writeoffs, type=writeback
    'ZZEX-ANOM':  're',         # is_anomalous=1 — ลูกหนี้จ่ายแล้ว
    'ZZEX-OLD':   'legacy',     # pre-2024
    # Both written off AND pre-2024. `bsn_ar_excluded()` scopes its writeoff
    # bucket to the collectable date window precisely so this doc is counted
    # ONCE, as legacy. Re-inventing the buckets here would double-count it.
    'ZZEX-OLDWO': 'legacy',
    # Anomalous AND written off — the shape จึงเจริญ actually has on prod (all
    # 11 of its rows). `re` wins the bucket, and the write-off decision is still
    # disclosed, so the row must carry BOTH labels.
    'ZZEX-ANOMWO': 're',
}

_SEEDED = {
    # doc_no,       date,         anomalous, outstanding
    CONTROL_DOC:   ('2025-03-01', 0, 1000.00),
    'ZZEX-WOFF':   ('2025-03-02', 0, 2000.00),
    'ZZEX-WBACK':  ('2025-03-03', 0, -500.00),
    'ZZEX-ANOM':   ('2025-03-04', 1, 3000.00),
    'ZZEX-OLD':    ('2023-12-31', 0, 4000.00),
    'ZZEX-OLDWO':  ('2023-11-30', 0, 5000.00),
    'ZZEX-ANOMWO': ('2025-04-10', 1, 1500.00),
}

WRITEOFFS = {
    'ZZEX-WOFF':  ('expense',   '2026-01-15', 'ลูกค้าปิดกิจการ — ตัดเป็นค่าใช้จ่าย'),
    'ZZEX-WBACK': ('writeback', '2026-02-20', 'ตั้งกลับรายการที่ตัดไปแล้ว'),
    'ZZEX-OLDWO': ('expense',   '2026-01-15', 'หนี้เก่าที่ถูกตัดด้วย'),
    'ZZEX-ANOMWO': ('expense',  '2026-03-03', 'ปิดบัญชี ตัดพร้อมกับ RE'),
}


def _seed(db_path):
    """Force the six rows above for our test customer at the latest snapshot.

    Also seeds an orphan (blank customer_code) copy so the NAME-keyed wrapper
    has its own population — fixing only the code path would otherwise pass.
    Returns the snapshot date used.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        snap = conn.execute(
            "SELECT MAX(snapshot_date_iso) d FROM express_ar_outstanding WHERE entity='BSN'"
        ).fetchone()['d']
        assert snap, 'live dev DB has no BSN AR snapshot — fixture cannot run'
        batch = conn.execute(
            "SELECT id FROM express_import_log ORDER BY id DESC LIMIT 1").fetchone()
        assert batch, 'no express_import_log row to hang the FK on'
        batch_id = batch['id']

        # Force, do not inherit.
        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code = ?", (CODE,))
        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_name IN (?, ?)",
                     (NAME, ORPHAN_NAME))
        conn.execute("DELETE FROM ar_writeoffs WHERE doc_no LIKE 'ZZEX-%'")

        def _ins(code, name, doc, d, anom, out):
            conn.execute("""
                INSERT INTO express_ar_outstanding
                    (batch_id, snapshot_date_iso, customer_code, customer_name,
                     doc_date_iso, doc_no, is_anomalous, bill_amount,
                     paid_amount, outstanding_amount, entity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'BSN')
            """, (batch_id, snap, code, name, d, doc, anom, out, out))

        for doc, (d, anom, out) in _SEEDED.items():
            _ins(CODE, NAME, doc, d, anom, out)
            _ins('', ORPHAN_NAME, doc + '-O', d, anom, out)

        # A customer with NOTHING excluded — the control for every "the section
        # is shown" assertion. Without it, a partial that renders unconditionally
        # would pass all of them.
        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code = ?",
                     (CLEAN_CODE,))
        _ins(CLEAN_CODE, CLEAN_NAME, 'ZZEX-ONLYCLEAN', '2025-05-05', 0, 700.00)

        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code = ?",
                     (ONLY_EXCL_CODE,))
        _ins(ONLY_EXCL_CODE, ONLY_EXCL_NAME, 'ZZEX-ALLGONE', '2025-06-06', 1, 800.00)

        conn.execute("DELETE FROM express_ar_outstanding WHERE TRIM(customer_code) = ?",
                     (PAD_CODE,))
        _ins(PAD_CODE + ' ', PAD_NAME, 'ZZEX-PAD-OK', '2025-07-07', 0, 600.00)
        _ins(PAD_CODE + ' ', PAD_NAME, 'ZZEX-PAD-RE', '2025-07-08', 1, 900.00)

        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code = ?",
                     (CREDIT_CODE,))
        _ins(CREDIT_CODE, CREDIT_NAME, 'ZZEX-CR-BILL', '2025-08-01', 0, 1200.00)
        _ins(CREDIT_CODE, CREDIT_NAME, 'ZZEX-CR-CREDIT', '2025-08-02', 0, -250.00)
        _ins(CREDIT_CODE, CREDIT_NAME, 'ZZEX-CR-RE', '2025-08-03', 1, 400.00)

        for doc, (wtype, wdate, reason) in WRITEOFFS.items():
            for suffix in ('', '-O'):
                conn.execute("""
                    INSERT INTO ar_writeoffs
                        (doc_no, customer_code, customer_name, amount, type,
                         writeoff_date, reason, excludes_revenue)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """, (doc + suffix, CODE, NAME, _SEEDED[doc][2], wtype, wdate, reason))
        conn.commit()
        return snap
    finally:
        conn.close()


def _by_doc(rows):
    return {r['doc_no']: r for r in rows}


# ── the population ───────────────────────────────────────────────────────────

def test_returns_every_excluded_doc_and_never_the_chaseable_one(tmp_db):
    _seed(tmp_db)
    rows, snap = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)

    docs = sorted(r['doc_no'] for r in rows)
    # COUNT FIRST. An empty result makes every "is not in" below vacuous.
    assert len(docs) == len(EXPECTED), f'expected {len(EXPECTED)} excluded docs, got {docs}'
    assert docs == sorted(EXPECTED)
    assert CONTROL_DOC not in docs, 'a CHASEABLE bill was listed as excluded'


def test_snapshot_date_is_the_latest_bsn_snapshot(tmp_db):
    snap = _seed(tmp_db)
    _rows, returned = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    assert returned == snap


def test_only_the_latest_snapshot_is_returned(tmp_db):
    """An older snapshot's rows for the same customer must not leak in."""
    snap = _seed(tmp_db)
    conn = sqlite3.connect(tmp_db)
    try:
        batch_id = conn.execute(
            "SELECT id FROM express_import_log ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute("""
            INSERT INTO express_ar_outstanding
                (batch_id, snapshot_date_iso, customer_code, customer_name,
                 doc_date_iso, doc_no, is_anomalous, bill_amount,
                 paid_amount, outstanding_amount, entity)
            VALUES (?, '2000-01-01', ?, ?, '2023-01-01', 'ZZEX-STALE', 0, 9.0, 0, 9.0, 'BSN')
        """, (batch_id, CODE, NAME))
        conn.commit()
    finally:
        conn.close()

    rows, _snap = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    docs = [r['doc_no'] for r in rows]
    # Control: the current-snapshot rows are still there, so a bare "STALE not
    # in docs" cannot pass by the function returning nothing.
    assert len(docs) == len(EXPECTED), docs
    assert 'ZZEX-STALE' not in docs


# ── the buckets ──────────────────────────────────────────────────────────────

def test_each_row_carries_exactly_one_reason_and_it_is_the_right_one(tmp_db):
    _seed(tmp_db)
    rows, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    assert len(rows) == len(EXPECTED)

    got = {r['doc_no']: r['excluded_by'] for r in rows}
    assert got == EXPECTED
    assert set(got.values()) <= {'writeoff', 're', 'legacy'}


def test_a_writeoff_that_is_also_pre_2024_is_counted_once_as_legacy(tmp_db):
    """`bsn_ar_excluded()` scopes its writeoff bucket to the collectable date
    window so the buckets stay disjoint. This function must copy that, not
    re-invent it — otherwise ZZEX-OLDWO appears twice or in the wrong bucket."""
    _seed(tmp_db)
    rows, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)

    hits = [r for r in rows if r['doc_no'] == 'ZZEX-OLDWO']
    assert len(hits) == 1, f'ZZEX-OLDWO appeared {len(hits)} times — buckets overlap'
    assert hits[0]['excluded_by'] == 'legacy'


def test_writeoff_rows_carry_their_type_date_and_reason(tmp_db):
    _seed(tmp_db)
    rows, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    by_doc = _by_doc(rows)
    assert len(by_doc) == len(EXPECTED)

    woff = by_doc['ZZEX-WOFF']
    assert woff['writeoff_type'] == 'expense'
    assert woff['writeoff_date'] == '2026-01-15'
    assert woff['writeoff_reason'] == 'ลูกค้าปิดกิจการ — ตัดเป็นค่าใช้จ่าย'


def test_a_writeback_is_reported_as_writeback_not_folded_into_expense(tmp_db):
    """A reversed decision must not read as a forgiven bill."""
    _seed(tmp_db)
    rows, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    by_doc = _by_doc(rows)
    # Control: the expense write-off is present in the same result, so this
    # cannot pass because the function dropped every write-off.
    assert by_doc['ZZEX-WOFF']['writeoff_type'] == 'expense'
    assert by_doc['ZZEX-WBACK']['writeoff_type'] == 'writeback'


def test_re_and_legacy_rows_carry_no_writeoff_metadata(tmp_db):
    _seed(tmp_db)
    rows, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    by_doc = _by_doc(rows)
    # Control: a row that SHOULD carry metadata does, in the same result.
    assert by_doc['ZZEX-WOFF']['writeoff_date'] is not None

    for doc in ('ZZEX-ANOM', 'ZZEX-OLD'):
        r = by_doc[doc]
        assert r['writeoff_type'] is None, doc
        assert r['writeoff_date'] is None, doc
        assert r['writeoff_reason'] is None, doc


def test_rows_carry_the_amounts_and_identity_the_page_renders(tmp_db):
    _seed(tmp_db)
    rows, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    r = _by_doc(rows)['ZZEX-ANOM']
    assert r['doc_date_iso'] == '2025-03-04'
    assert r['customer_code'] == CODE
    assert r['customer'] == NAME
    assert round(float(r['outstanding']), 2) == 3000.00
    assert round(float(r['bill_amount']), 2) == 3000.00
    assert round(float(r['paid_amount']), 2) == 0.00


# ── the load-bearing property ────────────────────────────────────────────────

def test_chaseable_and_excluded_partition_the_customers_snapshot(tmp_db):
    """chaseable ∪ excluded == every row this customer has in the snapshot,
    disjoint, and the outstanding amounts add up.

    This is the assertion that makes the pair trustworthy: it fails if the
    complement drifts from the predicate in EITHER direction — a doc shown on
    both lists, or a doc that has quietly become invisible on both.
    """
    _seed(tmp_db)

    excluded, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    chaseable = ar_followup.get_customer_ar_detail(CODE, db_path=tmp_db)

    exc_docs = {r['doc_no'] for r in excluded}
    cha_docs = {r['doc_no'] for r in chaseable}

    # Control: BOTH sides must be non-empty, or "disjoint" and "union" are free.
    assert cha_docs, 'no chaseable rows — the partition assertions would be vacuous'
    assert exc_docs, 'no excluded rows — the partition assertions would be vacuous'

    assert exc_docs & cha_docs == set(), f'counted twice: {exc_docs & cha_docs}'
    assert exc_docs | cha_docs == set(_SEEDED), (
        f'invisible on both lists: {set(_SEEDED) - (exc_docs | cha_docs)}')

    total = round(sum(float(r['outstanding']) for r in excluded)
                  + sum(float(r['outstanding']) for r in chaseable), 2)
    assert total == round(sum(v[2] for v in _SEEDED.values()), 2)


# ── the name-keyed wrapper ───────────────────────────────────────────────────

def test_name_keyed_wrapper_covers_the_orphan_population(tmp_db):
    """The mobile page keys by NAME. A customer with no code is only reachable
    that way, so fixing the code path alone would leave this surface empty."""
    _seed(tmp_db)
    rows, _ = cashflow.bsn_ar_excluded_docs(ORPHAN_NAME, db_path=tmp_db)

    docs = sorted(r['doc_no'] for r in rows)
    assert len(docs) == len(EXPECTED), docs
    assert docs == sorted(d + '-O' for d in EXPECTED)
    assert CONTROL_DOC + '-O' not in docs


def test_name_and_code_wrappers_agree_for_a_customer_reachable_both_ways(tmp_db):
    _seed(tmp_db)
    by_code, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)
    by_name, _ = cashflow.bsn_ar_excluded_docs(NAME, db_path=tmp_db)

    assert len(by_code) == len(EXPECTED)
    assert ({(r['doc_no'], r['excluded_by']) for r in by_code}
            == {(r['doc_no'], r['excluded_by']) for r in by_name})


# ── the complement is derived from the predicate, not re-typed ───────────────

def test_the_complement_is_not_a_hand_typed_copy_of_the_predicate():
    """Guard the guard. The whole point of ADR 0012 is that the population is
    defined once; a hand-typed complement is the same defect in mirror image.

    ⚠ The DOCSTRING of `_excluded_docs` names BSN_AR_PREDICATE several times, so
    a bare `'BSN_AR_PREDICATE' in source` check passes on prose alone — it stayed
    green through a mutation that hand-typed the whole clause into the SQL. Strip
    the docstring first, and keep a control so a strip that ate everything fails
    loudly instead of passing."""
    import inspect
    src = inspect.getsource(cashflow._excluded_docs)
    body = src.replace(cashflow._excluded_docs.__doc__ or '', '')

    # Control: the SQL survived the strip, so the assertions below have a subject.
    assert 'SELECT ao.doc_no' in body, 'docstring strip removed the query itself'

    assert 'NOT ({BSN_AR_PREDICATE})' in body, (
        '_excluded_docs must build its WHERE from the imported BSN_AR_PREDICATE')
    assert "doc_no NOT IN (SELECT doc_no FROM ar_writeoffs)" not in body, (
        'the write-off clause is hand-typed into the query instead of imported — '
        'that is the ADR 0012 drift in mirror image')


def test_one_writeoff_decision_per_doc_is_a_db_invariant(tmp_db):
    """The write-off join in `_excluded_docs` is a plain LEFT JOIN, which is only
    safe because `ar_writeoffs` carries UNIQUE(doc_no). If that constraint is
    ever dropped, a doc with two write-off rows would fan out into two rows and
    silently break the chaseable/excluded partition on a money page — so pin the
    assumption here rather than defending against it in the query."""
    _seed(tmp_db)
    conn = sqlite3.connect(tmp_db)
    try:
        # Control: the row we are about to duplicate really is there.
        assert conn.execute("SELECT COUNT(*) FROM ar_writeoffs WHERE doc_no='ZZEX-WOFF'"
                            ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("""
                INSERT INTO ar_writeoffs
                    (doc_no, customer_code, customer_name, amount, type,
                     writeoff_date, reason, excludes_revenue)
                VALUES ('ZZEX-WOFF', ?, ?, 2000.00, 'writeback', '2026-06-30',
                        'a second decision on the same doc', 0)
            """, (CODE, NAME))
    finally:
        conn.close()


# ── ar_followup: the code-or-name fork the dunning page goes through ─────────

def test_dunning_helper_resolves_a_code_to_the_same_rows(tmp_db):
    _seed(tmp_db)
    rows = ar_followup.get_customer_excluded_docs(CODE, db_path=tmp_db)
    docs = sorted(r['doc_no'] for r in rows)
    assert len(docs) == len(EXPECTED), docs
    assert docs == sorted(EXPECTED)


def test_dunning_helper_resolves_an_orphan_name(tmp_db):
    """The dunning page keys by code OR name. Resolving the excluded side
    differently from the chaseable side is how a doc ends up on neither list,
    so both go through the same `_resolve_target`."""
    _seed(tmp_db)
    rows = ar_followup.get_customer_excluded_docs(ORPHAN_NAME, db_path=tmp_db)
    docs = sorted(r['doc_no'] for r in rows)
    assert len(docs) == len(EXPECTED), docs
    assert docs == sorted(d + '-O' for d in EXPECTED)


def test_dunning_helper_and_chaseable_helper_partition_the_orphan_too(tmp_db):
    """The partition must hold on the NAME branch as well — it is a different
    query in both helpers."""
    _seed(tmp_db)
    excluded = ar_followup.get_customer_excluded_docs(ORPHAN_NAME, db_path=tmp_db)
    chaseable = ar_followup.get_customer_ar_detail(ORPHAN_NAME, db_path=tmp_db)

    exc = {r['doc_no'] for r in excluded}
    cha = {r['doc_no'] for r in chaseable}
    assert exc and cha, 'one side is empty — the assertions below would be vacuous'
    assert exc & cha == set()
    assert exc | cha == {d + '-O' for d in _SEEDED}


# ── the dunning page renders it ──────────────────────────────────────────────

def _manager_client():
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')
    from app import app
    app.config['WTF_CSRF_ENABLED'] = False
    client = app.test_client()
    with client.session_transaction() as s:
        s['role'] = 'admin'; s['username'] = 'test'; s['user_id'] = 1
    return client


def test_dunning_page_shows_every_excluded_doc_with_its_reason(tmp_db):
    _seed(tmp_db)
    html = _manager_client().get(
        f'/accounting/ar-followup/customer/{CODE}').get_data(as_text=True)

    # Control FIRST: the page rendered our customer's chaseable bill at all.
    assert CONTROL_DOC in html, 'page did not render our data — nothing below means anything'

    for doc in EXPECTED:
        assert doc in html, f'{doc} was removed from the total and never disclosed'

    # Assert on the ELEMENT, not a bare substring: a badge label can occur inside
    # a longer string, and a stray match would hide a missing badge.
    assert '>ตัดหนี้สูญ<' in html
    assert '>ตั้งกลับ<' in html
    assert '>ลูกค้าจ่ายแล้ว (RE)<' in html
    assert '>หนี้ก่อนปี 2024<' in html
    assert 'ลูกค้าปิดกิจการ — ตัดเป็นค่าใช้จ่าย' in html, 'write-off reason not shown'
    assert '2026-01-15' in html, 'write-off date not shown'


def test_dunning_page_states_the_total_is_chaseable_and_what_was_removed(tmp_db):
    _seed(tmp_db)
    html = _manager_client().get(
        f'/accounting/ar-followup/customer/{CODE}').get_data(as_text=True)
    assert '>หนี้ที่ไม่นับว่าตามได้<' in html
    # 6 excluded docs; 2000 - 500 + 3000 + 4000 + 5000 + 1500 = 15,000.00
    assert 'ตัด 6 ใบ' in html
    assert '฿15,000.00' in html


def test_dunning_page_omits_the_section_when_nothing_was_excluded(tmp_db):
    """The control for the two tests above. A partial that rendered
    unconditionally would pass both of them and fail only here."""
    _seed(tmp_db)
    html = _manager_client().get(
        f'/accounting/ar-followup/customer/{CLEAN_CODE}').get_data(as_text=True)

    assert 'ZZEX-ONLYCLEAN' in html, 'page did not render the clean customer at all'
    assert '>หนี้ที่ไม่นับว่าตามได้<' not in html
    assert '>ตัดหนี้สูญ<' not in html


# ── the two sides of a page must key IDENTICALLY ─────────────────────────────

def test_dunning_helper_does_not_out_match_its_own_chaseable_query(tmp_db):
    """A customer renamed in the master, with no sales history under the new
    name, resolves to NO code — so the dunning page falls to its orphan branch
    and matches on the snapshot name only. An excluded query that ALSO matched
    through `customers.name` would render a section for a customer the list
    above it did not recognise. Both sides must see the same customer or neither.
    """
    _seed(tmp_db)
    conn = sqlite3.connect(tmp_db)
    try:
        # The master calls this code NEW_NAME; the snapshot still says NAME, and
        # nothing outside the snapshot knows NEW_NAME at all.
        conn.execute("DELETE FROM customers WHERE code = ?", (CODE,))
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)",
                     (CODE, 'ทดสอบ ชื่อใหม่ในทะเบียน'))
        conn.commit()
    finally:
        conn.close()

    chaseable = ar_followup.get_customer_ar_detail('ทดสอบ ชื่อใหม่ในทะเบียน', db_path=tmp_db)
    excluded = ar_followup.get_customer_excluded_docs('ทดสอบ ชื่อใหม่ในทะเบียน', db_path=tmp_db)

    # Control: the snapshot name still finds the customer through BOTH helpers,
    # so an "everything is empty" bug cannot make this test pass.
    assert len(ar_followup.get_customer_ar_detail(NAME, db_path=tmp_db)) == 1
    assert len(ar_followup.get_customer_excluded_docs(NAME, db_path=tmp_db)) == len(EXPECTED)

    assert chaseable == [], 'fixture wrong — the chaseable side was supposed to miss'
    assert excluded == [], (
        'the excluded side matched a customer the chaseable side did not — '
        'the two are keyed differently')


# ── links that would 404 are not rendered as links ───────────────────────────

def test_only_writeoff_docs_are_linked_to_the_sales_document(tmp_db):
    """RE rows are receipts (never in sales_transactions) and pre-2024 rows
    predate the Sendy ledger, so /sales/doc 404s for both — verified against the
    running app. Rendering them as links puts dead links in front of someone
    mid-phone-call. Write-off rows are >=2024 IVs and do resolve."""
    _seed(tmp_db)
    html = _manager_client().get(
        f'/accounting/ar-followup/customer/{CODE}').get_data(as_text=True)

    # Control: the linkable bucket IS linked, so this cannot pass by the section
    # dropping every link.
    assert '/sales/doc/ZZEX-WOFF"' in html, 'write-off doc lost its link'
    for doc in ('ZZEX-ANOM', 'ZZEX-OLD', 'ZZEX-OLDWO'):
        assert f'/sales/doc/{doc}"' not in html, f'{doc} rendered a link that 404s'
        assert doc in html, f'{doc} disappeared entirely instead of losing its link'


# ── a doc can be excluded for one reason and still carry a write-off decision ─

def _row_html(html, doc_no):
    """The single <tr> of the excluded section for this doc.

    ⚠ Matches the ELEMENT `>DOC<`, never `doc_no in frag`: the doc numbers here
    are prefixes of one another (`ZZEX-ANOM` is inside `ZZEX-ANOMWO`), so a
    substring match silently returns the WRONG row and the assertion that
    follows reports on a document the caller never asked about.
    """
    section = html.split('หนี้ที่ไม่นับว่าตามได้', 1)[1]
    for frag in section.split('<tr class="text-subtle">')[1:]:
        frag = frag.split('</tr>', 1)[0]
        if f'>{doc_no}<' in frag:
            return frag
    return ''


def test_a_doc_that_is_also_written_off_carries_both_labels(tmp_db):
    """`re` and `legacy` win the bucket over `writeoff` so the buckets stay
    disjoint — which on real data (all 11 of จึงเจริญ's rows) leaves a หมายเหตุ
    reading 'AR write-off' beside a badge saying the customer already paid.
    Both facts are true, so the row says both (Put, 2026-09-09)."""
    _seed(tmp_db)
    html = _manager_client().get(
        f'/accounting/ar-followup/customer/{CODE}').get_data(as_text=True)

    anomwo = _row_html(html, 'ZZEX-ANOMWO')
    assert anomwo, 'ZZEX-ANOMWO is not on the page at all'
    assert '>ลูกค้าจ่ายแล้ว (RE)<' in anomwo, 'lost its bucket badge'
    assert '>มีบันทึกตัดหนี้<' in anomwo, 'write-off decision not disclosed'

    oldwo = _row_html(html, 'ZZEX-OLDWO')
    assert '>หนี้ก่อนปี 2024<' in oldwo
    assert '>มีบันทึกตัดหนี้<' in oldwo


def test_the_second_badge_appears_only_where_a_write_off_decision_exists(tmp_db):
    """Control for the test above: the badge must NOT spray onto every row.
    A row with no write-off record does not get it, and the write-off bucket
    does not get it either — there it would only repeat its own badge."""
    _seed(tmp_db)
    html = _manager_client().get(
        f'/accounting/ar-followup/customer/{CODE}').get_data(as_text=True)

    # Exactly the two rows that have a write-off record on a non-writeoff bucket.
    assert html.count('>มีบันทึกตัดหนี้<') == 2, 'the badge leaked onto other rows'

    for doc in ('ZZEX-ANOM', 'ZZEX-OLD'):
        frag = _row_html(html, doc)
        assert frag, f'{doc} missing from the page'
        assert '>มีบันทึกตัดหนี้<' not in frag, f'{doc} has no write-off record'
    assert '>มีบันทึกตัดหนี้<' not in _row_html(html, 'ZZEX-WOFF'), (
        'the writeoff bucket already says so in its own badge')


# ═════════════════════════════════════════════════════════════════════════════
# The three remaining per-customer surfaces (#470 /express/ar, #471 /customer,
# #472 /m/customer). The dunning page above was #469; these consume the same
# seam, so what is new here is the WIRING and the keying, not the query.
# ═════════════════════════════════════════════════════════════════════════════

def _seed_master(db_path):
    """Give the fixture customers a `customers` master row.

    `/customer/code/<code>` 404s a code with neither a master row nor sales, and
    `/m/customer/<name>` looks the customer up by `customers.name` — so without
    this the render tests below would exercise the 404 path and pass for the
    wrong reason. Deliberately NOT folded into `_seed`: the seam tests above
    assert `row['customer'] == NAME` through the snapshot's own name, and adding
    a master row there would change what they are testing.
    """
    conn = sqlite3.connect(db_path)
    try:
        for code, name in ((CODE, NAME), (CLEAN_CODE, CLEAN_NAME),
                           (ONLY_EXCL_CODE, ONLY_EXCL_NAME), (PAD_CODE, PAD_NAME)):
            conn.execute("DELETE FROM customers WHERE code = ? OR name = ?", (code, name))
            conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (code, name))
        conn.commit()
    finally:
        conn.close()


# ── #471 customer summary — /customer/code/<code> ───────────────────────────

def test_customer_summary_lists_the_excluded_docs_below_its_bills(tmp_db):
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(f'/customer/code/{CODE}').get_data(as_text=True)

    # Control FIRST: the chaseable bill this page has always shown is still there.
    assert CONTROL_DOC in html, 'page did not render its own bill list — nothing below means anything'

    assert '>หนี้ที่ไม่นับว่าตามได้<' in html
    for doc in EXPECTED:
        assert doc in html, f'{doc} was removed from the total and never disclosed'
    assert '>ลูกค้าจ่ายแล้ว (RE)<' in html
    assert '>ตัดหนี้สูญ<' in html


def test_customer_summary_states_what_was_removed_from_its_total(tmp_db):
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(f'/customer/code/{CODE}').get_data(as_text=True)
    # 6 excluded docs; 2000 - 500 + 3000 + 4000 + 5000 + 1500 = 15,000.00
    assert 'ตัด 6 ใบ' in html
    assert '฿15,000.00' in html


def test_customer_summary_omits_the_section_when_nothing_was_excluded(tmp_db):
    """The control for the two tests above."""
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(f'/customer/code/{CLEAN_CODE}').get_data(as_text=True)

    assert 'ZZEX-ONLYCLEAN' in html, 'page did not render the clean customer at all'
    assert '>หนี้ที่ไม่นับว่าตามได้<' not in html
    assert '>ตัดหนี้สูญ<' not in html


def test_customer_summary_shows_the_section_even_with_no_chaseable_bills(tmp_db):
    """The bill card is wrapped in `{% if unpaid_bills %}`. A customer whose every
    bill is excluded has none, so a section nested inside that card would vanish
    for exactly the customer who needs the explanation most."""
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(f'/customer/code/{ONLY_EXCL_CODE}').get_data(as_text=True)

    assert '>หนี้ที่ไม่นับว่าตามได้<' in html
    assert 'ZZEX-ALLGONE' in html
    assert '>ลูกค้าจ่ายแล้ว (RE)<' in html


def test_customer_summary_keys_both_of_its_lists_the_same_way(tmp_db):
    """`_unpaid_bills` matched `ao.customer_code = ?` while the excluded wrapper
    TRIMs. A code stored with stray whitespace therefore landed on one list and
    not the other — ADR 0012's defect in miniature, one page instead of two."""
    _seed(tmp_db)
    chaseable, _ = models.get_customer_unpaid_bills_by_code(PAD_CODE)
    excluded, _ = cashflow.bsn_ar_excluded_docs_by_code(PAD_CODE, db_path=tmp_db)

    assert [r['doc_base'] for r in chaseable] == ['ZZEX-PAD-OK'], (
        'the chaseable side did not match a code stored with a trailing space')
    assert [r['doc_no'] for r in excluded] == ['ZZEX-PAD-RE']


def test_customer_summary_lists_partition_the_positive_snapshot_rows(tmp_db):
    """chaseable ∪ excluded == every row this customer has, disjoint.

    ⚠ Scoped to `outstanding_amount > 0`, because `_unpaid_bills` drops credit
    rows on purpose (ADR 0012). This test does NOT pin that clause — every
    chaseable row on this fixture is positive, so the filter never fires here
    and deleting it leaves this green. The test below owns that gap.
    """
    _seed(tmp_db)
    chaseable, _ = models.get_customer_unpaid_bills_by_code(CODE)
    excluded, _ = cashflow.bsn_ar_excluded_docs_by_code(CODE, db_path=tmp_db)

    cha = {r['doc_base'] for r in chaseable}
    exc_positive = {r['doc_no'] for r in excluded if float(r['outstanding']) > 0}
    # Control: both sides non-empty, or "disjoint" and "union" are free.
    assert cha and exc_positive

    assert cha & exc_positive == set(), f'counted twice: {cha & exc_positive}'
    assert cha | exc_positive == {d for d, v in _SEEDED.items() if v[2] > 0}

    # The credit row is on the excluded list and in neither positive set — the
    # documented gap, pinned so a future change to either filter shows up here.
    assert 'ZZEX-WBACK' in {r['doc_no'] for r in excluded}
    assert 'ZZEX-WBACK' not in cha


def test_the_only_gap_between_this_pages_two_lists_is_a_credit_row(tmp_db):
    """`_unpaid_bills` keeps `outstanding_amount > 0` deliberately (ADR 0012 — a
    per-customer bill list should not render a credit) while the excluded helper
    does not filter on amount at all. A CHASEABLE credit row therefore appears on
    NEITHER list: ~3 rows / −฿346 on the prod snapshot. That is the one
    legitimate hole in this page's partition, and it is pinned here so a change
    to either filter shows up as a failure instead of quietly widening it.

    ⚠ This customer exists because the `> 0` clause never fires on the CODE
    fixture — every chaseable row there is positive — so the partition test
    above stayed green with the clause deleted. Verified by mutation 2026-09-09.
    """
    _seed(tmp_db)
    chaseable, _ = models.get_customer_unpaid_bills_by_code(CREDIT_CODE)
    excluded, _ = cashflow.bsn_ar_excluded_docs_by_code(CREDIT_CODE, db_path=tmp_db)

    cha = {r['doc_base'] for r in chaseable}
    exc = {r['doc_no'] for r in excluded}
    # Control: both sides found the customer, so the set arithmetic below has a
    # subject. The chaseable side must hold the positive bill and NOT the credit.
    assert cha == {'ZZEX-CR-BILL'}, cha
    assert exc == {'ZZEX-CR-RE'}, exc

    all_rows = {'ZZEX-CR-BILL', 'ZZEX-CR-CREDIT', 'ZZEX-CR-RE'}
    assert cha & exc == set()
    assert all_rows - (cha | exc) == {'ZZEX-CR-CREDIT'}, (
        'the credit row is the ONLY document this page shows on neither list')


# ── #470 Express AR drill-down — /express/ar/customer/<code> ─────────────────

def test_express_ar_page_lists_the_excluded_docs(tmp_db):
    _seed(tmp_db)
    html = _manager_client().get(
        f'/express/ar/customer/{CODE}').get_data(as_text=True)

    assert CONTROL_DOC in html, 'page did not render its chaseable list'
    assert '>หนี้ที่ไม่นับว่าตามได้<' in html
    for doc in EXPECTED:
        assert doc in html, f'{doc} was removed from the total and never disclosed'
    assert 'ตัด 6 ใบ' in html
    assert '฿15,000.00' in html


def test_express_ar_page_omits_the_section_when_nothing_was_excluded(tmp_db):
    _seed(tmp_db)
    html = _manager_client().get(
        f'/express/ar/customer/{CLEAN_CODE}').get_data(as_text=True)

    assert 'ZZEX-ONLYCLEAN' in html, 'page did not render the clean customer at all'
    assert '>หนี้ที่ไม่นับว่าตามได้<' not in html


def test_express_ar_page_renders_a_customer_whose_every_bill_is_excluded(tmp_db):
    """The headline fix. This customer exists, is in the snapshot, and has debt
    on the books — the page used to flash "ไม่พบลูกหนี้รหัส X" and redirect."""
    _seed(tmp_db)
    resp = _manager_client().get(f'/express/ar/customer/{ONLY_EXCL_CODE}')
    assert resp.status_code == 200, 'a real customer was still told they do not exist'

    html = resp.get_data(as_text=True)
    assert '>ไม่มีหนี้ที่ต้องตาม<' in html, 'the chaseable table has no empty state'
    assert '>หนี้ที่ไม่นับว่าตามได้<' in html
    assert 'ZZEX-ALLGONE' in html
    # Identity must come off the excluded rows, not off an empty chaseable list.
    assert ONLY_EXCL_NAME in html, 'the page could not name the customer'


def test_express_ar_page_still_redirects_a_code_with_no_snapshot_rows(tmp_db):
    """A genuine typo is still caught — the flash-and-redirect fires only when
    BOTH lists are empty."""
    _seed(tmp_db)
    client = _manager_client()
    resp = client.get('/express/ar/customer/ZZNOSUCHCODE')
    assert resp.status_code == 302
    assert '/express/ar' in resp.headers['Location']

    # Control: the route is not redirecting everything.
    assert client.get(f'/express/ar/customer/{ONLY_EXCL_CODE}').status_code == 200


def test_express_ar_page_keys_both_of_its_lists_the_same_way(tmp_db):
    """The route's inline chaseable query matched `customer_code = ?` while the
    excluded wrapper TRIMs. Both lists must see the same customer."""
    _seed(tmp_db)
    html = _manager_client().get(
        f'/express/ar/customer/{PAD_CODE}').get_data(as_text=True)

    assert 'ZZEX-PAD-OK' in html, 'the chaseable side missed a code stored padded'
    assert 'ZZEX-PAD-RE' in html, 'the excluded side missed it'
    assert '>หนี้ที่ไม่นับว่าตามได้<' in html


# ── #472 mobile customer page — /m/customer/<name> ──────────────────────────

def test_mobile_customer_page_notes_what_was_removed_in_one_line(tmp_db):
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(f'/m/customer/{NAME}').get_data(as_text=True)

    # Control: the page rendered this customer's bills at all.
    assert CONTROL_DOC in html, 'mobile page did not render its bill list'
    assert '>ตัด 6 ใบ ฿15,000.00<' in html
    assert 'ตามได้' in html


def test_mobile_customer_page_does_not_render_the_desktop_table(tmp_db):
    """A phone screen on a sales trip. The count is the deliverable; the table
    is explicitly out of scope for this surface."""
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(f'/m/customer/{NAME}').get_data(as_text=True)

    # Control: the note IS there, so this cannot pass by the wiring being absent.
    assert '>ตัด 6 ใบ ฿15,000.00<' in html
    assert '>หนี้ที่ไม่นับว่าตามได้<' not in html
    assert '>ตัดหนี้สูญ<' not in html
    assert 'ZZEX-WOFF' not in html


def test_mobile_customer_page_omits_the_note_when_nothing_was_excluded(tmp_db):
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(f'/m/customer/{CLEAN_NAME}').get_data(as_text=True)

    assert 'ZZEX-ONLYCLEAN' in html, 'page did not render the clean customer at all'
    assert 'ตัด 1 ใบ' not in html
    assert 'ยอดค้างด้านบนคือยอด' not in html
