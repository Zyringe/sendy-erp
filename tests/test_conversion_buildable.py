"""Tests for models.get_buildable — the pack/unpack 'true availability' helper.

buildable(P) = sum over ACTIVE formulas whose output is P of
    (min over inputs of floor(stock(input) / input.qty)) * output_qty
true_available(P) = stock(P) + buildable(P)

One level deep (no recursion). Seeds a clean-schema DB directly.
"""
import models


def _seed_product(conn, pid, name, unit="ตัว"):
    conn.execute(
        "INSERT INTO products(id, sku, product_name, unit_type) VALUES (?,?,?,?)",
        (pid, pid, name, unit),
    )


def _set_stock(conn, pid, qty):
    conn.execute(
        "INSERT INTO stock_levels(product_id, quantity) VALUES (?,?) "
        "ON CONFLICT(product_id) DO UPDATE SET quantity=excluded.quantity",
        (pid, qty),
    )


def _formula(conn, name, output_pid, output_qty, inputs, is_active=1):
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active) VALUES (?,?,?,?)",
        (name, output_pid, output_qty, is_active),
    )
    fid = cur.lastrowid
    for ipid, iqty in inputs:
        conn.execute(
            "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
            (fid, ipid, iqty),
        )
    return fid


def test_unpack_buildable(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack"); _seed_product(c, 20, "loose")
    _set_stock(c, 10, 5)
    _formula(c, "unpack", output_pid=20, output_qty=2, inputs=[(10, 1)])
    c.commit()
    res = models.get_buildable([20], conn=c)
    assert res[20]["buildable"] == 10  # floor(5/1) * 2


def test_pack_buildable(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack"); _seed_product(c, 20, "loose")
    _set_stock(c, 20, 7)
    _formula(c, "pack", output_pid=10, output_qty=1, inputs=[(20, 2)])
    c.commit()
    res = models.get_buildable([10], conn=c)
    assert res[10]["buildable"] == 3  # floor(7/2) * 1


def test_multi_input_chud_min_binds(empty_db_conn):
    c = empty_db_conn
    for pid in (30, 40, 41):
        _seed_product(c, pid, f"p{pid}")
    _set_stock(c, 40, 10); _set_stock(c, 41, 12)
    _formula(c, "chud", output_pid=30, output_qty=1, inputs=[(40, 2), (41, 3)])
    c.commit()
    res = models.get_buildable([30], conn=c)
    assert res[30]["buildable"] == 4  # min(floor(10/2)=5, floor(12/3)=4)


def test_zero_input_stock_gives_zero_but_key_present(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 60, "out"); _seed_product(c, 61, "in")
    _set_stock(c, 61, 0)
    _formula(c, "z", output_pid=60, output_qty=5, inputs=[(61, 1)])
    c.commit()
    res = models.get_buildable([60], conn=c)
    assert res[60]["buildable"] == 0  # product IS a formula output → key present, buildable 0


def test_two_formulas_same_output_sum(empty_db_conn):
    c = empty_db_conn
    for pid in (50, 51, 52):
        _seed_product(c, pid, f"p{pid}")
    _set_stock(c, 51, 4); _set_stock(c, 52, 6)
    _formula(c, "A", output_pid=50, output_qty=1, inputs=[(51, 1)])
    _formula(c, "B", output_pid=50, output_qty=1, inputs=[(52, 1)])
    c.commit()
    res = models.get_buildable([50], conn=c)
    assert res[50]["buildable"] == 10
    assert len(res[50]["sources"]) == 2


def test_inactive_formula_ignored(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 70, "out"); _seed_product(c, 71, "in")
    _set_stock(c, 71, 100)
    _formula(c, "dead", output_pid=70, output_qty=1, inputs=[(71, 1)], is_active=0)
    c.commit()
    res = models.get_buildable([70], conn=c)
    assert 70 not in res  # no active formula → not in result


def test_product_ids_filter(empty_db_conn):
    c = empty_db_conn
    for pid in (10, 20, 30, 40):
        _seed_product(c, pid, f"p{pid}")
    _set_stock(c, 10, 5); _set_stock(c, 40, 8)
    _formula(c, "f1", output_pid=20, output_qty=1, inputs=[(10, 1)])
    _formula(c, "f2", output_pid=30, output_qty=1, inputs=[(40, 1)])
    c.commit()
    res = models.get_buildable([20], conn=c)
    assert set(res.keys()) == {20}


def test_true_available_is_stock_plus_buildable(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack"); _seed_product(c, 20, "loose")
    _set_stock(c, 10, 5)   # pack stock (input for unpack)
    _set_stock(c, 20, 3)   # loose's own stock
    _formula(c, "unpack", output_pid=20, output_qty=2, inputs=[(10, 1)])
    c.commit()
    res = models.get_buildable([20], conn=c)
    assert res[20]["output_stock"] == 3
    assert res[20]["true_available"] == 13  # 3 + 10


def test_float_noise_absorbed(empty_db_conn):
    # trigger-maintained REAL stock can read as 5.9999999999999 for a true 6
    c = empty_db_conn
    _seed_product(c, 10, "pack"); _seed_product(c, 20, "loose")
    _set_stock(c, 10, 6 - 1e-13)
    _formula(c, "unpack", output_pid=20, output_qty=1, inputs=[(10, 1)])
    c.commit()
    res = models.get_buildable([20], conn=c)
    assert res[20]["buildable"] == 6  # not 5


def test_all_outputs_when_no_filter(empty_db_conn):
    c = empty_db_conn
    for pid in (10, 20, 30, 40):
        _seed_product(c, pid, f"p{pid}")
    _set_stock(c, 10, 5); _set_stock(c, 40, 8)
    _formula(c, "f1", output_pid=20, output_qty=1, inputs=[(10, 1)])
    _formula(c, "f2", output_pid=30, output_qty=1, inputs=[(40, 1)])
    c.commit()
    res = models.get_buildable(conn=c)  # no filter → all active-formula outputs
    assert {20, 30} <= set(res.keys())
