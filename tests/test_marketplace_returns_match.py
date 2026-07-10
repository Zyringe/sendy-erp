"""TDD for bucket-3 of the 2026-07-10 /grilling session: settled cancel/return
orders should ALSO be linked to their Express document when one exists (Put's
original ask #2). Before this, marketplace_match.py deliberately excluded the
WHOLE cancel/return family from the pool forever (see _is_matchable_status) —
correct for the ~95% that never got invoiced at all (cancelled before
shipping), but wrong for the handful that DID settle (shipped, then returned/
lost, with a real Express document — an SR credit-note for a genuine return,
or occasionally just the original IV when no separate SR was ever keyed, e.g.
a lost-in-transit compensation).

This is a SEPARATE pass from the main automatch pool, run only for settled
cancel/return orders, searching BOTH 'IV%' and 'SR%' docs (the main pool only
ever touches 'IV%', so there is no double-claim risk from widening here).
No amount signal is used at all — a return's actual_payout can be positive,
negative, or zero with no consistent relationship to a doc's net (verified on
the real 6 currently-qualifying orders: -37.9, -5.93, 0.0, +671.4, +71.3,
-43.46) — so matching relies on product identity only: single<->single
product overlap is trusted (same D12 logic as the main matcher), multi-item
requires an EXACT product-set match (no partial-overlap + amount-band
fallback, since there is no trustworthy amount to corroborate with). No
amount-only guessing at all for this category — if no product-matched edge
exists, the order stays unmatched, and that is NOT counted in the main
'unmatched' stat (unlike the main pool, being unmatched is the EXPECTED,
correct outcome for most cancel/return orders, not a problem to flag).

Fixture pattern matches test_marketplace_matchable_gate.py: deletes real
orders/links/marketplace sales rows from the tmp clone, seeds synthetic data.
product_ids 350/351/352 reused across tests in THIS file only (distinct from
300/301/302 in test_marketplace_matchable_gate.py and 440/458/500/686/687/
777/999 in test_marketplace_match.py) so no file needs to coordinate with
another.
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
    c.commit()
    return c


def _add_order(c, order_sn, order_date, status, platform='shopee',
                actual_payout=None, settled_at=None, product_ids=None):
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, actual_payout, settled_at, order_date, currency)
           VALUES (?,?,?,?,?,?, 'THB')""",
        (platform, order_sn, status, actual_payout, settled_at, order_date + ' 10:00'))
    for pid in (product_ids or []):
        c.execute(
            """INSERT INTO marketplace_order_items
               (order_id, platform, order_sn, line_key, internal_product_id, qty)
               VALUES (?,?,?,?,?,1)""",
            (cur.lastrowid, platform, order_sn, str(pid), pid))
    c.commit()


def _add_doc(c, doc_base, date_iso, customer_code='Zหน้าร้าน', product_ids=None, vat_type=1):
    """product_ids: list of (product_id, net) tuples, one sales_transactions
    row each, all sharing doc_base — mirrors a real multi-line Express doc."""
    for i, (pid, net) in enumerate(product_ids or [], start=1):
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
                vat_type, total, net, product_id, created_at, synced_to_stock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, '2026-06-01 00:00:00', 1)""",
            (date_iso, f"{doc_base}-{i}", doc_base, 'หน้าร้านS', customer_code,
             1, net, vat_type, net, net, pid))
    c.commit()


def test_settled_returned_order_links_to_sr_doc(mm_conn):
    """The core case: a settled 'returned' order with a real SR (credit-note)
    document sharing its product, within the (wider, return-specific) window."""
    c = mm_conn
    _add_order(c, 'O-RETURNED-SR', '2026-06-04', 'returned',
               actual_payout=-10.0, settled_at='2026-06-05', product_ids=[350])
    _add_doc(c, 'SR9000001', '2026-06-12', product_ids=[(350, 99.0)])  # 8 days after
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base, match_method, confidence FROM marketplace_order_invoice "
        "WHERE order_sn='O-RETURNED-SR'").fetchone()
    assert row is not None
    assert row['doc_base'] == 'SR9000001'
    assert row['match_method'] == 'auto' and row['confidence'] == 'confident'
    assert stats['returns_matched'] == 1


def test_settled_returned_order_links_to_iv_when_no_sr_exists(mm_conn):
    """No SR was ever keyed for this one (e.g. a lost-in-transit compensation,
    not a real return) — falls back to the original IV when it's the only
    Express document sharing the product."""
    c = mm_conn
    _add_order(c, 'O-LOST-IV', '2026-06-04', 'Lost by 3PL',
               actual_payout=88.0, settled_at='2026-06-05', product_ids=[351])
    _add_doc(c, 'IV9000010', '2026-06-04', product_ids=[(351, 100.0)])
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-LOST-IV'").fetchone()
    assert row['doc_base'] == 'IV9000010'
    assert stats['returns_matched'] == 1


def test_unsettled_cancel_return_order_still_excluded(mm_conn):
    """Regression: an UNSETTLED cancel/return order must stay completely out
    of the pool, unchanged from #280/#281 — this bucket-3 pass only ever
    considers SETTLED cancel/return orders."""
    c = mm_conn
    _add_order(c, 'O-CANCELLED-UNSETTLED', '2026-06-04', 'ยกเลิกแล้ว',
               actual_payout=None, settled_at=None, product_ids=[350])
    _add_doc(c, 'SR9000002', '2026-06-05', product_ids=[(350, 40.0)])
    stats = mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-CANCELLED-UNSETTLED'"
    ).fetchone()[0] == 0
    assert stats.get('returns_matched', 0) == 0


def test_settled_return_with_no_candidate_is_not_counted_as_unmatched(mm_conn):
    """Most settled cancel/return orders correctly have NO Express document at
    all — that is the expected outcome, not a problem. It must not inflate the
    main 'unmatched' stat (which flags orders that SHOULD have matched)."""
    c = mm_conn
    _add_order(c, 'O-RETURNED-NOCANDIDATE', '2026-06-04', 'returned',
               actual_payout=-5.0, settled_at='2026-06-05', product_ids=[352])
    stats = mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-RETURNED-NOCANDIDATE'"
    ).fetchone()[0] == 0
    assert stats['unmatched'] == 0          # not counted here
    assert stats['returns_matched'] == 0


def test_multi_item_return_requires_exact_product_set_match(mm_conn):
    """No amount signal is trustworthy for returns, so a multi-item return
    needs an EXACT product-set match — a doc sharing only ONE of two products
    must NOT match (no partial-overlap + amount-band fallback here)."""
    c = mm_conn
    _add_order(c, 'O-MULTI-PARTIAL', '2026-06-04', 'returned',
               actual_payout=0.0, settled_at='2026-06-05', product_ids=[350, 351])
    _add_doc(c, 'SR9000003', '2026-06-06', product_ids=[(350, 50.0)])  # only 1 of 2
    stats = mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-MULTI-PARTIAL'"
    ).fetchone()[0] == 0
    assert stats['returns_matched'] == 0


def test_multi_item_return_matches_exact_product_set(mm_conn):
    c = mm_conn
    _add_order(c, 'O-MULTI-EXACT', '2026-06-04', 'returned',
               actual_payout=0.0, settled_at='2026-06-05', product_ids=[350, 351])
    _add_doc(c, 'SR9000004', '2026-06-06', product_ids=[(350, 50.0), (351, 30.0)])
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-MULTI-EXACT'").fetchone()
    assert row['doc_base'] == 'SR9000004'
    assert stats['returns_matched'] == 1


def test_return_never_steals_iv_already_claimed_by_a_regular_order(mm_conn):
    """A regular (matchable-status) order's claimed IV must never be stolen by
    a settled-return order that also happens to share the product."""
    c = mm_conn
    _add_order(c, 'O-REGULAR', '2026-06-03', 'สำเร็จแล้ว',
               actual_payout=60.0, settled_at='2026-06-04', product_ids=[350])
    _add_order(c, 'O-RETURNED-COMPETING', '2026-06-04', 'returned',
               actual_payout=-5.0, settled_at='2026-06-05', product_ids=[350])
    _add_doc(c, 'IV9000020', '2026-06-04', product_ids=[(350, 60.0)])  # only one doc exists
    stats = mm.run_automatch(c, 'shopee')
    regular_link = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-REGULAR'").fetchone()
    returned_link = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-RETURNED-COMPETING'").fetchone()
    assert regular_link['doc_base'] == 'IV9000020'
    assert returned_link is None            # the only doc was already claimed
    assert stats['returns_matched'] == 0


def test_return_never_amount_only_guesses(mm_conn):
    """Even an exact-amount coincidence must NOT link a return order when
    there is no product overlap at all — amount alone is never trusted here,
    stricter than the main pool's D13 amount-only-guess allowance."""
    c = mm_conn
    _add_order(c, 'O-RETURNED-NOPROD', '2026-06-04', 'returned',
               actual_payout=None, settled_at='2026-06-05', product_ids=None)
    _add_doc(c, 'SR9000005', '2026-06-06', product_ids=[(999, 77.0)])
    # NB: the order itself resolves to no product at all (product_ids=None)
    stats = mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-RETURNED-NOPROD'"
    ).fetchone()[0] == 0
    assert stats['returns_matched'] == 0


def test_return_processing_lag_wider_than_normal_forward_window(mm_conn):
    """Real data shows genuine SR matches up to ~9 days after the order date
    (beyond FORWARD_WINDOW_DAYS=7 used by the main pool) — this pass uses a
    wider window so those aren't missed."""
    c = mm_conn
    _add_order(c, 'O-RETURNED-LAG', '2026-06-04', 'returned',
               actual_payout=-2.0, settled_at='2026-06-20', product_ids=[350])
    _add_doc(c, 'SR9000006', '2026-06-13', product_ids=[(350, 40.0)])  # 9 days after
    stats = mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-RETURNED-LAG'").fetchone()
    assert row['doc_base'] == 'SR9000006'
    assert stats['returns_matched'] == 1
