"""Order-driven platform stock deduction.

Design doc: projects/order-driven-platform-deduction/plan.md.

Task 1.2: `resolve_line_listing` (listing-grain resolver) — same 3-step
fallback as `resolve_marketplace_product_id` but resolves to the LISTING
(platform_skus row), not the internal product, and has no
internal_product_id requirement.

Task 1.3: the diff engine (`_apply_order_stock_effect`) wired into
`import_marketplace_orders` — deducts/credits `platform_skus.stock` per
resolved listing, diffed against `platform_stock_deductions` provenance
(source_table='marketplace_orders'), gated by `platform_skus.stock_as_of`.

Every fixture force-states its own `platform_skus` rows on `empty_db_conn`
(a zero-row schema clone of the live worktree DB, which already carries
migration 175 — verified separately in tests/test_mig175_order_driven.py).
Every "nothing happened" assertion is paired with a CONTROL that shows the
machinery firing on a qualifying sibling (verification-discipline.md).
"""
import sqlite3

import pytest

from models.marketplace import resolve_line_listing


# ── Task 1.2: resolve_line_listing ──────────────────────────────────────────

def _seed_sku(conn, platform='shopee', variation_id=None, seller_sku=None,
              product_name='p', variation_name=None, stock=10,
              stock_as_of=None, imported_at='2026-01-01 00:00:00',
              is_ignored=0):
    cur = conn.execute(
        "INSERT INTO platform_skus (platform, variation_id, seller_sku, "
        "product_name, variation_name, stock, stock_as_of, imported_at, is_ignored) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (platform, variation_id, seller_sku, product_name, variation_name,
         stock, stock_as_of, imported_at, is_ignored))
    conn.commit()
    return cur.lastrowid


def _seed_stub(conn, platform='shopee', product_name='p', variation_name=None,
               imported_at='2026-01-01 00:00:00'):
    """A listing row with variation_id NULL AND stock NULL — the shape a
    marketplace export leaves when a listing was seen but never carried a
    figure. Must never be a resolution candidate (a stub winning would make
    the deduction a silent no-op)."""
    cur = conn.execute(
        "INSERT INTO platform_skus (platform, variation_id, seller_sku, "
        "product_name, variation_name, stock, imported_at) "
        "VALUES (?, NULL, NULL, ?, ?, NULL, ?)",
        (platform, product_name, variation_name, imported_at))
    conn.commit()
    return cur.lastrowid


def _item(variation_id=None, seller_sku=None, item_name=None,
          variation_name=None, qty=1.0):
    return {'variation_id': variation_id, 'seller_sku': seller_sku,
            'item_name': item_name, 'variation_name': variation_name, 'qty': qty}


def _count(conn, where, *params):
    return conn.execute(
        f"SELECT COUNT(*) FROM platform_skus WHERE {where}", params).fetchone()[0]


def test_resolves_by_variation_id(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, variation_id='V1', stock=5)
    assert _count(conn, "variation_id='V1'") == 1  # vacuity guard

    row = resolve_line_listing(conn, 'shopee', _item(variation_id='V1'))
    assert row is not None and row['id'] == lid


def test_resolves_by_seller_sku(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, seller_sku='S1', stock=5)
    assert _count(conn, "seller_sku='S1'") == 1  # vacuity guard

    row = resolve_line_listing(conn, 'shopee', _item(seller_sku='S1'))
    assert row is not None and row['id'] == lid


def test_resolves_by_name_and_variation_name(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, product_name='เทปกาว', variation_name='แดง', stock=5)
    assert _count(conn, "product_name='เทปกาว'") == 1  # vacuity guard

    row = resolve_line_listing(
        conn, 'shopee', _item(item_name='เทปกาว', variation_name='แดง'))
    assert row is not None and row['id'] == lid


def test_is_ignored_row_never_returned(empty_db_conn):
    conn = empty_db_conn
    _seed_sku(conn, variation_id='V-IGN', stock=5, is_ignored=1)
    assert _count(conn, "variation_id='V-IGN'") == 1  # vacuity guard

    row = resolve_line_listing(conn, 'shopee', _item(variation_id='V-IGN'))
    assert row is None


def test_no_match_returns_none(empty_db_conn):
    conn = empty_db_conn
    row = resolve_line_listing(conn, 'shopee', _item(variation_id='NOPE'))
    assert row is None


def test_ambiguous_seller_sku_returns_none_with_control(empty_db_conn):
    conn = empty_db_conn
    _seed_sku(conn, seller_sku='DUP', product_name='a', stock=5)
    _seed_sku(conn, seller_sku='DUP', product_name='b', stock=5)
    assert _count(conn, "seller_sku='DUP'") == 2  # vacuity guard: 2 real candidates

    row = resolve_line_listing(conn, 'shopee', _item(seller_sku='DUP'))
    assert row is None  # ambiguous — do not guess with write power

    # CONTROL: one row alone still resolves — the ambiguity guard isn't
    # silently swallowing every seller_sku lookup.
    solo_lid = _seed_sku(conn, seller_sku='SOLO', stock=5)
    assert _count(conn, "seller_sku='SOLO'") == 1  # vacuity guard
    row2 = resolve_line_listing(conn, 'shopee', _item(seller_sku='SOLO'))
    assert row2 is not None and row2['id'] == solo_lid


def test_stub_row_never_a_candidate_real_sibling_resolves(empty_db_conn):
    conn = empty_db_conn
    _seed_stub(conn, product_name='ตัวอย่าง')
    real_lid = _seed_sku(conn, product_name='ตัวอย่าง', stock=5)
    assert _count(conn, "product_name='ตัวอย่าง'") == 2  # vacuity guard: stub + real

    row = resolve_line_listing(conn, 'shopee', _item(item_name='ตัวอย่าง'))
    assert row is not None and row['id'] == real_lid  # stub excluded, real wins


def test_stub_row_alone_returns_none(empty_db_conn):
    conn = empty_db_conn
    _seed_stub(conn, product_name='stub-only')
    assert _count(conn, "product_name='stub-only'") == 1  # vacuity guard

    row = resolve_line_listing(conn, 'shopee', _item(item_name='stub-only'))
    assert row is None
