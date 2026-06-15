"""Tests for peer_pricing.product_peer_prices.

Fixture: 3 customers (A, B, C) buy product 1 in unit 'ตัว' at 90/95/100 (no VAT).
  - Customer A (90): cheaper than peers (B=95, C=100 → peer median = 97.5)
  - Customer C (100): higher than peers (A=90, B=95 → peer median = 92.5)
  - Customer B (95): compare vs A=90, C=100 → peer median = 95.0 → same
  - peer_n for each = 2 (two distinct other customers)
"""
import sqlite3
import peer_pricing as pp


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE sales_transactions("
        "product_id INT, unit TEXT, customer_code TEXT, "
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT, discount TEXT)"
    )
    rows = [
        (1, 'ตัว', 'A', 1, 90, 90, 0, '2025-01-01', ''),
        (1, 'ตัว', 'B', 1, 95, 95, 0, '2025-01-01', ''),
        (1, 'ตัว', 'C', 1, 100, 100, 0, '2025-01-01', ''),
    ]
    c.executemany("INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c


def test_cheaper_than_peers():
    res = pp.product_peer_prices(_db(), customer_code='A')
    row = {r['product_id']: r for r in res}[1]
    assert abs(row['peer_median'] - 97.5) < 0.01
    assert row['flag'] == 'cheaper'


def test_higher_than_peers():
    res = pp.product_peer_prices(_db(), customer_code='C')
    row = {r['product_id']: r for r in res}[1]
    assert row['flag'] == 'higher'


def test_same_as_peers():
    """B at 95 vs peers A(90) C(100) -> peer_median=95.0 -> same."""
    res = pp.product_peer_prices(_db(), customer_code='B')
    row = {r['product_id']: r for r in res}[1]
    assert abs(row['peer_median'] - 95.0) < 0.01
    assert row['flag'] == 'same'


def test_peer_n():
    """Each customer should see peer_n=2."""
    for cust in ('A', 'B', 'C'):
        res = pp.product_peer_prices(_db(), customer_code=cust)
        row = {r['product_id']: r for r in res}[1]
        assert row['peer_n'] == 2, f"expected 2 peers for {cust}, got {row['peer_n']}"


def test_return_keys():
    """Every row must carry the documented keys."""
    res = pp.product_peer_prices(_db(), customer_code='A')
    assert len(res) == 1
    row = res[0]
    for k in ('product_id', 'unit', 'customer_median', 'customer_latest', 'peer_median', 'peer_n', 'diff', 'flag'):
        assert k in dict(row), f"missing key: {k}"


def test_customer_latest_is_most_recent_not_median():
    """customer_latest = cash from the most-recent-dated buy, distinct from the median."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE sales_transactions("
        "product_id INT, unit TEXT, customer_code TEXT, "
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT, discount TEXT)"
    )
    # X buys product 1: 20, 20 (older), then 16 on the latest date (2025-05).
    #   median([20,20,16]) = 20   but   latest = 16
    c.executemany("INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, 'ตัว', 'X', 1, 20, 20, 0, '2024-01-01', ''),
        (1, 'ตัว', 'X', 1, 20, 20, 0, '2024-06-01', ''),
        (1, 'ตัว', 'X', 1, 16, 16, 0, '2025-05-01', ''),
        (1, 'ตัว', 'Y', 1, 25, 25, 0, '2024-01-01', ''),  # a peer so peer_median is defined
    ])
    c.commit()
    row = {r['product_id']: r for r in pp.product_peer_prices(c, customer_code='X')}[1]
    assert row['customer_median'] == 20
    assert row['customer_latest'] == 16


def test_no_peers_flag_same():
    """If there is only one customer buying a product, peer_median=None and flag='same'."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE sales_transactions("
        "product_id INT, unit TEXT, customer_code TEXT, "
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT, discount TEXT)"
    )
    c.execute("INSERT INTO sales_transactions VALUES (1,'ตัว','SOLO',2,80,160,0,'2025-01-01','')")
    c.commit()
    res = pp.product_peer_prices(c, customer_code='SOLO')
    assert len(res) == 1
    row = res[0]
    assert row['peer_n'] == 0
    assert row['peer_median'] is None
    assert row['flag'] == 'same'


def test_vat_type2_included_in_peer_cash():
    """vat_type=2 rows: cash = net*1.07. Verify the VAT adjustment is applied."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE sales_transactions("
        "product_id INT, unit TEXT, customer_code TEXT, "
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT, discount TEXT)"
    )
    # A buys at net=100 vat_type=0 → cash=100
    # B buys at net=100 vat_type=2 → cash=107 (apples-to-apples)
    c.executemany("INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, 'ตัว', 'A', 1, 100, 100, 0, '2025-01-01', ''),
        (1, 'ตัว', 'B', 1, 100, 100, 2, '2025-01-01', ''),
    ])
    c.commit()
    res_a = pp.product_peer_prices(c, customer_code='A')
    row_a = {r['product_id']: r for r in res_a}[1]
    # A's peer is B whose cash=107; peer_median should be ~107
    assert abs(row_a['peer_median'] - 107) < 0.5
    assert row_a['flag'] == 'cheaper'


# ── New: gross list price + discount passthrough (call-card pricing upgrade) ──

def _db_disc(rows):
    """Fixture with the discount column. rows = list of
    (product_id, unit, customer_code, qty, unit_price, net, vat_type, date_iso, discount)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE sales_transactions("
        "product_id INT, unit TEXT, customer_code TEXT, "
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT, discount TEXT)"
    )
    c.executemany("INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c


def test_customer_latest_list_and_discount():
    """customer_latest_list/disc come from the customer's most-recent-dated line:
    gross unit_price (20) + its discount string ('20%'), distinct from the net cash (16)."""
    c = _db_disc([
        (1, 'ตัว', 'X', 1, 20, 20, 0, '2024-01-01', ''),     # older, no discount
        (1, 'ตัว', 'X', 1, 20, 16, 0, '2025-05-01', '20%'),  # latest: 20 gross, -20% → 16 net
        (1, 'ตัว', 'Y', 1, 25, 25, 0, '2024-01-01', ''),     # a peer so peer_median is defined
    ])
    row = {r['product_id']: r for r in pp.product_peer_prices(c, customer_code='X')}[1]
    assert row['customer_latest'] == 16
    assert row['customer_latest_list'] == 20
    assert row['customer_latest_disc'] == '20%'


def test_peer_representative_is_median_peer_odd():
    """With 3 peers (cash 90/95/100) the representative is the median peer (95),
    so peer_repr_list/disc come from THAT peer's line (100 gross, '5%')."""
    c = _db_disc([
        (1, 'ตัว', 'T',  1, 100, 92,  0, '2025-01-01', '8%'),   # target
        (1, 'ตัว', 'P1', 1, 100, 90,  0, '2025-01-01', '10%'),  # peer cash 90
        (1, 'ตัว', 'P2', 1, 100, 95,  0, '2025-01-01', '5%'),   # peer cash 95 (median)
        (1, 'ตัว', 'P3', 1, 100, 100, 0, '2025-01-01', ''),     # peer cash 100
    ])
    row = {r['product_id']: r for r in pp.product_peer_prices(c, customer_code='T')}[1]
    assert abs(row['peer_median'] - 95) < 0.01
    assert row['peer_repr_list'] == 100
    assert row['peer_repr_disc'] == '5%'


def test_peer_representative_tie_picks_lower():
    """Even peer count: peer_median=95 sits between peers 90 and 100 (both 5 away).
    Tie → pick the LOWER-median peer (90), so its discount ('10%') is shown."""
    c = _db_disc([
        (1, 'ตัว', 'T',  1, 100, 93,  0, '2025-01-01', '7%'),
        (1, 'ตัว', 'P1', 1, 100, 90,  0, '2025-01-01', '10%'),  # lower peer
        (1, 'ตัว', 'P2', 1, 100, 100, 0, '2025-01-01', ''),     # higher peer
    ])
    row = {r['product_id']: r for r in pp.product_peer_prices(c, customer_code='T')}[1]
    assert abs(row['peer_median'] - 95) < 0.01
    assert row['peer_repr_list'] == 100
    assert row['peer_repr_disc'] == '10%'


def test_discount_string_passthrough_compound():
    """Compound discount text ('15+5%') is returned verbatim, not parsed."""
    c = _db_disc([
        # 215 gross, 15+5% cascade → 215*0.85*0.95 = 173.6125 net
        (1, 'ม้วน', 'X', 1, 215, 173.6125, 0, '2025-05-01', '15+5%'),
        (1, 'ม้วน', 'Y', 1, 200, 200, 0, '2025-01-01', ''),
    ])
    row = {r['product_id']: r for r in pp.product_peer_prices(c, customer_code='X')}[1]
    assert row['customer_latest_list'] == 215
    assert row['customer_latest_disc'] == '15+5%'


def test_new_fields_present_and_none_without_peers():
    """All four new keys exist; peer_repr_* are None when there are no peers."""
    c = _db_disc([
        (1, 'ตัว', 'SOLO', 2, 80, 160, 0, '2025-01-01', ''),
    ])
    row = pp.product_peer_prices(c, customer_code='SOLO')[0]
    for k in ('customer_latest_list', 'customer_latest_disc', 'peer_repr_list', 'peer_repr_disc'):
        assert k in dict(row), f"missing key: {k}"
    assert row['peer_repr_list'] is None
    assert row['peer_repr_disc'] is None
