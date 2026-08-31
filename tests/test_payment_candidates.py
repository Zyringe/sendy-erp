"""find_payment_candidates — "ยอดที่โอนมานี้ เป็นของบิลไหน".

Put types an amount; the matcher answers which customer's outstanding invoices
could sum to it. Two defects motivated this file, both measured on the real dev
DB on 2026-08-31 before the rewrite:

  1. Customers with more than 15 outstanding bills were NEVER subset-searched —
     the old code compared their GRAND TOTAL only and `continue`d. Searching
     ฿1,440 did not surface IV6901291 (หน้าร้านL) even though that single bill
     is exactly ฿1,440. The three customers it silenced (61 / 41 / 24 bills) are
     the ones who actually transfer money.
  2. Tolerance was `max(amount * 5%, 200)`, so a ฿7.77 search returned a
     "candidate" — every bill within ฿200 of anything.

Every fixture here FORCES its own `sales_transactions` population (the tmp_db
fixture clones the live DB *with its data*, so a test that merely inserts what
it needs is sitting on ~20k real rows and 175 real outstanding bills).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import models


def _seed(db_path, bills, customer='ทดสอบ ฮาร์ดแวร์', code='T001', vat_type=0):
    """Replace sales_transactions with exactly `bills` — [(doc_base, net), ...].

    Returns the connection so a test can add more customers before asserting.
    """
    conn = sqlite3.connect(db_path)
    # Throwaway copy: the audit triggers would write 20k rows for the wipe.
    for trg in ('audit_sales_transactions_insert', 'audit_sales_transactions_delete'):
        conn.execute(f'DROP TRIGGER IF EXISTS {trg}')
    conn.execute('DELETE FROM sales_transactions')
    _add(conn, bills, customer, code, vat_type)
    conn.commit()
    return conn


def _add(conn, bills, customer, code, vat_type=0):
    for doc_base, net in bills:
        conn.execute(
            """INSERT INTO sales_transactions
                 (date_iso, doc_no, doc_base, customer, customer_code,
                  qty, unit_price, vat_type, net, created_at, synced_to_stock)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, '2026-01-01', 0)""",
            ('2026-01-01', doc_base, doc_base, customer, code, net, vat_type, net))


@pytest.fixture
def seeded(tmp_db):
    """Three bills, one customer. The baseline population for simple cases."""
    conn = _seed(tmp_db, [('IV001', 1000.0), ('IV002', 2500.0), ('IV003', 340.25)])
    yield tmp_db
    conn.close()


# ── the regression this rewrite exists for ────────────────────────────────────

def test_customer_with_many_bills_is_still_subset_searched(tmp_db):
    """24 outstanding bills — the old code compared the grand total only."""
    bills = [(f'IV9{i:03d}', 100.0 + i) for i in range(24)]   # 100.00 .. 123.00
    conn = _seed(tmp_db, bills)
    conn.close()

    # CONTROL: the customer is reachable at all — a search for the grand total
    # succeeded even under the old code, so if THIS comes back empty the
    # fixture never reached the function and the assertion below proves nothing.
    total = sum(n for _, n in bills)
    assert [c for c in models.find_payment_candidates(total) if c['customer'] == 'ทดสอบ ฮาร์ดแวร์']

    # The actual regression: one single bill out of the 24.
    hits = models.find_payment_candidates(123.0, tolerance=0)
    assert len(hits) == 1
    assert [b['doc_base'] for b in hits[0]['matched_bills']] == ['IV9023']
    assert hits[0]['diff'] == 0


def test_many_bill_customer_matches_a_multi_bill_subset(tmp_db):
    bills = [(f'IV9{i:03d}', 100.0 + i) for i in range(24)]
    conn = _seed(tmp_db, bills)
    conn.close()
    # 122 + 123 = 245 — the ONLY subset that reaches it (the next-best pair is
    # 121 + 123 = 244, and the cheapest triple is 100 + 101 + 102 = 303), so the
    # assertion pins one answer rather than whichever tie the walk-back returns.
    hits = models.find_payment_candidates(245.0, tolerance=0)
    assert len(hits) == 1
    assert sorted(b['doc_base'] for b in hits[0]['matched_bills']) == ['IV9022', 'IV9023']


# ── tolerance ────────────────────────────────────────────────────────────────

def test_small_amount_finds_nothing_when_no_bill_is_near(seeded):
    """The ฿200-floor bug: every bill here is >= ฿340."""
    assert models.find_payment_candidates(7.77) == []
    # CONTROL: the same DB does answer a real amount.
    assert models.find_payment_candidates(1000.0)


def test_tolerance_zero_rejects_a_near_miss(seeded):
    assert models.find_payment_candidates(1005.0, tolerance=0) == []


def test_tolerance_admits_a_near_miss_and_reports_the_difference(seeded):
    hits = models.find_payment_candidates(1005.0, tolerance=10)
    assert [b['doc_base'] for b in hits[0]['matched_bills']] == ['IV001']
    assert hits[0]['diff'] == pytest.approx(-5.0)      # bill is 5 baht SHORT of the transfer


def test_default_tolerance_is_twenty_baht(seeded):
    assert models.find_payment_candidates(1019.0)      # within ฿20
    assert models.find_payment_candidates(1021.0) == []


# ── population ───────────────────────────────────────────────────────────────

def test_bill_with_an_active_receipt_is_excluded(seeded):
    conn = sqlite3.connect(seeded)
    conn.execute("INSERT INTO received_payments (re_no, date_iso, customer, total, cancelled)"
                 " VALUES ('RE001', '2026-01-02', 'ทดสอบ ฮาร์ดแวร์', 1000.0, 0)")
    rid = conn.execute("SELECT id FROM received_payments WHERE re_no='RE001'").fetchone()[0]
    conn.execute("INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount)"
                 " VALUES (?, 'IV001', 'IV', 1000.0)", (rid,))
    conn.commit()
    conn.close()
    assert models.find_payment_candidates(1000.0, tolerance=0) == []


def test_bill_with_a_cancelled_receipt_is_still_outstanding(seeded):
    conn = sqlite3.connect(seeded)
    conn.execute("INSERT INTO received_payments (re_no, date_iso, customer, total, cancelled)"
                 " VALUES ('RE002', '2026-01-02', 'ทดสอบ ฮาร์ดแวร์', 1000.0, 1)")
    rid = conn.execute("SELECT id FROM received_payments WHERE re_no='RE002'").fetchone()[0]
    conn.execute("INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount)"
                 " VALUES (?, 'IV001', 'IV', 1000.0)", (rid,))
    conn.commit()
    conn.close()
    hits = models.find_payment_candidates(1000.0, tolerance=0)
    assert [b['doc_base'] for b in hits[0]['matched_bills']] == ['IV001']


def test_vat_type_2_bills_are_grossed_up_before_matching(tmp_db):
    """vat_type 2 stores net EX-VAT; the customer transfers the VAT-inclusive cash."""
    conn = _seed(tmp_db, [('IV100', 1000.0)], vat_type=2)
    conn.close()
    assert models.find_payment_candidates(1000.0, tolerance=0) == []
    hits = models.find_payment_candidates(1070.0, tolerance=0)
    assert [b['doc_base'] for b in hits[0]['matched_bills']] == ['IV100']


def test_credit_note_and_history_docs_never_appear(tmp_db):
    conn = _seed(tmp_db, [('SR001', 500.0), ('HS001', 500.0), ('IV200', 500.0)])
    conn.close()
    hits = models.find_payment_candidates(500.0, tolerance=0)
    docs = [b['doc_base'] for h in hits for b in h['matched_bills']]
    assert docs == ['IV200']


# ── output contract ──────────────────────────────────────────────────────────

def test_ranked_by_closeness_then_deterministic(tmp_db):
    conn = _seed(tmp_db, [('IV001', 1000.0)], customer='กใกล้', code='C1')
    _add(conn, [('IV002', 995.0)], 'ขไกล', 'C2')
    _add(conn, [('IV003', 1000.0)], 'คเท่ากัน', 'C3')
    conn.commit()
    conn.close()
    hits = models.find_payment_candidates(1000.0, tolerance=10)
    assert [h['customer'] for h in hits] == ['กใกล้', 'คเท่ากัน', 'ขไกล']
    assert models.find_payment_candidates(1000.0, tolerance=10) == hits   # stable


def test_row_shape_is_what_the_page_renders(seeded):
    hit = models.find_payment_candidates(1000.0, tolerance=0)[0]
    assert set(hit) == {'customer', 'customer_code', 'matched_bills', 'matched_sum',
                        'diff', 'total_unpaid_bills', 'total_outstanding', 'match_count'}
    assert hit['match_count'] == 1
    assert hit['customer_code'] == 'T001'
    assert hit['total_unpaid_bills'] == 3
    assert hit['total_outstanding'] == pytest.approx(3840.25)
    assert hit['matched_bills'][0]['vat_type'] == 0


def test_max_results_caps_the_list(tmp_db):
    conn = _seed(tmp_db, [('IV000', 500.0)], customer='ลูกค้า00', code='C00')
    for i in range(1, 12):
        _add(conn, [(f'IV{i:03d}', 500.0)], f'ลูกค้า{i:02d}', f'C{i:02d}')
    conn.commit()
    conn.close()
    assert len(models.find_payment_candidates(500.0, tolerance=0, max_results=5)) == 5


def test_non_positive_amount_returns_nothing(seeded):
    assert models.find_payment_candidates(0) == []
    assert models.find_payment_candidates(-100) == []


# ── /ar?tab=match — the page the matcher lives on ────────────────────────────

def _admin():
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1; sess['username'] = 'admin'; sess['role'] = 'admin'
    return c


def test_match_tab_is_reachable_from_every_other_tab(seeded):
    """The whole reason this was rebuilt: the feature existed but had no door."""
    body = _admin().get('/ar?tab=overview').data.decode()
    assert 'tab=match' in body and 'จับคู่ยอดโอน' in body


def test_match_tab_renders_the_form_with_the_default_tolerance(seeded):
    r = _admin().get('/ar?tab=match')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'name="amount"' in body and 'name="tol"' in body
    assert 'value="20"' in body                       # MATCH_TOLERANCE_BAHT
    assert 'คาดคะเนการชำระเงิน' not in body           # nothing searched yet


def test_match_tab_shows_an_exact_hit(seeded):
    body = _admin().get('/ar?tab=match&amount=1000').data.decode()
    assert 'ตรงเป๊ะ' in body
    assert 'IV001' in body
    assert 'IV002' not in body                        # the ฿2,500 bill is not a match


def test_match_tab_honours_a_custom_tolerance(seeded):
    tight = _admin().get('/ar?tab=match&amount=1005&tol=0').data.decode()
    assert 'ไม่พบชุดบิลค้าง' in tight
    loose = _admin().get('/ar?tab=match&amount=1005&tol=10').data.decode()
    assert 'IV001' in loose and '-5.00' in loose


def test_match_tab_survives_a_non_numeric_amount(seeded):
    r = _admin().get('/ar?tab=match&amount=abc')
    assert r.status_code == 200
    assert 'คาดคะเนการชำระเงิน' not in r.data.decode()


def test_match_tab_reports_no_hit_rather_than_an_empty_table(seeded):
    body = _admin().get('/ar?tab=match&amount=999999').data.decode()
    assert 'ไม่พบชุดบิลค้าง' in body


# ── ambiguity is reported, not hidden ────────────────────────────────────────

def test_a_customer_with_many_ways_to_hit_the_amount_says_so(tmp_db):
    """61 small bills always contain SOME subset summing to anything — an exact
    match there is coincidence, not evidence. Measured on the real book
    (2026-08-31): a ฿1,440 search produced a 9-bill "exact" hit for หน้าร้านS
    and a 10-bill one for หน้าร้านL, both meaningless. The count is what lets
    Put tell those apart from a real single-invoice match."""
    conn = _seed(tmp_db, [(f'IV8{i:03d}', 100.0) for i in range(20)],
                 customer='ซมั่ว', code='N1')                      # 100 x 20
    _add(conn, [('IV7001', 400.0)], 'กชัด', 'N2')                  # one clean bill
    conn.commit()
    conn.close()

    hits = models.find_payment_candidates(400.0, tolerance=0)
    clean = [h for h in hits if h['customer'] == 'กชัด'][0]
    noisy = [h for h in hits if h['customer'] == 'ซมั่ว']

    assert clean['match_count'] == 1
    assert noisy and noisy[0]['match_count'] > 1        # C(20,4) ways to make 400
    # The unambiguous customer outranks the noisy one even though both are exact.
    assert hits[0]['customer'] == 'กชัด'


def test_a_statement_payment_of_many_bills_is_still_found(tmp_db):
    """Beyond the small-combination depth, the shapes a real transfer actually
    takes: everything outstanding, or everything up to a date."""
    bills = [(f'IV6{i:03d}', 111.0) for i in range(9)]     # 9 bills, 999.00 total
    conn = _seed(tmp_db, bills)
    conn.close()
    hits = models.find_payment_candidates(999.0, tolerance=0)
    assert len(hits[0]['matched_bills']) == 9              # whole book paid off

    hits = models.find_payment_candidates(666.0, tolerance=0)
    assert [b['doc_base'] for b in hits[0]['matched_bills']] == [f'IV6{i:03d}' for i in range(6)]


def test_search_stays_fast_on_the_worst_real_customer(tmp_db):
    """61 outstanding bills is the real maximum (หน้าร้านS). Anything on a
    request path has a 60-second gunicorn budget; this must not approach it."""
    import time
    conn = _seed(tmp_db, [(f'IV5{i:03d}', 100.0 + (i % 37)) for i in range(61)])
    conn.close()
    t = time.time()
    models.find_payment_candidates(1440.0)
    assert time.time() - t < 5.0


def test_a_single_matching_bill_is_never_lost_to_the_ambiguity_cap(tmp_db):
    """Found on real data, 2026-08-31: หน้าร้านL holds IV6901291 at exactly
    ฿1,440 and a ฿1,440 search returned four-bill coincidences for that customer
    instead. The walk runs cheapest-bill-first, so with enough small bills it
    hit the match cap long before reaching the one large invoice that IS the
    amount — the cap must bound how much ambiguity is COUNTED, never truncate
    the search before the simplest answers are in hand."""
    # 40 bills of ฿340-379: hundreds of four-bill combinations hit ฿1,440 exactly,
    # and every one of them is cheaper than the single ฿1,440 invoice, so a
    # cheapest-first walk meets them all before it ever reaches the real answer.
    bills = [(f'IV4{i:03d}', 340.0 + i) for i in range(40)]
    bills.append(('IV4999', 1440.0))                               # the real answer
    conn = _seed(tmp_db, bills)
    conn.close()

    hits = [h for h in models.find_payment_candidates(1440.0, tolerance=0)
            if h['customer'] == 'ทดสอบ ฮาร์ดแวร์']
    assert hits, 'the customer vanished entirely'
    assert [b['doc_base'] for b in hits[0]['matched_bills']] == ['IV4999']
    assert hits[0]['match_count'] > 1        # the coincidences are still counted


# ── /scrutinize findings, 2026-08-31 ─────────────────────────────────────────

def test_non_finite_amounts_are_refused_not_crashed(seeded):
    """`?amount=inf` reached round(inf * 100) and 500'd with OverflowError —
    verified live before the fix. float() accepts 'inf'/'nan'/'1e400', so the
    web boundary must reject non-finite values, not just non-numeric ones."""
    for bad in (float('inf'), float('-inf'), float('nan')):
        assert models.find_payment_candidates(bad) == []
    assert models.find_payment_candidates(1000.0, tolerance=float('inf'))

    c = _admin()
    for q in ('inf', '1e400', 'nan', '-inf'):
        assert c.get(f'/ar?tab=match&amount={q}').status_code == 200, q
    assert c.get('/ar?tab=match&amount=1440&tol=inf').status_code == 200


def test_a_huge_book_stays_inside_the_request_budget(tmp_db):
    """Cost is C(n, 4) per customer once the target is large enough that nothing
    prunes. Measured on the bare walk before the fix: 200 bills = 9.8s,
    300 = 51.9s — one customer alone past gunicorn's 60s. Today's largest book
    is 61 bills, so this is a scheduled outage, not a live one: marketplace
    shopfront customers accrue small unpaid invoices and never settle."""
    import time
    bills = [(f'IV3{i:04d}', 1000.0 + i) for i in range(300)]
    bills.append(('IV39999', 200000.0))            # the single-invoice answer
    conn = _seed(tmp_db, bills)
    conn.close()

    t = time.time()
    hits = models.find_payment_candidates(200000.0, tolerance=0)
    elapsed = time.time() - t
    assert elapsed < 5.0, f'took {elapsed:.1f}s'
    # CONTROL: the budget must bound the search WITHOUT losing the simple answer.
    assert [b['doc_base'] for b in hits[0]['matched_bills']] == ['IV39999']


def test_snapshot_staleness_banner_is_not_shown_on_the_match_tab(seeded):
    """The banner speaks for the Express AR snapshot ("ยอดค้างจริงอาจเปลี่ยนไปแล้ว
    ห้ามใช้ทวงหนี้ก่อนนำเข้าใหม่"). This tab's numbers come from the Sendy ledger
    instead, so showing it puts two contradictory provenance claims on one money
    page. Its own header says it belongs on pages serving the authoritative
    collection balance."""
    c = _admin()
    banner = 'นำเข้า AR snapshot ใหม่'
    # CONTROL: the banner really does render in this DB (snapshot is stale) —
    # without this, the assertion below passes for the wrong reason.
    assert banner in c.get('/ar?tab=overview').data.decode()
    assert banner not in c.get('/ar?tab=match').data.decode()


def test_the_searched_amount_is_echoed_cleanly(seeded):
    body = _admin().get('/ar?tab=match&amount=1440').data.decode()
    assert 'value="1440"' in body and 'value="1440.0"' not in body
