"""Migration 133 — backfill blank customer_code on marketplace SR rows.

Root cause (found 2026-07-08): the team books each marketplace order as one
Express invoice under a platform customer code (Zหน้าร้าน / Bหน้าร้าน /
Lหน้าร้าน), but keys marketplace RETURNS (SR / ใบลดหนี้) with the customer
NAME filled and the customer CODE left blank. Sendy imported both verbatim, so
`/customers` (models.get_customers groups by customer_code) shows each shop
TWICE: once under its real code, once under a blank code holding only the SR
rows. Verified 100% systematic — EVERY marketplace SR doc has a blank code,
EVERY IV carries the code.

The migration backfills the blank SR rows to the shop's platform code so each
shop collapses to one `/customers` entry. It is a pure relabel:
  - net / qty / total are untouched (no money movement)
  - guarded to SR docs only (`doc_base LIKE 'SR%'`) so a blank-code IV — which
    would be a DIFFERENT bug — is never silently swept in
  - the marketplace matcher reads `doc_base LIKE 'IV%'` only, so it is unaffected

Tests (deterministic, on the schema-only empty_db):
  1. Each marketplace SR row with a blank code gets its platform code.
  2. A blank-code IV row for the same shop is NOT touched (doc_base guard).
  3. A blank-code SR row for a non-marketplace customer is NOT touched.
  4. An already-coded row is unchanged.
  5. Row count and SUM(net) are unchanged (relabel only).
  6. Re-running the migration is a no-op (idempotent).
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_133 = os.path.join(
    REPO, "data", "migrations",
    "133_marketplace_sr_customer_code_backfill.sql")
ROLLBACK_133 = os.path.join(
    REPO, "data", "migrations",
    "133_marketplace_sr_customer_code_backfill.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _seed(conn):
    """Insert a controlled mix of rows covering every guard branch."""
    rows = [
        # (doc_no, customer, customer_code, doc_base, net) — the target rows:
        ("SR6700002-1", "หน้าร้านS", None, "SR6700002", 100.0),   # -> Zหน้าร้าน
        ("SR6700003-1", "หน้าร้านS", "",   "SR6700003", 50.0),    # -> Zหน้าร้าน ('' too)
        ("SR6800001-1", "หน้าร้านB", None, "SR6800001", 20.0),    # -> Bหน้าร้าน
        ("SR6900001-1", "หน้าร้านL", None, "SR6900001", 30.0),    # -> Lหน้าร้าน
        # controls that must NOT change:
        ("IV6900539-1", "หน้าร้านS", "Zหน้าร้าน", "IV6900539", 299.0),  # already coded
        ("IV6900540-1", "หน้าร้านS", None, "IV6900540", 199.0),   # blank IV -> stays blank (guard)
        ("SR7000001-1", "ร้านทดสอบ",  None, "SR7000001", 40.0),   # non-marketplace SR -> stays blank
    ]
    conn.executemany(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, customer, customer_code, doc_base, net) "
        "VALUES ('2026-03-01', ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _code(conn, doc_no):
    return conn.execute(
        "SELECT customer_code FROM sales_transactions WHERE doc_no=?", (doc_no,)
    ).fetchone()[0]


def test_marketplace_sr_rows_get_platform_code(empty_db_conn):
    _seed(empty_db_conn)
    _apply(empty_db_conn, MIG_133)
    assert _code(empty_db_conn, "SR6700002-1") == "Zหน้าร้าน"
    assert _code(empty_db_conn, "SR6700003-1") == "Zหน้าร้าน"
    assert _code(empty_db_conn, "SR6800001-1") == "Bหน้าร้าน"
    assert _code(empty_db_conn, "SR6900001-1") == "Lหน้าร้าน"


def test_blank_iv_row_not_touched(empty_db_conn):
    """The doc_base LIKE 'SR%' guard: a blank-code IV is a different bug, leave it."""
    _seed(empty_db_conn)
    _apply(empty_db_conn, MIG_133)
    assert _code(empty_db_conn, "IV6900540-1") is None


def test_non_marketplace_sr_not_touched(empty_db_conn):
    _seed(empty_db_conn)
    _apply(empty_db_conn, MIG_133)
    assert _code(empty_db_conn, "SR7000001-1") is None


def test_already_coded_row_unchanged(empty_db_conn):
    _seed(empty_db_conn)
    _apply(empty_db_conn, MIG_133)
    assert _code(empty_db_conn, "IV6900539-1") == "Zหน้าร้าน"


def test_no_blank_marketplace_sr_remains(empty_db_conn):
    _seed(empty_db_conn)
    _apply(empty_db_conn, MIG_133)
    remaining = empty_db_conn.execute(
        "SELECT COUNT(*) FROM sales_transactions "
        "WHERE customer LIKE 'หน้าร้าน%' "
        "  AND (customer_code IS NULL OR customer_code='') "
        "  AND doc_base LIKE 'SR%'"
    ).fetchone()[0]
    assert remaining == 0


def test_row_count_and_net_sum_unchanged(empty_db_conn):
    """Relabel only — no rows added/removed, no money moved."""
    _seed(empty_db_conn)
    n_before = empty_db_conn.execute(
        "SELECT COUNT(*) FROM sales_transactions").fetchone()[0]
    sum_before = empty_db_conn.execute(
        "SELECT ROUND(SUM(net), 2) FROM sales_transactions").fetchone()[0]
    _apply(empty_db_conn, MIG_133)
    n_after = empty_db_conn.execute(
        "SELECT COUNT(*) FROM sales_transactions").fetchone()[0]
    sum_after = empty_db_conn.execute(
        "SELECT ROUND(SUM(net), 2) FROM sales_transactions").fetchone()[0]
    assert n_after == n_before
    assert sum_after == sum_before


def test_idempotent(empty_db_conn):
    _seed(empty_db_conn)
    _apply(empty_db_conn, MIG_133)
    codes_1 = empty_db_conn.execute(
        "SELECT doc_no, customer_code FROM sales_transactions ORDER BY doc_no"
    ).fetchall()
    _apply(empty_db_conn, MIG_133)  # second run must not error or change anything
    codes_2 = empty_db_conn.execute(
        "SELECT doc_no, customer_code FROM sales_transactions ORDER BY doc_no"
    ).fetchall()
    assert codes_1 == codes_2
