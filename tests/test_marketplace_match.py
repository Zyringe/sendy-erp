"""Tests for marketplace_match — linking marketplace orders to Express IVs.

The matcher links a settled Shopee/Lazada order to the Express invoice the team
keyed shortly AFTER the platform order, via a GLOBAL product-first/nearest-date
assignment (see marketplace_match module docstring + matcher-rebuild-spec.md).
'confident' = a product-compatible + date-valid match, regardless of the ฿ gap
(D12) — amount is a tiebreaker only, never a confidence gate. 'review' = an
amount-only guess with no product corroboration (D13) — never 'confident', even
when the ฿ matches exactly (that used to be the bug: see
test_exact_amount_confident / test_vat_type2_amount_matches_payout below, which
were updated for the rebuild, not left pinning the old amount-blind-confidence
flaw the rebuild fixes).

Fixture isolates the matcher: it deletes every real order (cascades to their
items) and the real Zหน้าร้าน/Lหน้าร้าน sales rows from the tmp clone, then seeds
synthetic data. Deleting (not just un-settling) is required since the matcher's
gate is STATUS-based, not settled-only (2026-07-10) — a merely un-settled real
order can still be status-matchable and pollute the pool.
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


def _add_order(c, order_sn, payout, order_date, platform='shopee', product_id=None):
    """Settled order; order_date is the matcher anchor (settled_at only needs to be
    non-null so the order counts as settled)."""
    settled_at = order_date
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, actual_payout, settled_at, order_date, currency)
           VALUES (?,?, 'สำเร็จแล้ว', ?, ?, ?, 'THB')""",
        (platform, order_sn, payout, settled_at, order_date + ' 10:00'))
    if product_id is not None:
        c.execute(
            """INSERT INTO marketplace_order_items
               (order_id, platform, order_sn, line_key, internal_product_id, qty)
               VALUES (?,?,?,?,?,1)""",
            (cur.lastrowid, platform, order_sn, str(product_id), product_id))
    c.commit()


def _add_iv(c, doc_base, net, date_iso, customer_code='Zหน้าร้าน', vat_type=1, lines=1, product_id=None):
    for i in range(1, lines + 1):
        line_net = net if i == 1 else 0.0
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
                vat_type, total, net, product_id, created_at, synced_to_stock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, '2026-06-01 00:00:00', 1)""",
            (date_iso, f"{doc_base}-{i}", doc_base, 'หน้าร้านS', customer_code,
             1, line_net, vat_type, line_net, line_net, product_id))
    c.commit()


def test_exact_amount_no_product_is_review_guess(mm_conn):
    """REBUILD CHANGE (D13): neither side has a product, so this is an amount-only
    guess — it still auto-links (exact ฿ is a strong signal), but as 'review', never
    'confident'. Pre-rebuild this was the "amount-blind confidence" bug (plan.md
    §2/§3 D2): an exact-฿ amount-only coincidence used to be stamped 'confident'
    with no product check at all — exactly the class of silent wrong-match the
    rebuild's D12/D13 split fixes. See test_amount_differs_is_review below for the
    real 'confident' case (product-matched, amount doesn't even need to agree)."""
    c = mm_conn
    _add_order(c, 'O-UNIQ', 99.0, '2026-06-04')
    _add_iv(c, 'IV9000001', 99.0, '2026-06-05')           # 1 day after, amount matches
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['review'] == 1 and stats['confident'] == 0
    row = c.execute(
        "SELECT doc_base, match_method, confidence FROM marketplace_order_invoice WHERE order_sn='O-UNIQ'"
    ).fetchone()
    assert row['doc_base'] == 'IV9000001'
    assert row['match_method'] == 'auto' and row['confidence'] == 'review'


def test_iv_before_order_is_excluded(mm_conn):
    """An invoice dated BEFORE the platform order can't be that order's invoice."""
    c = mm_conn
    _add_order(c, 'O-AFTER', 50.0, '2026-06-10')
    _add_iv(c, 'IV9000002', 50.0, '2026-06-03')           # 7 days BEFORE the order
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_product_overlap_beats_nearer_date(mm_conn):
    """A product-matching invoice wins over a date-nearer one with no shared product
    (the 246/236 same-amount-sibling case)."""
    c = mm_conn
    _add_order(c, 'O-PROD', 50.0, '2026-06-04', product_id=999)
    _add_iv(c, 'IV_NEAR', 50.0, '2026-06-04')                       # gap 0, no product
    _add_iv(c, 'IV_PROD', 50.0, '2026-06-05', product_id=999)       # gap 1, shares product 999
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1
    assert c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-PROD'"
    ).fetchone()['doc_base'] == 'IV_PROD'


def test_amount_differs_is_review(mm_conn):
    """REBUILD CHANGE (D12/D6): product confirms the invoice, so it's trusted
    ('confident') even though the amount differs (Shopee adjusted the payout) —
    the ฿ gap is informational (ส่วนลด/ยอดต่าง), not a match-quality downgrade.
    Pre-rebuild a product match was still downgraded to 'review' on any ฿ gap;
    that's exactly what D6/D12 were written to stop (plan.md §3b)."""
    c = mm_conn
    _add_order(c, 'O-ADJ', 236.0, '2026-06-05', product_id=440)
    _add_iv(c, 'IV9000010', 246.0, '2026-06-06', product_id=440)    # +10฿, shares product
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['confident'] == 1 and stats['review'] == 0
    row = c.execute(
        "SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='O-ADJ'").fetchone()
    assert row['doc_base'] == 'IV9000010' and row['confidence'] == 'confident'


def test_unconfirmed_large_amount_gap_unmatched(mm_conn):
    """No shared product AND a big amount gap → not a wild guess, left unmatched."""
    c = mm_conn
    _add_order(c, 'O-FAR', 100.0, '2026-06-08')
    _add_iv(c, 'IV9000011', 200.0, '2026-06-08')           # same day but 100฿ off, no product
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_oldest_order_takes_nearest(mm_conn):
    """Two same-amount orders + two invoices: each gets a distinct invoice."""
    c = mm_conn
    _add_order(c, 'O-1', 12.0, '2026-06-04')
    _add_order(c, 'O-2', 12.0, '2026-06-05')
    _add_iv(c, 'IV9000020', 12.0, '2026-06-04')
    _add_iv(c, 'IV9000021', 12.0, '2026-06-06')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 2 and stats['unmatched'] == 0
    docs = {r['order_sn']: r['doc_base'] for r in c.execute(
        "SELECT order_sn, doc_base FROM marketplace_order_invoice").fetchall()}
    assert docs['O-1'] != docs['O-2']


def test_more_orders_than_ivs_leaves_remainder(mm_conn):
    c = mm_conn
    _add_order(c, 'O-A', 12.0, '2026-06-04')
    _add_order(c, 'O-B', 12.0, '2026-06-05')
    _add_iv(c, 'IV9000030', 12.0, '2026-06-05')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 1


def test_manual_not_clobbered_and_iv_not_reused(mm_conn):
    c = mm_conn
    _add_order(c, 'O-M', 60.0, '2026-06-04')
    _add_order(c, 'O-A', 60.0, '2026-06-05')
    _add_iv(c, 'IV9000040', 60.0, '2026-06-05')
    mm.link_manual(c, 'shopee', 'O-M', 'IV9000040', confirmed_by='put')
    mm.run_automatch(c, 'shopee')
    rows = c.execute(
        "SELECT order_sn FROM marketplace_order_invoice WHERE doc_base='IV9000040'").fetchall()
    assert [r['order_sn'] for r in rows] == ['O-M']                 # not stolen by auto
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-A'").fetchone()[0] == 0


def test_idempotent(mm_conn):
    c = mm_conn
    _add_order(c, 'O-IDEM', 33.0, '2026-06-04')
    _add_iv(c, 'IV9000050', 33.0, '2026-06-05')
    mm.run_automatch(c, 'shopee')
    mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-IDEM'").fetchone()[0] == 1


def test_vat_type2_amount_matches_payout(mm_conn):
    """VAT-aware net (net*1.07 for vat_type=2) must feed the amount-only guess pass
    correctly. No product on either side, so per D13 this is 'review', never
    'confident' (see test_exact_amount_no_product_is_review_guess) — but amounts
    are picked so a BROKEN VAT calc (raw net=500, diff=35) would exceed
    FUZZY_AMOUNT_TOL(15) and leave it unmatched, while the correct VAT-adjusted
    net (500*1.07=535, diff=0) matches cleanly — still a real test of the VAT
    math, just via matched-or-not instead of the (now retired) confidence label."""
    c = mm_conn
    _add_order(c, 'O-VAT', 535.0, '2026-06-09')
    _add_iv(c, 'IV9000060', 500.0, '2026-06-10', vat_type=2)        # 500*1.07 = 535
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['review'] == 1 and stats['confident'] == 0
    row = c.execute(
        "SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='O-VAT'"
    ).fetchone()
    assert row['doc_base'] == 'IV9000060' and row['confidence'] == 'review'


def test_lazada_uses_L_code(mm_conn):
    c = mm_conn
    _add_order(c, 'L-1', 88.0, '2026-06-04', platform='lazada')
    _add_iv(c, 'IV9000070', 88.0, '2026-06-05', customer_code='Lหน้าร้าน')
    stats = mm.run_automatch(c, 'lazada')
    assert stats['matched'] == 1
    assert c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='L-1'"
    ).fetchone()['doc_base'] == 'IV9000070'


def test_shopee_ignores_lazada_iv(mm_conn):
    c = mm_conn
    _add_order(c, 'O-SHOP', 55.0, '2026-06-04', platform='shopee')
    _add_iv(c, 'IV9000080', 55.0, '2026-06-05', customer_code='Lหน้าร้าน')  # wrong book
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_manual_link_steals_iv(mm_conn):
    c = mm_conn
    _add_order(c, 'O-OLD', 50.0, '2026-06-04')
    _add_order(c, 'O-NEW', 50.0, '2026-06-05')
    _add_iv(c, 'IV9000200', 50.0, '2026-06-05')
    mm.run_automatch(c, 'shopee')
    holder = c.execute(
        "SELECT order_sn FROM marketplace_order_invoice WHERE doc_base='IV9000200'").fetchone()['order_sn']
    other = 'O-NEW' if holder == 'O-OLD' else 'O-OLD'
    stolen = mm.link_manual(c, 'shopee', other, 'IV9000200', confirmed_by='put')
    assert holder in stolen
    rows = c.execute(
        "SELECT order_sn, match_method FROM marketplace_order_invoice WHERE doc_base='IV9000200'").fetchall()
    assert len(rows) == 1 and rows[0]['order_sn'] == other and rows[0]['match_method'] == 'manual'


def test_picker_surfaces_near_amount_iv(mm_conn):
    """The picker shows a near-amount invoice (10฿ off) with its diff + days-after."""
    c = mm_conn
    _add_order(c, 'O-NOISE', 236.0, '2026-06-06')
    _add_iv(c, 'IV9000220', 246.0, '2026-06-08')           # +10฿, 2 days after the order
    order = c.execute("SELECT * FROM marketplace_orders WHERE order_sn='O-NOISE'").fetchone()
    cands = mm.iv_candidates(c, order)
    m = next(x for x in cands if x['doc_base'] == 'IV9000220')
    assert m['amount_diff'] == 10.0
    assert m['date_gap'] == 2


def test_picker_ranks_product_match_first(mm_conn):
    c = mm_conn
    _add_order(c, 'O-PK', 80.0, '2026-06-04', product_id=777)
    _add_iv(c, 'IV_A', 80.0, '2026-06-04')                          # gap 0, no product
    _add_iv(c, 'IV_B', 80.0, '2026-06-05', product_id=777)          # gap 1, product match
    order = c.execute("SELECT * FROM marketplace_orders WHERE order_sn='O-PK'").fetchone()
    cands = mm.iv_candidates(c, order)
    assert cands[0]['doc_base'] == 'IV_B' and cands[0]['product_match'] is True


def test_picker_ranks_exact_amount_before_nearer_date_when_no_product_match(mm_conn):
    """When the order maps to a sibling pid (no IV shares its product), the EXACT-
    amount IV must rank above a nearer-date wrong-amount one. Regression for
    260701HNJFD30G: IV6901054 (gap0, ฿-146) wrongly outranked IV6901057 (gap1, ฿0)
    because the picker sorted date before amount."""
    c = mm_conn
    _add_order(c, 'O-AMT', 364.0, '2026-07-01', product_id=194)   # sibling pid; no IV has it
    _add_iv(c, 'IV_SAMEDAY', 218.0, '2026-07-01')                 # gap 0, ฿146 off, no product
    _add_iv(c, 'IV_EXACT',   364.0, '2026-07-02')                 # gap 1, exact ฿, no product
    order = c.execute("SELECT * FROM marketplace_orders WHERE order_sn='O-AMT'").fetchone()
    cands = mm.iv_candidates(c, order)
    assert cands[0]['doc_base'] == 'IV_EXACT'
    # the near-date wrong-amount IV still appears, just ranked lower
    assert 'IV_SAMEDAY' in {x['doc_base'] for x in cands}


def test_lazada_matches_iv_on_gross_not_net(mm_conn):
    """No product on either side, so this is an amount-only guess (D13: 'review',
    never 'confident' — see test_exact_amount_no_product_is_review_guess). Still a
    real test of the gross-vs-net billed_basis: if the matcher used net payout(80)
    instead of gross item_value(100), the diff (|100-80|=20) would exceed
    FUZZY_AMOUNT_TOL(15) and it would go UNMATCHED, not just mislabeled — so
    `matched` proves gross is used, independent of the (now-retired) confidence
    assertion this test used to make."""
    c = mm_conn
    # Lazada order: gross 100, net payout 80 (20% fee). Team keyed the IV at GROSS=100.
    cur = c.execute("""INSERT INTO marketplace_orders
        (platform, order_sn, status, actual_payout, settled_at, order_date, item_total, currency)
        VALUES ('lazada','LZ1','confirmed', 80, '2026-06-10', '2026-06-10 10:00', 100, 'THB')""")
    c.execute("""INSERT INTO marketplace_order_fees (platform, order_sn, item_value, net_payout, fee_total)
                 VALUES ('lazada','LZ1',100,80,20)""")
    c.commit()
    _add_iv(c, 'IV7000001', 100.0, '2026-06-11', customer_code='Lหน้าร้าน')
    res = mm.run_automatch(c, 'lazada')
    assert res['matched'] == 1 and res['review'] == 1
    row = c.execute("SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='LZ1'").fetchone()
    assert row['doc_base'] == 'IV7000001'
    assert row['confidence'] == 'review'


def test_exact_match_not_stolen_and_cross_product_guess_blocked(mm_conn):
    """OB's product+date match to IV_SHARED must lock in, not get displaced by OA.

    REBUILD CHANGE (D7): OA declares product 457 but IV_A is product 458 — a KNOWN,
    DIFFERING product pair. Pre-rebuild, the amount-only fallback pass had no
    product-conflict guard at all, so OA's exact ฿88 coincidence auto-linked it to
    the WRONG-PRODUCT invoice IV_A. That is precisely issue 3 / mode C from
    plan.md (the ถุงหิ้ว→กันชน cross-product steal) — D7 explicitly hardens the
    guess pass to skip an IV when order-product AND IV-product are both known and
    differ. So OA must now stay UNMATCHED rather than get a fabricated wrong link
    (D14 floor) — a real amount-only guess still fires when the invoice's product
    is unmapped/unknown (see test_amount_only_guess_labeled in
    test_marketplace_match_rebuild.py)."""
    c = mm_conn
    # OA (older): shares product 457 with IV_SHARED, but IV_A (its old, WRONG,
    # amount-exact match) is a different known product (458). OB (newer): exactly IV_SHARED.
    _add_order(c, 'OA', 88, '2026-05-29', product_id=457)
    _add_order(c, 'OB', 32, '2026-05-30', product_id=457)
    _add_iv(c, 'IV_A',      88.0, '2026-05-30', product_id=458)   # OA exact amount, DIFFERENT known product
    _add_iv(c, 'IV_SHARED', 32.0, '2026-05-30', product_id=457)   # OB exact (product+amount); also OA product-overlap
    mm.run_automatch(c, 'shopee')
    links = {r['order_sn']: (r['doc_base'], r['confidence']) for r in c.execute(
        "SELECT order_sn, doc_base, confidence FROM marketplace_order_invoice WHERE platform='shopee'")}
    assert links['OB'] == ('IV_SHARED', 'confident')   # product+date match locked, not stolen by OA
    assert 'OA' not in links                           # D7: never a cross-known-product guess


def test_product_beats_loose_near_amount_neighbour(mm_conn):
    """The 913281 case: a product-overlap match with a FEE-DIVERGENT amount must not
    be displaced by an older order whose amount is only LOOSELY near. Order OB
    (product 500, payout 47 vs IV 100 — off 53, a Lazada fee) is the real match;
    OA (no product, payout 91 — off 9, a loose coincidence) must NOT steal it. The
    product pass runs before the loose-amount fallback."""
    c = mm_conn
    _add_order(c, 'OA', 91.0, '2026-06-04')                  # loose-near (off 9), no product
    _add_order(c, 'OB', 47.0, '2026-06-05', product_id=500)  # product, fee-divergent (off 53)
    _add_iv(c, 'IV_X', 100.0, '2026-06-06', product_id=500)
    mm.run_automatch(c, 'shopee')
    links = {r['order_sn']: r['doc_base'] for r in c.execute(
        "SELECT order_sn, doc_base FROM marketplace_order_invoice WHERE platform='shopee'")}
    assert links.get('OB') == 'IV_X'      # product wins over a loose amount coincidence
    assert 'OA' not in links


def test_product_beats_even_dead_on_amount_only(mm_conn):
    """Deliberate trade-off (see run_automatch NOTE): product priority wins even over
    a DEAD-ON amount-only match. OA has no product but payout == IV to the satang;
    OB shares the product with a far-off amount. OB (product) takes it — a dead-on
    amount with no product is usually a coincidence, and reserving such matches was
    tested on the full dataset and rejected (it stole 27 product matches). This test
    pins that the product-first order is intentional, not an oversight."""
    c = mm_conn
    _add_order(c, 'OA', 100.0, '2026-06-04')                 # dead-on amount, no product
    _add_order(c, 'OB', 60.0,  '2026-06-05', product_id=500) # product, far-off amount
    _add_iv(c, 'IV_X', 100.0, '2026-06-06', product_id=500)
    mm.run_automatch(c, 'shopee')
    links = {r['order_sn']: r['doc_base'] for r in c.execute(
        "SELECT order_sn, doc_base FROM marketplace_order_invoice WHERE platform='shopee'")}
    assert links.get('OB') == 'IV_X'      # product wins (intentional)
    assert 'OA' not in links


def _add_combo_formula(c, pack, comps, is_active=0):
    """Combo markers are stored INACTIVE (is_active=0, like prod's fid 126) so they
    never show up as a runnable /conversions — that inactive flag is exactly what
    _combo_components keys on. Pass is_active=1 to simulate a real manufacturing
    conversion, which must NOT be treated as a marketplace combo."""
    fid = c.execute("INSERT INTO conversion_formulas (name, output_product_id, output_qty, is_active) "
                    "VALUES (?,?,1,?)", (f'[combo] {pack}', pack, is_active)).lastrowid
    for p in comps:
        c.execute("INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity) VALUES (?,?,1)", (fid, p))
    c.commit()


def test_combo_order_matches_component_bundle_iv(mm_conn):
    """A combo/pack marketplace order (product P = pack of A+B) product-matches an
    Express IV keyed as the two SEPARATE components — because the team books a combo
    sale as its components (253 ↔ a 251+252 two-line invoice). _order_products expands
    the combo to its components, so the overlap is seen even though the order's single
    pid ≠ the IV pids. Amount is far off here, so ONLY the expansion can match it."""
    c = mm_conn
    _add_combo_formula(c, 900, [901, 902])                          # pack 900 = 901 + 902
    _add_order(c, 'O-COMBO', 100.0, '2026-06-04', product_id=900)   # combo order
    _add_iv(c, 'IV_BUNDLE', 150.0, '2026-06-05', product_id=901)    # line 1 = component 901
    c.execute("""INSERT INTO sales_transactions (date_iso, doc_no, doc_base, customer, customer_code,
                 qty, unit_price, vat_type, total, net, product_id, created_at, synced_to_stock)
                 VALUES ('2026-06-05','IV_BUNDLE-2','IV_BUNDLE','หน้าร้านS','Zหน้าร้าน',1,0,1,0,0,902,'2026-06-01 00:00:00',1)""")
    c.commit()
    mm.run_automatch(c, 'shopee')
    row = c.execute("SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-COMBO'").fetchone()
    assert row is not None and row['doc_base'] == 'IV_BUNDLE'       # matched via component expansion


def test_single_component_pack_is_not_expanded(mm_conn):
    """A ตัว/แผง pack (ONE component) is NOT expanded — those share a unit and match
    without it; expanding would over-match. Only multi-component combos expand."""
    c = mm_conn
    _add_combo_formula(c, 910, [911])                               # single-component "pack"
    _add_order(c, 'O-PACK', 100.0, '2026-06-04', product_id=910)
    _add_iv(c, 'IV_LOOSE', 150.0, '2026-06-05', product_id=911)     # only 911; amount off 50
    mm.run_automatch(c, 'shopee')
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-PACK'").fetchone()[0] == 0


def test_active_manufacturing_formula_not_expanded(mm_conn):
    """A real (is_active=1) multi-input manufacturing conversion is NOT a marketplace
    combo. _combo_components keys on the INACTIVE flag, so an active assembly formula
    (e.g. building one finished product from several parts) can't silently corrupt
    matching. Same shape as test_combo_order_matches_component_bundle_iv but active →
    no expansion → the product overlap is never seen and (amount far off) no match."""
    c = mm_conn
    _add_combo_formula(c, 920, [921, 922], is_active=1)             # ACTIVE assembly, not a combo marker
    _add_order(c, 'O-ACTIVE', 100.0, '2026-06-04', product_id=920)
    _add_iv(c, 'IV_ASSY', 150.0, '2026-06-05', product_id=921)     # line 1 = part 921
    c.execute("""INSERT INTO sales_transactions (date_iso, doc_no, doc_base, customer, customer_code,
                 qty, unit_price, vat_type, total, net, product_id, created_at, synced_to_stock)
                 VALUES ('2026-06-05','IV_ASSY-2','IV_ASSY','หน้าร้านS','Zหน้าร้าน',1,0,1,0,0,922,'2026-06-01 00:00:00',1)""")
    c.commit()
    mm.run_automatch(c, 'shopee')
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-ACTIVE'").fetchone()[0] == 0
