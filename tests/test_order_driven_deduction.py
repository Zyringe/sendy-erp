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

import models.marketplace as marketplace_mod
from models.marketplace import (
    resolve_line_listing, RESTOCK_STATUSES, import_marketplace_orders,
)


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


# ── Task 1.3: the diff engine, wired into import_marketplace_orders ────────

def _order(order_sn, items, status='ready_to_ship', order_date='2026-08-20 10:00',
           platform='shopee'):
    return {
        'platform': platform, 'order_sn': order_sn, 'status': status,
        'order_date': order_date, 'paid_date': None,
        'buyer_name': 'x', 'buyer_phone': '0000000000', 'ship_address': 'addr',
        'item_total': sum((i.get('item_subtotal') or 0) for i in items),
        'marketplace_fee': None, 'payout': None, 'currency': 'THB', 'items': items,
    }


def _line(line_key, variation_id=None, seller_sku=None, item_name=None,
          variation_name=None, qty=1.0, unit_price=10.0):
    return {
        'line_key': line_key, 'variation_id': variation_id, 'seller_sku': seller_sku,
        'item_name': item_name, 'variation_name': variation_name, 'qty': qty,
        'unit_price': unit_price, 'item_subtotal': qty * unit_price,
    }


def _stock(conn, lid):
    return conn.execute(
        "SELECT stock FROM platform_skus WHERE id=?", (lid,)).fetchone()[0]


def _provenance(conn, order_id, lid):
    row = conn.execute(
        "SELECT units FROM platform_stock_deductions WHERE source_table='marketplace_orders' "
        "AND source_id=? AND platform_sku_id=?", (order_id, lid)).fetchone()
    return row[0] if row else None


def _prov_rowid(conn, order_id, lid):
    row = conn.execute(
        "SELECT rowid, units FROM platform_stock_deductions "
        "WHERE source_table='marketplace_orders' AND source_id=? AND platform_sku_id=?",
        (order_id, lid)).fetchone()
    return tuple(row) if row else None


def _order_id(conn, platform, order_sn):
    return conn.execute(
        "SELECT id FROM marketplace_orders WHERE platform=? AND order_sn=?",
        (platform, order_sn)).fetchone()[0]


def test_first_import_deducts_exact_listing(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, variation_id='V1', stock=50, stock_as_of='2026-01-01 00:00:00')
    assert _stock(conn, lid) == 50  # vacuity guard

    order = _order('ORD-1', [_line('L1', variation_id='V1', qty=2.0)])
    import_marketplace_orders(conn, [order], 'f.xlsx')

    oid = _order_id(conn, 'shopee', 'ORD-1')
    assert _stock(conn, lid) == 48
    assert _provenance(conn, oid, lid) == 2


def test_order_before_baseline_is_gated(empty_db_conn):
    conn = empty_db_conn
    gated_lid = _seed_sku(conn, variation_id='V-GATED', stock=50,
                          stock_as_of='2026-08-25 00:00:00')
    control_lid = _seed_sku(conn, variation_id='V-CTRL', stock=50,
                            stock_as_of='2026-01-01 00:00:00')
    assert _stock(conn, gated_lid) == 50 and _stock(conn, control_lid) == 50  # vacuity guard

    order = _order('ORD-2', [
        _line('L1', variation_id='V-GATED', qty=3.0),
        _line('L2', variation_id='V-CTRL', qty=3.0),
    ], order_date='2026-08-20 10:00')
    stats = import_marketplace_orders(conn, [order], 'f.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-2')

    assert _stock(conn, gated_lid) == 50               # gated: untouched
    assert _provenance(conn, oid, gated_lid) is None
    assert stats['gated_lines'] == 1

    assert _stock(conn, control_lid) == 47              # CONTROL: later-than-baseline deducts
    assert _provenance(conn, oid, control_lid) == 3


def test_reimport_is_idempotent(empty_db_conn):
    conn = empty_db_conn
    _seed_sku(conn, variation_id='V-IDEM', stock=20, stock_as_of='2026-01-01 00:00:00')

    order = _order('ORD-3', [_line('L1', variation_id='V-IDEM', qty=4.0)])
    import_marketplace_orders(conn, [order], 'f1.xlsx')
    lid = conn.execute(
        "SELECT id FROM platform_skus WHERE variation_id='V-IDEM'").fetchone()[0]
    oid = _order_id(conn, 'shopee', 'ORD-3')
    stock_after_1 = _stock(conn, lid)
    row_after_1 = _prov_rowid(conn, oid, lid)
    assert stock_after_1 == 16 and row_after_1 is not None  # vacuity guard

    import_marketplace_orders(conn, [order], 'f1.xlsx')   # re-import, identical payload

    assert _stock(conn, lid) == stock_after_1
    # Same ROWID (not merely same value) proves the provenance row was never
    # touched (delta==0 -> skip), not coincidentally rewritten to the same units.
    assert _prov_rowid(conn, oid, lid) == row_after_1


def test_qty_edit_applies_delta_only(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, variation_id='V-EDIT', stock=50, stock_as_of='2026-01-01 00:00:00')

    order = _order('ORD-4', [_line('L1', variation_id='V-EDIT', qty=2.0)])
    import_marketplace_orders(conn, [order], 'f1.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-4')
    assert _stock(conn, lid) == 48 and _provenance(conn, oid, lid) == 2  # vacuity guard

    order2 = _order('ORD-4', [_line('L1', variation_id='V-EDIT', qty=5.0)])
    import_marketplace_orders(conn, [order2], 'f1.xlsx')

    assert _stock(conn, lid) == 45     # extra 3 off (48-3), not re-deducting the full 5
    assert _provenance(conn, oid, lid) == 5


def test_cancel_credits_exactly_applied(empty_db_conn):
    conn = empty_db_conn
    assert 'returned' in RESTOCK_STATUSES  # vacuity guard on the constant
    lid = _seed_sku(conn, variation_id='V-CANCEL', stock=3, stock_as_of='2026-01-01 00:00:00')

    order = _order('ORD-5', [_line('L1', variation_id='V-CANCEL', qty=10.0)])
    import_marketplace_orders(conn, [order], 'f1.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-5')
    assert _stock(conn, lid) == 0 and _provenance(conn, oid, lid) == 3  # clamped at 3 of 10

    order2 = _order('ORD-5', [_line('L1', variation_id='V-CANCEL', qty=10.0)], status='returned')
    import_marketplace_orders(conn, [order2], 'f1.xlsx')

    assert _stock(conn, lid) == 3            # restores exactly what was applied, not the wanted 10
    assert _provenance(conn, oid, lid) is None    # provenance gone


def test_scrapped_gets_no_credit(empty_db_conn):
    conn = empty_db_conn
    assert 'returned' in RESTOCK_STATUSES and 'Package scrapped' not in RESTOCK_STATUSES
    scrapped_lid = _seed_sku(conn, variation_id='V-SCRAP', stock=3,
                             stock_as_of='2026-01-01 00:00:00')
    control_lid = _seed_sku(conn, variation_id='V-RETURN-CTRL', stock=3,
                            stock_as_of='2026-01-01 00:00:00')

    import_marketplace_orders(
        conn, [_order('ORD-6', [_line('L1', variation_id='V-SCRAP', qty=10.0)])], 'f1.xlsx')
    import_marketplace_orders(
        conn, [_order('ORD-7', [_line('L1', variation_id='V-RETURN-CTRL', qty=10.0)])], 'f1.xlsx')
    oid1 = _order_id(conn, 'shopee', 'ORD-6')
    oid2 = _order_id(conn, 'shopee', 'ORD-7')
    assert _stock(conn, scrapped_lid) == 0 and _provenance(conn, oid1, scrapped_lid) == 3
    assert _stock(conn, control_lid) == 0 and _provenance(conn, oid2, control_lid) == 3  # vacuity guard

    import_marketplace_orders(conn, [_order(
        'ORD-6', [_line('L1', variation_id='V-SCRAP', qty=10.0)],
        status='Package scrapped')], 'f1.xlsx')
    assert _stock(conn, scrapped_lid) == 0              # unchanged — no credit
    assert _provenance(conn, oid1, scrapped_lid) == 3   # still applied, not reversed

    import_marketplace_orders(conn, [_order(
        'ORD-7', [_line('L1', variation_id='V-RETURN-CTRL', qty=10.0)],
        status='returned')], 'f1.xlsx')
    assert _stock(conn, control_lid) == 3               # CONTROL: a restock status DOES credit
    assert _provenance(conn, oid2, control_lid) is None


def test_unknown_status_deducts(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, variation_id='V-UNK', stock=50, stock_as_of='2026-01-01 00:00:00')
    novel_status = 'some-brand-new-status-2099'
    assert novel_status not in RESTOCK_STATUSES  # vacuity guard

    order = _order('ORD-8', [_line('L1', variation_id='V-UNK', qty=4.0)], status=novel_status)
    import_marketplace_orders(conn, [order], 'f.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-8')

    assert _stock(conn, lid) == 46
    assert _provenance(conn, oid, lid) == 4


def test_null_stock_listing_untouched(empty_db_conn):
    conn = empty_db_conn
    null_lid = _seed_sku(conn, variation_id='V-NULL', stock=None,
                         stock_as_of='2026-01-01 00:00:00')
    control_lid = _seed_sku(conn, variation_id='V-NULL-CTRL', stock=10,
                            stock_as_of='2026-01-01 00:00:00')
    assert _stock(conn, null_lid) is None  # vacuity guard

    order = _order('ORD-9', [
        _line('L1', variation_id='V-NULL', qty=3.0),
        _line('L2', variation_id='V-NULL-CTRL', qty=3.0),
    ])
    import_marketplace_orders(conn, [order], 'f.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-9')

    assert _stock(conn, null_lid) is None
    assert _provenance(conn, oid, null_lid) is None

    assert _stock(conn, control_lid) == 7        # CONTROL: machinery fires on the sibling
    assert _provenance(conn, oid, control_lid) == 3


def test_unresolvable_line_skipped_counted(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, variation_id='V-RESOLVABLE', stock=10,
                    stock_as_of='2026-01-01 00:00:00')

    order = _order('ORD-10', [
        _line('L1', variation_id='V-DOES-NOT-EXIST', qty=1.0),
        _line('L2', variation_id='V-RESOLVABLE', qty=2.0),
    ])
    stats = import_marketplace_orders(conn, [order], 'f.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-10')

    assert stats['skipped_lines'] == 1
    assert _stock(conn, lid) == 8              # CONTROL: resolvable sibling deducts
    assert _provenance(conn, oid, lid) == 2


def test_two_lines_same_listing_aggregate(empty_db_conn):
    conn = empty_db_conn
    lid = _seed_sku(conn, variation_id='V-AGG', stock=20, stock_as_of='2026-01-01 00:00:00')

    order = _order('ORD-11', [
        _line('L1', variation_id='V-AGG', qty=2.0),
        _line('L2', variation_id='V-AGG', qty=3.0),
    ])
    import_marketplace_orders(conn, [order], 'f.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-11')

    assert _stock(conn, lid) == 15             # 2+3 deducted once, from ONE aggregated diff
    n_rows = conn.execute(
        "SELECT COUNT(*) FROM platform_stock_deductions WHERE source_table='marketplace_orders'"
        " AND source_id=? AND platform_sku_id=?", (oid, lid)).fetchone()[0]
    assert n_rows == 1                          # one provenance row, not two
    assert _provenance(conn, oid, lid) == 5


def test_fractional_qty_skipped(empty_db_conn):
    """qty cast rule: provenance units is INTEGER, marketplace_order_items.qty
    is REAL. A non-integer qty is a parse bug — skip + count rather than
    silently floor it into the stock ledger."""
    conn = empty_db_conn
    lid = _seed_sku(conn, variation_id='V-FRAC', stock=10, stock_as_of='2026-01-01 00:00:00')

    order = _order('ORD-12', [_line('L1', variation_id='V-FRAC', qty=2.5)])
    stats = import_marketplace_orders(conn, [order], 'f.xlsx')
    oid = _order_id(conn, 'shopee', 'ORD-12')

    assert stats['skipped_lines'] == 1
    assert _stock(conn, lid) == 10       # untouched
    assert _provenance(conn, oid, lid) is None


# ── Concurrency: the lock must span the read AND the write ─────────────────

def test_import_holds_lock_before_first_write(empty_db, empty_db_conn, monkeypatch):
    """BEGIN IMMEDIATE must be the very first statement of
    import_marketplace_orders — inject the interleaving writer from
    _order_header_values, the function called strictly before the header
    UPSERT (the first write in the whole import). A seam placed after the
    first write would prove nothing either way
    (.claude/rules/erp-engineering-discipline.md, concurrency-test rule)."""
    conn = empty_db_conn
    seen = {}
    real_fn = marketplace_mod._order_header_values

    def _spy(*args, **kwargs):
        if not seen:
            seen['fired'] = True
            probe = sqlite3.connect(empty_db, timeout=0.1)
            try:
                with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                    probe.execute(
                        "INSERT INTO marketplace_orders (platform, order_sn, status) "
                        "VALUES ('shopee','INTRUDER','x')")
            finally:
                probe.close()
            seen['locked'] = True
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(marketplace_mod, '_order_header_values', _spy)

    order = _order('ORD-CONC', [_line('L1', variation_id='V-CONC', qty=1.0)])
    stats = import_marketplace_orders(conn, [order], 'f.xlsx')

    assert seen.get('locked') is True
    assert stats['orders'] == 1
