"""TDD for the read-only IV-match worklist diagnostic (build-phase 1 of the
marketplace-iv-matching plan). See projects/marketplace-iv-matching/worklist-spec.md
and plan.md §2a for the three failure modes this page surfaces.

Fixture builds ONE shopee order per bucket (A/B/C/D) + one product-verified OK
order + one cancelled order, on a tmp clone of the live DB, and asserts
get_iv_match_worklist classifies each correctly and EXCLUDES the OK + cancelled
ones. This is the load-bearing test — the classifier is real business logic
(mirrors marketplace_match.py's OP/IVP overlap decision), not a UI tweak.

Writes NOTHING — the function under test is pure read (no matcher change).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def worklist_conn(tmp_db_conn):
    """One shopee order per bucket, under a unique 'WLBUCKET*' order_sn / 'IV93*'
    doc_base prefix so assertions never collide with the cloned live data (which
    already has real bucket-A/B/C/D rows from prod-mirrored history)."""
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders WHERE order_sn LIKE 'WLBUCKET%'")
    c.execute("DELETE FROM sales_transactions WHERE doc_base LIKE 'IV93%'")
    c.execute("DELETE FROM marketplace_wallet_txns WHERE order_sn LIKE 'WLBUCKET%'")
    c.execute("DELETE FROM marketplace_order_invoice WHERE order_sn LIKE 'WLBUCKET%'")
    c.execute("DELETE FROM product_generic_standins WHERE note LIKE 'WL test%'")

    # Two distinct products so bucket C's cross-product mismatch is genuine:
    # X = the order's mapped product, Y = the (wrongly) linked IV's product.
    px = c.execute("INSERT INTO products (product_name) VALUES ('WL Test Product X')").lastrowid
    py = c.execute("INSERT INTO products (product_name) VALUES ('WL Test Product Y')").lastrowid

    def _order(order_sn, status, actual_payout, settled_at, order_date, payout_id=None):
        cur = c.execute(
            """INSERT INTO marketplace_orders
               (platform, order_sn, status, order_date, actual_payout, settled_at, payout_id, currency)
               VALUES ('shopee', ?, ?, ?, ?, ?, ?, 'THB')""",
            (order_sn, status, order_date, actual_payout, settled_at, payout_id))
        return cur.lastrowid

    def _item(order_id, order_sn, internal_product_id):
        c.execute(
            """INSERT INTO marketplace_order_items
               (order_id, platform, order_sn, line_key, item_name, qty, internal_product_id)
               VALUES (?, 'shopee', ?, 'L1', 'test item', 1, ?)""",
            (order_id, order_sn, internal_product_id))

    def _iv(doc_base, product_id, amount, date_iso):
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, product_id, customer, customer_code,
                qty, unit_price, vat_type, total, net, synced_to_stock)
               VALUES (?, ?, ?, ?, 'หน้าร้านS', 'Zหน้าร้าน', 1, ?, 1, ?, ?, 1)""",
            (date_iso, doc_base + '-1', doc_base, product_id, amount, amount, amount))

    def _link(order_sn, doc_base, confidence='review'):
        c.execute(
            """INSERT INTO marketplace_order_invoice
               (platform, order_sn, doc_base, customer_code, match_method, confidence)
               VALUES ('shopee', ?, ?, 'Zหน้าร้าน', 'auto', ?)""",
            (order_sn, doc_base, confidence))

    # A — money likely arrived (status สำเร็จแล้ว) but the Income/Settlement file
    # was never imported, so actual_payout is still NULL. A distant sentinel
    # period (2099-01) keeps this out of any real period bucket.
    oa = _order('WLBUCKETA', 'สำเร็จแล้ว', None, None, '2099-01-15 10:00')
    _item(oa, 'WLBUCKETA', px)
    c.execute(
        """INSERT INTO marketplace_wallet_txns (platform, txn_time, txn_type, order_sn, amount)
           VALUES ('shopee', '2099-01-16 00:00', 'income', 'WLBUCKETA', 88.0)""")

    # B — settled, but its only line item was never mapped to a product.
    ob = _order('WLBUCKETB', 'สำเร็จแล้ว', 50.0, '2026-05-11', '2026-05-10 10:00')
    _item(ob, 'WLBUCKETB', None)

    # C — settled + mapped + linked, but the linked IV is a DIFFERENT product
    # (the cross-product "เดายอด" steal, e.g. issue 3 in plan.md).
    oc = _order('WLBUCKETC', 'สำเร็จแล้ว', 60.0, '2026-05-12', '2026-05-11 10:00')
    _item(oc, 'WLBUCKETC', px)
    _iv('IV9300001', py, 60.0, '2026-05-12')
    _link('WLBUCKETC', 'IV9300001')

    # D — settled + mapped, but no IV linked at all (IV shortage, e.g. issue 2).
    od = _order('WLBUCKETD', 'สำเร็จแล้ว', 70.0, '2026-05-13', '2026-05-12 10:00')
    _item(od, 'WLBUCKETD', px)

    # OK — settled + mapped + linked to an IV that SHARES the product. Must be
    # excluded (product-first: trusted regardless of any ฿ gap).
    ook = _order('WLBUCKETOK', 'สำเร็จแล้ว', 80.0, '2026-05-14', '2026-05-13 10:00')
    _item(ook, 'WLBUCKETOK', px)
    _iv('IV9300002', px, 999.0, '2026-05-14')   # amount deliberately differs — must not matter
    _link('WLBUCKETOK', 'IV9300002')

    # Cancelled — would otherwise land in bucket D (settled+mapped+unlinked);
    # must be excluded regardless.
    ocxl = _order('WLBUCKETCANCEL', 'ยกเลิกแล้ว', 90.0, '2026-05-15', '2026-05-14 10:00')
    _item(ocxl, 'WLBUCKETCANCEL', px)

    # COMBO — a ชุด order whose raw item is the PACK pid, linked to an IV the team
    # keyed as the pack's separate COMPONENTS (two lines). The matcher product-
    # matches this via combo expansion (see marketplace_match._combo_components),
    # so the worklist must ALSO expand and treat it as OK — NOT flag bucket C.
    # Regression for the pid-253 false-positives (187 correct Lazada matches
    # mislabelled because the classifier compared the raw pack pid).
    pack = c.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('WL Combo Pack','ชุด')").lastrowid
    cp1 = c.execute("INSERT INTO products (product_name) VALUES ('WL Combo Part 1')").lastrowid
    cp2 = c.execute("INSERT INTO products (product_name) VALUES ('WL Combo Part 2')").lastrowid
    fid = c.execute(
        """INSERT INTO conversion_formulas (name, output_product_id, output_qty, is_active)
           VALUES ('WL combo', ?, 1, 0)""", (pack,)).lastrowid
    c.execute("INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity) VALUES (?,?,1)", (fid, cp1))
    c.execute("INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity) VALUES (?,?,1)", (fid, cp2))
    ocombo = _order('WLBUCKETCOMBO', 'สำเร็จแล้ว', 65.0, '2026-05-16', '2026-05-15 10:00')
    _item(ocombo, 'WLBUCKETCOMBO', pack)
    for i, (pid, amt) in enumerate([(cp1, 30.0), (cp2, 35.0)], 1):
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, product_id, customer, customer_code,
                qty, unit_price, vat_type, total, net, synced_to_stock)
               VALUES ('2026-05-16', ?, 'IV9300003', ?, 'หน้าร้านS', 'Zหน้าร้าน', 1, ?, 1, ?, ?, 1)""",
            ('IV9300003-%d' % i, pid, amt, amt, amt))
    _link('WLBUCKETCOMBO', 'IV9300003')

    # STANDIN — a variant-pid order linked to an IV the team books under the
    # curated generic catch-all (product_generic_standins, mig 134). Pass 1.5
    # product-matches this via stand-in substitution, so the worklist must ALSO
    # substitute and treat it as OK — NOT flag bucket C. Regression for the
    # 2026-07-11 false positives (21 of 22 live bucket-C rows were correct
    # Pass 1.5 links: ลูกรีเวท variants → generic 848, หัวสายชำระ → 908).
    variant = c.execute("INSERT INTO products (product_name) VALUES ('WL Standin Variant')").lastrowid
    generic = c.execute("INSERT INTO products (product_name) VALUES ('WL Standin Generic')").lastrowid
    c.execute(
        "INSERT INTO product_generic_standins (variant_product_id, generic_product_id, note) "
        "VALUES (?, ?, 'WL test standin')", (variant, generic))
    ostd = _order('WLBUCKETSTANDIN', 'สำเร็จแล้ว', 57.0, '2026-05-17', '2026-05-16 10:00')
    _item(ostd, 'WLBUCKETSTANDIN', variant)
    _iv('IV9300004', generic, 57.0, '2026-05-17')
    _link('WLBUCKETSTANDIN', 'IV9300004')

    # STANDIN-MISS — same variant-order shape, but linked to an IV whose product
    # is unrelated to both the variant AND its generic: still a real bucket-C
    # row (substitution must not blanket-suppress genuine cross-product steals).
    ostdm = _order('WLBUCKETSTANDINMISS', 'สำเร็จแล้ว', 58.0, '2026-05-18', '2026-05-17 10:00')
    _item(ostdm, 'WLBUCKETSTANDINMISS', variant)
    _iv('IV9300005', py, 58.0, '2026-05-18')
    _link('WLBUCKETSTANDINMISS', 'IV9300005')

    c.commit()
    return c


def _sns(rows):
    return {r['order_sn'] for r in rows}


def test_bucket_a_missing_settlement_summarized_by_period(worklist_conn):
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    row = next(r for r in wl['summary_a'] if r['period'] == '2099-01')
    assert row['count'] == 1
    assert row['wallet_income'] == 88.0
    # Bucket A is a SUMMARY — no per-order rows for it, and it never leaks into B/C/D.
    assert 'WLBUCKETA' not in _sns(wl['rows_b']) | _sns(wl['rows_c']) | _sns(wl['rows_d'])


def test_bucket_b_unmapped_product(worklist_conn):
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    assert 'WLBUCKETB' in _sns(wl['rows_b'])
    assert 'WLBUCKETB' not in _sns(wl['rows_c']) | _sns(wl['rows_d'])


def test_bucket_c_cross_product_guess(worklist_conn):
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    row = next(r for r in wl['rows_c'] if r['order_sn'] == 'WLBUCKETC')
    assert row['doc_base'] == 'IV9300001'
    assert 'WLBUCKETC' not in _sns(wl['rows_b']) | _sns(wl['rows_d'])


def test_bucket_d_no_free_invoice(worklist_conn):
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    assert 'WLBUCKETD' in _sns(wl['rows_d'])
    assert 'WLBUCKETD' not in _sns(wl['rows_b']) | _sns(wl['rows_c'])


def test_ok_and_cancelled_orders_excluded(worklist_conn):
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    all_sns = _sns(wl['rows_b']) | _sns(wl['rows_c']) | _sns(wl['rows_d'])
    period_sns = {r['period'] for r in wl['summary_a']}
    assert 'WLBUCKETOK' not in all_sns
    assert 'WLBUCKETCANCEL' not in all_sns
    assert 'WLBUCKETCANCEL' not in period_sns  # (sanity: not even in the A summary)


def test_badge_count_is_b_plus_c_plus_d(worklist_conn):
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    assert wl['count_bcd'] == len(wl['rows_b']) + len(wl['rows_c']) + len(wl['rows_d'])
    assert wl['count_bcd'] >= 3   # our B + C + D fixture rows, at minimum


def test_combo_order_matched_to_component_iv_excluded(worklist_conn):
    """A ชุด order linked to an IV keyed as its separate components must be a
    product-match via combo expansion, NOT flagged bucket C (the pid-253
    false-positive class)."""
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    all_sns = _sns(wl['rows_b']) | _sns(wl['rows_c']) | _sns(wl['rows_d'])
    assert 'WLBUCKETCOMBO' not in all_sns


def test_standin_order_matched_to_generic_iv_excluded(worklist_conn):
    """A variant-pid order linked to an IV keyed under its curated generic
    stand-in is a product match via substitution (mirrors matcher Pass 1.5 and
    the #288 picker) — NOT bucket C. Regression for 2026-07-11, when 21 of the
    22 live bucket-C rows were correct Pass 1.5 links flagged by the raw-pid
    compare."""
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    all_sns = _sns(wl['rows_b']) | _sns(wl['rows_c']) | _sns(wl['rows_d'])
    assert 'WLBUCKETSTANDIN' not in all_sns


def test_standin_substitution_does_not_blanket_suppress_bucket_c(worklist_conn):
    """The stand-in aware compare must still flag a variant order linked to an
    IV whose product matches NEITHER the variant NOR its generic."""
    import models
    wl = models.get_iv_match_worklist(worklist_conn, 'shopee')
    assert 'WLBUCKETSTANDINMISS' in _sns(wl['rows_c'])
