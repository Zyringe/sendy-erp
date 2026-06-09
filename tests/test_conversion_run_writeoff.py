"""run_conversion write-off (ของเสีย / yield loss) tests.

A run like 10 แผง → 20 ตัว may produce a broken ตัว; the operator writes it off so
only the GOOD units enter stock. Inputs are still fully consumed; the broken
units never enter stock_levels; input cost spreads over the good units.
"""
import models


def _seed_product(conn, pid, name, unit="ตัว"):
    conn.execute("INSERT INTO products(id, sku, product_name, unit_type) VALUES (?,?,?,?)",
                 (pid, pid, name, unit))


def _stock_in(conn, pid, qty):
    # seed stock via a transaction so stock_levels stays trigger-consistent
    conn.execute("INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
                 " VALUES (?,?,?,?,?,?)", (pid, 'IN', qty, 'unit', 'SEED', 'seed'))


def _formula(conn, name, output_pid, output_qty, inputs):
    cur = conn.execute("INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active)"
                       " VALUES (?,?,?,1)", (name, output_pid, output_qty))
    fid = cur.lastrowid
    for ipid, iqty in inputs:
        conn.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
                     (fid, ipid, iqty))
    return fid


def _stock(conn, pid):
    r = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return r["quantity"] if r else 0


def _setup(conn):
    _seed_product(conn, 10, "pack", "แผง"); _seed_product(conn, 20, "loose", "ตัว")
    _stock_in(conn, 10, 5)
    fid = _formula(conn, "unpack", output_pid=20, output_qty=2, inputs=[(10, 1)])
    conn.commit()
    return fid


def test_writeoff_reduces_good_output(empty_db_conn):
    c = empty_db_conn
    fid = _setup(c)
    ok, msg, details = models.run_conversion(fid, multiplier=3, writeoff_qty=1)
    assert ok, msg
    # expected 6, writeoff 1 → 5 good enter stock; inputs fully consumed (3 used)
    assert _stock(c, 10) == 2     # 5 - 3
    assert _stock(c, 20) == 5     # 0 + 5 good (NOT 6)


def test_writeoff_logged_and_cost_spreads_over_good(empty_db_conn):
    c = empty_db_conn
    fid = _setup(c)
    models.run_conversion(fid, multiplier=3, writeoff_qty=1)
    row = c.execute("SELECT output_qty, writeoff_qty FROM conversion_cost_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["output_qty"] == 5
    assert row["writeoff_qty"] == 1


def test_writeoff_zero_is_unchanged_behavior(empty_db_conn):
    c = empty_db_conn
    fid = _setup(c)
    ok, msg, _ = models.run_conversion(fid, multiplier=2, writeoff_qty=0)
    assert ok
    assert _stock(c, 10) == 3     # 5 - 2
    assert _stock(c, 20) == 4     # full 4, no write-off
    row = c.execute("SELECT output_qty, writeoff_qty FROM conversion_cost_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["output_qty"] == 4 and row["writeoff_qty"] == 0


def test_writeoff_exceeding_output_rejected(empty_db_conn):
    c = empty_db_conn
    fid = _setup(c)
    ok, msg, _ = models.run_conversion(fid, multiplier=1, writeoff_qty=5)  # expected only 2
    assert not ok
    assert "ของเสีย" in msg or "เกิน" in msg
    assert _stock(c, 10) == 5 and _stock(c, 20) == 0   # nothing moved


def test_total_loss_consumes_inputs_no_output(empty_db_conn):
    c = empty_db_conn
    fid = _setup(c)
    ok, msg, _ = models.run_conversion(fid, multiplier=1, writeoff_qty=2)  # all 2 broke
    assert ok
    assert _stock(c, 10) == 4     # 1 แผง consumed
    assert _stock(c, 20) == 0     # 0 good entered
    row = c.execute("SELECT output_qty, writeoff_qty FROM conversion_cost_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["output_qty"] == 0 and row["writeoff_qty"] == 2
