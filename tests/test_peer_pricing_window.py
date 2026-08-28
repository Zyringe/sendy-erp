"""Tests for peer_pricing.product_peer_prices's `window_from_by_pair` kwarg
(task-2-brief.md PR C / 2e, C3/C4/C5).

C5: a SEPARATE fixture from test_peer_pricing.py's minimal one -- that file's
fixture deliberately lacks doc_base/customer/ar_writeoffs (it only needs to
prove the `window_from_by_pair=None` legacy path, which touches none of those
columns). This file's fixture carries the full set
price_lookup.evidence_filter needs, so the FILTERED path can be exercised
here without ever touching test_peer_pricing.py.
"""
import sqlite3

import peer_pricing as pp


def _db2(rows, writeoffs=()):
    """rows = (product_id, unit, customer, customer_code, qty, unit_price,
    net, vat_type, date_iso, discount, doc_no, doc_base).

    Carries doc_base + an ar_writeoffs table so price_lookup.evidence_filter
    (excludes SR/HS returns, write-offs, รายการหน้าร้าน marketplace rows, the
    cost-basis dummy invoices) can run against it."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE sales_transactions("
        "product_id INT, unit TEXT, customer TEXT, customer_code TEXT, "
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT, "
        "discount TEXT, doc_no TEXT, doc_base TEXT)"
    )
    c.execute("CREATE TABLE ar_writeoffs (doc_no TEXT, excludes_revenue INTEGER)")
    c.executemany(
        "INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    for doc_no, excl in writeoffs:
        c.execute("INSERT INTO ar_writeoffs VALUES (?,?)", (doc_no, excl))
    c.commit()
    return c


# ── None vs a dict switch behavior ──────────────────────────────────────────

def test_none_is_legacy_all_time_unfiltered():
    """window_from_by_pair=None -- the default -- must behave EXACTLY like
    calling product_peer_prices with no kwarg at all: marketplace rows and
    write-offs are NOT excluded (that is what makes it 'legacy')."""
    rows = [
        (1, 'ตัว', 'หน้าร้านS', 'MKT1', 1, 100, 100, 0, '2026-01-01', '', 'IV1', 'IV1'),
        (1, 'ตัว', 'ร้าน T', 'T', 1, 100, 90, 0, '2026-01-01', '', 'IV2', 'IV2'),
    ]
    c = _db2(rows)
    res = pp.product_peer_prices(c, customer_code='T', window_from_by_pair=None)
    row = {r['product_id']: r for r in res}[1]
    # the marketplace row counts as a peer under the legacy (unfiltered) path
    assert row['peer_n'] == 1
    assert row['peer_median'] == 100


def test_dict_excludes_marketplace_and_writeoffs():
    """Given a map (even an empty one -- no pair entries), the population
    is restricted to evidence_filter: marketplace + write-off rows drop
    out entirely."""
    rows = [
        (1, 'ตัว', 'หน้าร้านS', 'MKT1', 1, 100, 100, 0, '2026-01-01', '', 'IV1', 'IV1'),
        (1, 'ตัว', 'ร้าน WO', 'WO', 1, 100, 100, 0, '2026-01-01', '', 'IV3-1', 'IV3'),
        (1, 'ตัว', 'ร้าน T', 'T', 1, 100, 90, 0, '2026-01-01', '', 'IV2', 'IV2'),
    ]
    c = _db2(rows, writeoffs=[('IV3', 1)])
    res = pp.product_peer_prices(c, customer_code='T', window_from_by_pair={})
    row = {r['product_id']: r for r in res}[1]
    # neither หน้าร้านS (marketplace) nor the write-off customer count
    assert row['peer_n'] == 0
    assert row['peer_median'] is None


# ── per-pair date floor + the documented fallback ───────────────────────────

def test_pair_present_restricts_by_its_own_date_floor():
    rows = [
        (1, 'ตัว', 'ร้าน T',  'T',  1, 100, 90,  0, '2026-07-01', '', 'IV1', 'IV1'),
        (1, 'ตัว', 'ร้าน P1', 'P1', 1, 100, 100, 0, '2026-01-01', '', 'IV2', 'IV2'),  # pre-floor
        (1, 'ตัว', 'ร้าน P2', 'P2', 1, 100, 60,  0, '2026-07-01', '', 'IV3', 'IV3'),  # post-floor
    ]
    c = _db2(rows)
    res = pp.product_peer_prices(c, customer_code='T',
                                  window_from_by_pair={(1, 'ตัว'): '2026-06-01'})
    row = {r['product_id']: r for r in res}[1]
    assert row['peer_n'] == 1
    assert row['peer_median'] == 60


def test_pair_absent_from_map_gets_evidence_filter_but_no_date_restriction():
    """A pair not present in the map (or mapped to None) still gets
    evidence_filter, but is NOT date-restricted -- an old bill still
    counts, it is never silently dropped and never treated as 'today'."""
    rows = [
        (1, 'ตัว', 'ร้าน T',  'T',  1, 100, 90,  0, '2020-01-01', '', 'IV1', 'IV1'),
        (1, 'ตัว', 'ร้าน P1', 'P1', 1, 100, 100, 0, '2019-01-01', '', 'IV2', 'IV2'),  # very old
    ]
    c = _db2(rows)
    # (1, 'ตัว') absent from the map entirely.
    res = pp.product_peer_prices(c, customer_code='T', window_from_by_pair={})
    row = {r['product_id']: r for r in res}[1]
    assert row['peer_n'] == 1
    assert row['peer_median'] == 100

    # same result when the pair maps explicitly to None.
    res2 = pp.product_peer_prices(c, customer_code='T',
                                   window_from_by_pair={(1, 'ตัว'): None})
    row2 = {r['product_id']: r for r in res2}[1]
    assert row2['peer_n'] == 1
    assert row2['peer_median'] == 100


def test_two_units_same_product_filtered_independently():
    """C3's own test: one product at TWO units with different epochs --
    each unit's PEER rows are filtered by ITS OWN epoch, independently.
    A single shared epoch would wrongly exclude/include the wrong bills
    for one of the two units."""
    rows = [
        # unit 'ตัว': epoch 2026-06-01. Peer PA1 has a pre-epoch bill (100,
        # excluded) and nothing post-epoch; peer PA2 is post-epoch (60, kept).
        (1, 'ตัว', 'ร้าน PA1', 'PA1', 1, 100, 100, 0, '2026-01-01', '', 'IV1', 'IV1'),
        (1, 'ตัว', 'ร้าน PA2', 'PA2', 1, 100, 60,  0, '2026-07-01', '', 'IV2', 'IV2'),
        (1, 'ตัว', 'ร้าน T',   'T',   1, 100, 90,  0, '2026-07-01', '', 'IV3', 'IV3'),
        # unit 'แผง': epoch 2026-03-01 (different!). Peer PB1's bill is
        # AFTER unit 'ตัว's epoch but BEFORE unit 'แผง's own epoch --
        # a shared/wrong epoch would keep it; the correct, independent
        # epoch for 'แผง' must exclude it.
        (1, 'แผง', 'ร้าน PB1', 'PB1', 1, 200, 200, 0, '2026-02-01', '', 'IV4', 'IV4'),
        (1, 'แผง', 'ร้าน PB2', 'PB2', 1, 200, 150, 0, '2026-04-01', '', 'IV5', 'IV5'),
        (1, 'แผง', 'ร้าน T',   'T',   1, 200, 180, 0, '2026-04-01', '', 'IV6', 'IV6'),
    ]
    c = _db2(rows)
    window_map = {(1, 'ตัว'): '2026-06-01', (1, 'แผง'): '2026-03-01'}
    res = pp.product_peer_prices(c, customer_code='T', window_from_by_pair=window_map)
    row_a = {(r['product_id'], r['unit']): r for r in res}[(1, 'ตัว')]
    row_b = {(r['product_id'], r['unit']): r for r in res}[(1, 'แผง')]
    assert row_a['peer_n'] == 1
    assert row_a['peer_median'] == 60
    assert row_b['peer_n'] == 1
    assert row_b['peer_median'] == 150
