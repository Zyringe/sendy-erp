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
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT)"
    )
    rows = [
        (1, 'ตัว', 'A', 1, 90, 90, 0, '2025-01-01'),
        (1, 'ตัว', 'B', 1, 95, 95, 0, '2025-01-01'),
        (1, 'ตัว', 'C', 1, 100, 100, 0, '2025-01-01'),
    ]
    c.executemany("INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?)", rows)
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
    for k in ('product_id', 'unit', 'customer_median', 'peer_median', 'peer_n', 'diff', 'flag'):
        assert k in dict(row), f"missing key: {k}"


def test_no_peers_flag_same():
    """If there is only one customer buying a product, peer_median=None and flag='same'."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE sales_transactions("
        "product_id INT, unit TEXT, customer_code TEXT, "
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT)"
    )
    c.execute("INSERT INTO sales_transactions VALUES (1,'ตัว','SOLO',2,80,160,0,'2025-01-01')")
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
        "qty REAL, unit_price REAL, net REAL, vat_type INT, date_iso TEXT)"
    )
    # A buys at net=100 vat_type=0 → cash=100
    # B buys at net=100 vat_type=2 → cash=107 (apples-to-apples)
    c.executemany("INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?)", [
        (1, 'ตัว', 'A', 1, 100, 100, 0, '2025-01-01'),
        (1, 'ตัว', 'B', 1, 100, 100, 2, '2025-01-01'),
    ])
    c.commit()
    res_a = pp.product_peer_prices(c, customer_code='A')
    row_a = {r['product_id']: r for r in res_a}[1]
    # A's peer is B whose cash=107; peer_median should be ~107
    assert abs(row_a['peer_median'] - 107) < 0.5
    assert row_a['flag'] == 'cheaper'
