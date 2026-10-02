"""#673 — scripts/import_catalog_pricing.py and a promo's minimum quantity.

The catalogue CSV gains `min_qty` + `min_qty_unit` (the unit read through the
unit map like `bundle_unit`). The minimum belongs to the row's PRICE-slot
intent and is part of the offer identity, so a re-import of the same file is
still all-zero and a changed minimum is a close + insert. RunAbort, before any
write (/interrogate item 6):
  - a live price occupant carries a minimum and the row's price intent has none
    (a re-import would otherwise silently WIPE the minimum);
  - a price intent carries a ยกลัง/ยกล่อง label and no minimum;
  - a minimum on a row with no price intent, a number that is not > 0, or a
    number without a unit;
  - a unit with no ratio for that product (the message lists the ones that
    resolve).
Every product is fresh; the DB is a clone (`tmp_db`).
"""
import csv
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import import_catalog_pricing as imp  # noqa: E402

BATCH = "2026-10-02"
FIELDS = ["product_id", "sku_code", "special_price", "promo_type", "promo_value",
          "bundle_buy", "bundle_free", "bundle_unit", "bundle_condition",
          "min_qty", "min_qty_unit"]
_pid = [981000]


def _product(db, rows=(('โหล', 12.0),)):
    _pid[0] += 1
    c = sqlite3.connect(db)
    pid = c.execute("INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
                    "is_active) VALUES (?, 'อัน', 100, 60, 1)",
                    (f'import673 #{_pid[0]}',)).lastrowid
    for u, r in rows:
        c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                  (pid, u, r))
    c.commit()
    c.close()
    return pid


def _csv(tmp_path, rows):
    p = tmp_path / "cat673.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return p


def _run(path, db, commit=True):
    return imp.run_import(path, Path(db), commit=commit, limit=None, show_sample=0,
                          verbose=False, batch_date=BATCH)


def _promos(db, pid):
    c = sqlite3.connect(db)
    rows = c.execute("SELECT promo_type, discount_value, min_qty, min_qty_unit, date_end "
                     "FROM promotions WHERE product_id = ? ORDER BY id", (pid,)).fetchall()
    c.close()
    return rows


def _row(pid, **kw):
    return {"product_id": str(pid), "sku_code": "S", "promo_type": "percent",
            "promo_value": "5", **kw}


def test_minimum_is_imported_as_a_word(tmp_db, tmp_path):
    pid = _product(tmp_db)
    stats = _run(_csv(tmp_path, [_row(pid, min_qty="3", min_qty_unit="หล")]), tmp_db)
    assert stats["promos_inserted"] == 1
    assert _promos(tmp_db, pid) == [("percent", 5.0, 3.0, "โหล", None)]


def test_reimport_of_the_same_minimum_is_all_zero(tmp_db, tmp_path):
    pid = _product(tmp_db)
    path = _csv(tmp_path, [_row(pid, min_qty="20", min_qty_unit="อัน")])
    _run(path, tmp_db)
    before = _promos(tmp_db, pid)
    stats = _run(path, tmp_db)
    assert (stats["promos_inserted"], stats["promos_closed"]) == (0, 0)
    assert _promos(tmp_db, pid) == before


def test_a_changed_minimum_closes_and_inserts(tmp_db, tmp_path):
    pid = _product(tmp_db)
    _run(_csv(tmp_path, [_row(pid, min_qty="20", min_qty_unit="อัน")]), tmp_db)
    stats = imp.run_import(_csv(tmp_path, [_row(pid, min_qty="10", min_qty_unit="อัน")]),
                           Path(tmp_db), commit=True, limit=None, show_sample=0,
                           verbose=False, batch_date="2026-10-03")
    assert (stats["promos_inserted"], stats["promos_closed"]) == (1, 1)
    assert [r[2] for r in _promos(tmp_db, pid)] == [20.0, 10.0]


def _refused(tmp_db, tmp_path, pid, row, needle):
    before = _promos(tmp_db, pid)
    with pytest.raises(imp.RunAbort) as exc:
        _run(_csv(tmp_path, [row]), tmp_db)
    assert needle in str(exc.value)
    assert str(pid) in str(exc.value)
    assert _promos(tmp_db, pid) == before


def test_reimport_without_the_minimum_is_refused_not_a_silent_wipe(tmp_db, tmp_path):
    pid = _product(tmp_db)
    _run(_csv(tmp_path, [_row(pid, min_qty="20", min_qty_unit="อัน")]), tmp_db)
    assert len(_promos(tmp_db, pid)) == 1          # control: the occupant exists
    _refused(tmp_db, tmp_path, pid, _row(pid, promo_value="7"), "ขั้นต่ำ")


def test_label_without_minimum_is_refused(tmp_db, tmp_path):
    pid = _product(tmp_db)
    _refused(tmp_db, tmp_path, pid, _row(pid, bundle_condition="ยกลัง"), "ยกลัง")


def test_label_with_minimum_is_accepted(tmp_db, tmp_path):
    """CONTROL for the refusal above."""
    pid = _product(tmp_db)
    _run(_csv(tmp_path, [_row(pid, bundle_condition="ยกลัง", min_qty="20",
                              min_qty_unit="อัน")]), tmp_db)
    assert _promos(tmp_db, pid) == [("percent", 5.0, 20.0, "อัน", None)]


@pytest.mark.parametrize("extra,needle", [
    (dict(min_qty="20"), "ขั้นต่ำ"),
    (dict(min_qty_unit="อัน"), "ขั้นต่ำ"),
    (dict(min_qty="0", min_qty_unit="อัน"), "> 0"),
    (dict(min_qty="-2", min_qty_unit="อัน"), "> 0"),
], ids=["qty without unit", "unit without qty", "zero", "negative"])
def test_bad_minimum_shapes_are_refused(tmp_db, tmp_path, extra, needle):
    pid = _product(tmp_db)
    _refused(tmp_db, tmp_path, pid, _row(pid, **extra), needle)


def test_minimum_on_a_row_with_no_price_promo_is_refused(tmp_db, tmp_path):
    pid = _product(tmp_db)
    row = {"product_id": str(pid), "sku_code": "S", "promo_type": "bundle",
           "bundle_buy": "10", "bundle_free": "1", "min_qty": "20", "min_qty_unit": "อัน"}
    _refused(tmp_db, tmp_path, pid, row, "ราคา")


def test_unit_with_no_ratio_is_refused_listing_the_ones_that_resolve(tmp_db, tmp_path):
    pid = _product(tmp_db)
    before = _promos(tmp_db, pid)
    with pytest.raises(imp.RunAbort) as exc:
        _run(_csv(tmp_path, [_row(pid, min_qty="1", min_qty_unit="ลัง")]), tmp_db)
    msg = str(exc.value)
    assert "ลัง" in msg and "อัน" in msg and "โหล" in msg
    assert _promos(tmp_db, pid) == before


def test_special_price_row_carries_the_minimum(tmp_db, tmp_path):
    pid = _product(tmp_db)
    _run(_csv(tmp_path, [{"product_id": str(pid), "sku_code": "S", "special_price": "80",
                          "min_qty": "2", "min_qty_unit": "โหล"}]), tmp_db)
    assert _promos(tmp_db, pid) == [("fixed", 80.0, 2.0, "โหล", None)]
