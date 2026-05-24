"""Unit-context helpers in bsn_suggest.py.

Covers the structural split between cost-from-purchase and unit-from-anywhere:

- _latest_purchase: unchanged contract — only purchase rows feed cost
- _latest_bsn_unit: latest unit across purchase ∪ sales (handles sale-only codes)
- _all_units_seen: distinct units ordered by latest date (split-unit detection)
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import bsn_suggest  # noqa: E402


CODE = "TESTU100"


def _seed_schema(conn):
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE purchase_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT, date_iso TEXT, unit TEXT,
            unit_price REAL, qty REAL
        );
        CREATE TABLE sales_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT, date_iso TEXT, unit TEXT,
            unit_price REAL, qty REAL
        );
    """)


def _add_p(conn, date, unit, cost=10.0):
    conn.execute(
        "INSERT INTO purchase_transactions (bsn_code, date_iso, unit, unit_price, qty) "
        "VALUES (?,?,?,?,1)", (CODE, date, unit, cost))


def _add_s(conn, date, unit):
    conn.execute(
        "INSERT INTO sales_transactions (bsn_code, date_iso, unit, unit_price, qty) "
        "VALUES (?,?,?,1,1)", (CODE, date, unit))


def test_no_transactions_returns_empty():
    conn = sqlite3.connect(":memory:")
    _seed_schema(conn)
    assert bsn_suggest._latest_purchase(conn, CODE) == {}
    assert bsn_suggest._latest_bsn_unit(conn, CODE) == {}
    assert bsn_suggest._all_units_seen(conn, CODE) == []


def test_purchase_only_unit_from_purchase():
    conn = sqlite3.connect(":memory:")
    _seed_schema(conn)
    _add_p(conn, "2026-04-01", "ตัว", cost=12.5)

    lp = bsn_suggest._latest_purchase(conn, CODE)
    assert lp["cost_price"] == 12.5
    assert lp["unit_type"] == "ตัว"

    lu = bsn_suggest._latest_bsn_unit(conn, CODE)
    assert lu == {"unit": "ตัว", "source": "purchase", "last_date": "2026-04-01"}

    units = bsn_suggest._all_units_seen(conn, CODE)
    assert units == [{"unit": "ตัว", "last_date": "2026-04-01"}]


def test_sale_only_unit_falls_back_to_sale_purchase_dict_empty():
    """The bug Put surfaced: 041ม6820 — no purchase, only a sale."""
    conn = sqlite3.connect(":memory:")
    _seed_schema(conn)
    _add_s(conn, "2026-05-22", "แผง")

    # _latest_purchase MUST stay empty — caller relies on this for cost prefill
    assert bsn_suggest._latest_purchase(conn, CODE) == {}

    # _latest_bsn_unit MUST surface the sale unit
    lu = bsn_suggest._latest_bsn_unit(conn, CODE)
    assert lu == {"unit": "แผง", "source": "sale", "last_date": "2026-05-22"}

    units = bsn_suggest._all_units_seen(conn, CODE)
    assert units == [{"unit": "แผง", "last_date": "2026-05-22"}]


def test_both_sources_latest_wins_regardless_of_type():
    """When both purchase + sale exist, latest_date picks the winner —
    not a hardcoded preference for purchase."""
    conn = sqlite3.connect(":memory:")
    _seed_schema(conn)
    _add_p(conn, "2026-03-10", "ตัว", cost=10.0)
    _add_s(conn, "2026-05-22", "แผง")  # newer

    # Purchase cost untouched
    lp = bsn_suggest._latest_purchase(conn, CODE)
    assert lp["unit_type"] == "ตัว"
    assert lp["cost_price"] == 10.0

    # Latest unit = the sale (newer)
    lu = bsn_suggest._latest_bsn_unit(conn, CODE)
    assert lu["unit"] == "แผง"
    assert lu["source"] == "sale"

    # Both units visible, latest first
    units = bsn_suggest._all_units_seen(conn, CODE)
    assert units == [
        {"unit": "แผง", "last_date": "2026-05-22"},
        {"unit": "ตัว", "last_date": "2026-03-10"},
    ]


def test_same_unit_different_sources_collapsed():
    conn = sqlite3.connect(":memory:")
    _seed_schema(conn)
    _add_p(conn, "2026-03-10", "ตัว")
    _add_s(conn, "2026-05-22", "ตัว")

    units = bsn_suggest._all_units_seen(conn, CODE)
    # Single entry — MAX(date) collapses identical units
    assert units == [{"unit": "ตัว", "last_date": "2026-05-22"}]


def test_null_and_empty_units_filtered():
    """Defense: rows with NULL or empty unit must not surface in either helper."""
    conn = sqlite3.connect(":memory:")
    _seed_schema(conn)
    conn.execute(
        "INSERT INTO sales_transactions (bsn_code, date_iso, unit, unit_price, qty) "
        "VALUES (?, '2026-05-22', NULL, 1, 1)", (CODE,))
    conn.execute(
        "INSERT INTO sales_transactions (bsn_code, date_iso, unit, unit_price, qty) "
        "VALUES (?, '2026-05-23', '', 1, 1)", (CODE,))
    _add_s(conn, "2026-05-20", "แผง")

    lu = bsn_suggest._latest_bsn_unit(conn, CODE)
    assert lu["unit"] == "แผง"  # NULL + '' skipped even though dates are newer

    units = bsn_suggest._all_units_seen(conn, CODE)
    assert units == [{"unit": "แผง", "last_date": "2026-05-20"}]
