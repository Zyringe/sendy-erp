"""TDD for scripts/2026_09_19_gross_to_piece.py — base unit กุรุส -> piece.

Written BEFORE the script (erp-engineering-discipline: anything mutating
`transactions` / `stock_levels` starts from a failing test).

The shape under test is pid 1320's real one on prod, which is why it is the
fixture: sales arrive in TWO units for the same physical quantity — `ตัว`
(actually a gross) at qty 0.5/1.0, and `แท่ง` (the piece) at qty 72 — while
purchases arrive in `กร` (gross). With `แท่ง = 1.0` the ledger posts -0.5 for
one and -72 for the other although both are half a gross, and a back-solved
opening ADJUST hides the difference.

⚠ The conversion is NOT a blanket `quantity_change * 144`. Each ledger row is
re-derived from its own source bill's unit, so the two spellings converge on the
same number. That convergence is the whole point and it is what test
`test_both_bill_units_converge` pins.
"""
import importlib.util
import pathlib
import sqlite3

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_19_gross_to_piece.py"
_spec = importlib.util.spec_from_file_location("gross_to_piece", _SCRIPT)
gross_to_piece = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gross_to_piece)
rebase = gross_to_piece.rebase
RebaseRefused = gross_to_piece.RebaseRefused

RATIO = 144


def _seed(conn, pid=1320):
    """A 1320-shaped product: mixed-unit sales, gross purchases, plug opening."""
    conn.executescript("""
        INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,
                              opening_cost, low_stock_threshold, is_active)
        VALUES (1320, 'ดินสอช่างไม้พระจันทร์แท้', 'ตัว', 989.4, 1250.0, 989.4, 5, 1);
        INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES
            (1320, 'กร', 1.0), (1320, 'ตัว', 1.0), (1320, 'แท่ง', 1.0);
        INSERT INTO product_price_tiers (product_id, qty_label, price)
        VALUES (1320, '1 กุรุส', 1250.0);
    """)
    # one gross-unit sale, one piece-unit sale: SAME physical quantity
    conn.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id,"
                 " qty, unit, unit_price, net, vat_type, synced_to_stock)"
                 " VALUES ('2025-02-13','IV6800448-6','IV6800448',?,0.5,'ตัว',1250.0,625.0,0,1)", (pid,))
    conn.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id,"
                 " qty, unit, unit_price, net, vat_type, synced_to_stock)"
                 " VALUES ('2025-05-16','IV6801282-4','IV6801282',?,72.0,'แท่ง',8.68,624.96,0,1)", (pid,))
    conn.execute("INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id,"
                 " qty, unit, unit_price, net, synced_to_stock)"
                 " VALUES ('2025-02-12','RR6800080','RR6800080',?,0.5,'กร',1020.0,494.7,1)", (pid,))
    conn.commit()


def _stock(conn, pid=1320):
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return row[0] if row else 0


@pytest.fixture()
def db(empty_db_conn):
    _seed(empty_db_conn)
    return empty_db_conn


def test_both_bill_units_converge_on_the_same_piece_quantity(db):
    """THE point of the rebase: 0.5 ตัว and 72 แท่ง are the same physical amount.

    Before: the ledger posts -0.5 and -72 for identical quantities.
    After:  both post -72.
    """
    rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
    legs = {r['reference_no']: r['quantity_change'] for r in db.execute(
        "SELECT reference_no, quantity_change FROM transactions"
        " WHERE product_id=1320 AND txn_type='OUT'")}
    assert len(legs) == 2, legs
    assert legs['IV6800448-6'] == pytest.approx(-72.0)
    assert legs['IV6801282-4'] == pytest.approx(-72.0)


def test_stock_is_preserved_not_multiplied(db):
    """Put's contract 2026-09-19: preserve the physical quantity.

    The count assertion comes first — a property over an empty ledger pins
    nothing (verification-discipline, the empty-collection trap).
    """
    rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
    n = db.execute("SELECT COUNT(*) FROM transactions WHERE product_id=1320").fetchone()[0]
    assert n >= 3, "ledger was not rebuilt"
    assert _stock(db) == pytest.approx(0.0)


def test_purchase_leg_scales_by_the_gross_ratio(db):
    rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
    qty = db.execute("SELECT quantity_change FROM transactions"
                     " WHERE product_id=1320 AND txn_type='IN'").fetchone()[0]
    assert qty == pytest.approx(72.0), "0.5 กร must become 72 แท่ง"


def test_unit_type_and_ratios_after(db):
    rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
    assert db.execute("SELECT unit_type FROM products WHERE id=1320").fetchone()[0] == 'แท่ง'
    ratios = dict(db.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=1320"))
    assert ratios['แท่ง'] == pytest.approx(1.0), "the new base unit is 1.0"
    assert ratios['กร'] == pytest.approx(144.0)
    assert ratios['ตัว'] == pytest.approx(144.0), "bills written at ตัว mean a gross, not a piece"


def test_stock_value_is_conserved(db):
    """cost divides EXACTLY (Put's batch-1 convention) so stock value cannot move."""
    before = _stock(db) * db.execute("SELECT cost_price FROM products WHERE id=1320").fetchone()[0]
    rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
    after = _stock(db) * db.execute("SELECT cost_price FROM products WHERE id=1320").fetchone()[0]
    assert after == pytest.approx(before, abs=0.005)
    cost = db.execute("SELECT cost_price FROM products WHERE id=1320").fetchone()[0]
    assert cost == pytest.approx(989.4 / 144), "cost must divide exactly, never rounded"


def test_tier_price_is_untouched(db):
    """A tier price is the PACK TOTAL — a gross still costs ฿1,250."""
    rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
    label, price = db.execute("SELECT qty_label, price FROM product_price_tiers"
                              " WHERE product_id=1320").fetchone()
    assert (label, price) == ('1 กุรุส', 1250.0)


def test_refuses_to_convert_twice(db):
    rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
    with pytest.raises(RebaseRefused):
        rebase(db, pid=1320, new_unit='แท่ง', ratio=RATIO, new_base_sell=8.69, preserve_stock=0)
