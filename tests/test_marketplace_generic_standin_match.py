"""TDD for Pass 1.5 (mig 134 / product_generic_standins), follow-up to the
2026-07-10 /grilling session's bucket 2b. Some color/size-specific products
have real, separately-tracked stock but are booked in Express under one
generic catch-all product instead — see the design doc:
Operations/05_analysis-reports/engineering/generic-standin-schema-design_2026-07-10.md

Pass 1.5 sits between the existing Pass 1 (direct product match) and Pass 2
(amount-only guess): for orders Pass 1 couldn't place, retry with each
product REPLACED by its curated generic stand-in (product_generic_standins),
never unioned in. Replacement (not union) matters: Pass 1 already exhaustively
tried the order's real product id(s) against the full IV pool and failed, so
nothing is lost by dropping it for this pass — and critically, keeping the
substituted set the SAME SIZE as the original preserves _product_compatible's
single<->single "trusted regardless of amount" rule (D12). A naive union
would inflate a single-item order to size 2, wrongly demoting it into the
amount-band-corroboration path.

Fixture pattern matches test_marketplace_returns_match.py: deletes real
orders/links/marketplace sales + generic-standin rows from the tmp clone,
seeds synthetic data. product_ids 360/361/362/363 reused across tests in
THIS file only (distinct from other marketplace_match test files' ranges).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import marketplace_match as mm


@pytest.fixture
def mm_conn(tmp_db_conn):
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders")
    c.execute("DELETE FROM marketplace_order_invoice")
    c.execute("DELETE FROM sales_transactions WHERE customer_code IN ('Zหน้าร้าน','Lหน้าร้าน')")
    c.execute("DELETE FROM product_generic_standins")
    c.commit()
    return c


def _add_standin(c, variant_pid, generic_pid):
    c.execute(
        "INSERT INTO product_generic_standins (variant_product_id, generic_product_id, note) "
        "VALUES (?,?,'test')", (variant_pid, generic_pid))
    c.commit()


def _add_order(c, order_sn, order_date, status='สำเร็จแล้ว', platform='shopee',
                actual_payout=None, item_total=None, product_ids=None):
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, actual_payout, item_total, settled_at, order_date, currency)
           VALUES (?,?,?,?,?,?,?, 'THB')""",
        (platform, order_sn, status, actual_payout, item_total, order_date, order_date + ' 10:00'))
    for pid in (product_ids or []):
        c.execute(
            """INSERT INTO marketplace_order_items
               (order_id, platform, order_sn, line_key, internal_product_id, qty)
               VALUES (?,?,?,?,?,1)""",
            (cur.lastrowid, platform, order_sn, str(pid), pid))
    c.commit()


def _add_doc(c, doc_base, date_iso, customer_code='Zหน้าร้าน', product_ids=None, vat_type=1):
    for i, (pid, net) in enumerate(product_ids or [], start=1):
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
                vat_type, total, net, product_id, created_at, synced_to_stock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, '2026-06-01 00:00:00', 1)""",
            (date_iso, f"{doc_base}-{i}", doc_base, 'หน้าร้านS', customer_code,
             1, net, vat_type, net, net, pid))
    c.commit()


def test_generic_standins_lookup(mm_conn):
    c = mm_conn
    _add_standin(c, 360, 361)
    _add_standin(c, 362, 361)
    lookup = mm._generic_standins(c)
    assert lookup == {360: 361, 362: 361}


def test_order_matches_via_generic_standin_when_no_direct_iv_exists(mm_conn):
    """The core case: order resolves to variant 360, no IV anywhere has 360,
    but a curated stand-in (361) has a real IV within window — matches,
    'confident' (same trust bar as a direct Pass-1 match, per the design)."""
    c = mm_conn
    _add_standin(c, 360, 361)
    _add_order(c, 'O-STANDIN', '2026-06-04', actual_payout=99.0, product_ids=[360])
    _add_doc(c, 'IV9200001', '2026-06-05', product_ids=[(361, 500.0)])  # amount doesn't matter
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base, match_method, confidence FROM marketplace_order_invoice "
        "WHERE order_sn='O-STANDIN'").fetchone()
    assert row is not None
    assert row['doc_base'] == 'IV9200001'
    assert row['match_method'] == 'auto' and row['confidence'] == 'confident'
    assert stats['confident'] >= 1


def test_direct_match_in_pass1_preferred_over_standin(mm_conn):
    """When a real invoice for the ACTUAL product exists, Pass 1 finds it —
    the order never reaches Pass 1.5, even though a standin candidate also
    technically exists nearby."""
    c = mm_conn
    _add_standin(c, 360, 361)
    _add_order(c, 'O-DIRECT', '2026-06-04', actual_payout=50.0, product_ids=[360])
    _add_doc(c, 'IV9200002', '2026-06-05', product_ids=[(360, 50.0)])   # direct match
    _add_doc(c, 'IV9200003', '2026-06-05', product_ids=[(361, 999.0)])  # standin candidate, unused
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-DIRECT'").fetchone()
    assert row['doc_base'] == 'IV9200002'
    # the standin IV must remain free (never claimed) since it was never needed
    other = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE doc_base='IV9200003'").fetchone()
    assert other is None


def test_healthy_sibling_without_standin_row_is_never_diluted(mm_conn):
    """A sibling product in the SAME family that has NO curated standin row
    must never get matched via someone else's generic — only explicitly
    curated pids participate. Regression pinned by db-architect's design doc."""
    c = mm_conn
    _add_standin(c, 360, 361)   # 362 (a "healthy sibling") deliberately has NO standin row
    _add_order(c, 'O-HEALTHY-SIBLING', '2026-06-04', actual_payout=40.0, product_ids=[362])
    _add_doc(c, 'IV9200004', '2026-06-05', product_ids=[(361, 40.0)])  # only the generic exists
    stats = mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-HEALTHY-SIBLING'"
    ).fetchone()[0] == 0


def test_pass_1_5_runs_before_amount_only_guess(mm_conn):
    """A standin-based product match must win 'confident', not fall through to
    Pass 2's amount-only 'review' guess against some unrelated coincidence."""
    c = mm_conn
    _add_standin(c, 360, 361)
    _add_order(c, 'O-BEATS-GUESS', '2026-06-04', actual_payout=77.0, product_ids=[360])
    _add_doc(c, 'IV9200005', '2026-06-05', product_ids=[(361, 500.0)])   # standin match, wrong amount
    _add_doc(c, 'IV9200006', '2026-06-05', product_ids=[(999, 77.0)])    # exact-amount coincidence, no product overlap at all
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='O-BEATS-GUESS'").fetchone()
    assert row['doc_base'] == 'IV9200005'
    assert row['confidence'] == 'confident'


def test_standin_match_never_steals_a_doc_already_claimed(mm_conn):
    """Two orders share the same variant->standin mapping; only ONE real IV
    exists for the generic — only one order gets matched, the other stays
    unmatched (no fabricated second link)."""
    c = mm_conn
    _add_standin(c, 360, 361)
    _add_order(c, 'O-FIRST', '2026-06-04', actual_payout=10.0, product_ids=[360])
    _add_order(c, 'O-SECOND', '2026-06-04', actual_payout=10.0, product_ids=[360])
    _add_doc(c, 'IV9200007', '2026-06-05', product_ids=[(361, 10.0)])  # only one doc
    stats = mm.run_automatch(c, 'shopee')
    linked = c.execute(
        "SELECT order_sn FROM marketplace_order_invoice WHERE doc_base='IV9200007'").fetchall()
    assert len(linked) == 1


def test_multi_item_order_only_the_standin_pid_is_substituted(mm_conn):
    """A 2-item order where only ONE product has a curated standin: the other
    product id is kept as-is in the substituted set (not touched), and the
    resulting pair must still match a doc carrying {generic, other} exactly
    (multi-item still needs product-set equality or amount corroboration —
    unchanged _product_compatible semantics, just fed a substituted set)."""
    c = mm_conn
    _add_standin(c, 360, 361)   # 362 has no standin, stays 362
    _add_order(c, 'O-MULTI', '2026-06-04', actual_payout=0.0, product_ids=[360, 362])
    _add_doc(c, 'IV9200008', '2026-06-05', product_ids=[(361, 20.0), (362, 20.0)])
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='O-MULTI'").fetchone()
    assert row['doc_base'] == 'IV9200008'
    assert row['confidence'] == 'confident'


def test_no_standin_candidate_stays_unmatched_not_a_review_guess(mm_conn):
    """An order whose product has no curated standin, and no direct IV
    either, must simply stay unmatched — Pass 1.5 must not invent anything."""
    c = mm_conn
    _add_order(c, 'O-NOTHING', '2026-06-04', actual_payout=15.0, product_ids=[363])
    stats = mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-NOTHING'"
    ).fetchone()[0] == 0
    assert stats['unmatched'] >= 1
