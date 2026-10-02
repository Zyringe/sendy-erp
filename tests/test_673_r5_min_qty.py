"""#673 — ตรวจบิล R5 (promo mismatch) with a minimum-quantity promo.

Below the minimum the line is EXPECTED at list price: a list-price line is not
"ไม่ได้ใช้โปร?". A line that got the promo discount anyway is its own finding,
"ให้ส่วนลดโปรแต่ไม่ถึงขั้นต่ำ N <unit>", and a price tier that happens to equal
the promo price does not excuse it (Sol, /interrogate 2026-10-02). At or above
the minimum R5 behaves as before.

The line quantity is measured through the same unit chain as the resolver
(price_lookup.promo_min_measure). Real DB clone (`tmp_db_conn`) with mig 199;
every product and promo is forced by the test.
"""
import os

import pytest

import review_rules

MIG_199 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'migrations', '199_promo_min_qty.sql')
_pid = [977000]
_doc = [9977000]


@pytest.fixture
def db(tmp_db_conn):
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'min_qty' not in cols:
        tmp_db_conn.executescript(open(MIG_199, encoding='utf-8').read())
        tmp_db_conn.commit()
    return tmp_db_conn


def _product(conn, *, rows=(), tiers=()):
    _pid[0] += 1
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, 'อัน', 100, 60, 1)", (f'r5 min #{_pid[0]}',)).lastrowid
    for unit, ratio in rows:
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                     (pid, unit, ratio))
    for label, price in tiers:
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                     (pid, label, price))
    conn.commit()
    return pid


def _promo(conn, pid, promo_type='percent', discount_value=5.0, *, min_qty=None,
           min_qty_unit=None, bundle_buy=None, bundle_free=None):
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, is_active, "
        "min_qty, min_qty_unit, bundle_buy, bundle_free) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (pid, f'โปร {promo_type}', promo_type, discount_value, min_qty, min_qty_unit,
         bundle_buy, bundle_free))
    conn.commit()


def _line(pid, qty, unit_price, unit='อัน'):
    _doc[0] += 1
    total = round(qty * unit_price, 2)
    return {
        'product_id': pid, 'unit': unit, 'unit_price': unit_price, 'qty': qty,
        'net': total, 'total': total, 'bsn_code': 'X', 'product_name_raw': 'x',
        'ref_invoice': '', 'date_iso': '2026-10-01', 'customer_code': 'TST673R5',
        'doc_no': f'IV{_doc[0]}-1', 'doc_base': f'IV{_doc[0]}',
    }


def _r5(conn, line):
    return [f for f in review_rules._check_row_rules(conn, line)
            if f['rule_code'] == 'R5_PROMO_MISMATCH']


def test_below_minimum_at_list_price_is_not_flagged(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    assert _r5(db, _line(pid, 19, 100.0)) == []


def test_at_minimum_at_list_price_is_flagged(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    flags = _r5(db, _line(pid, 20, 100.0))
    assert len(flags) == 1
    assert 'ไม่ได้ใช้โปร' in flags[0]['message_th']


def test_below_minimum_at_promo_price_is_flagged_with_its_own_message(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    flags = _r5(db, _line(pid, 19, 95.0))
    assert len(flags) == 1
    assert 'ให้ส่วนลดโปรแต่ไม่ถึงขั้นต่ำ 20 อัน' in flags[0]['message_th']


def test_a_matching_tier_does_not_excuse_a_below_minimum_discount(db):
    # A tier in ANOTHER unit whose price happens to equal the promo price. (A
    # '10 อัน' tier would be the canonical list of an อัน line itself, the way
    # the resolver reads a bare ask — fix round, item 3.)
    pid = _product(db, tiers=[('1 กล่อง', 95.0)])
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    flags = _r5(db, _line(pid, 19, 95.0))
    assert len(flags) == 1
    assert 'ไม่ถึงขั้นต่ำ' in flags[0]['message_th']
    # control: at the minimum the same price IS the promo price — clean
    assert _r5(db, _line(pid, 20, 95.0)) == []


def test_at_minimum_at_promo_price_is_clean(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    assert _r5(db, _line(pid, 20, 95.0)) == []


def test_minimum_measured_through_the_line_unit(db):
    """2 โหล = 24 อัน meets a 20 อัน minimum; 1 โหล = 12 does not."""
    pid = _product(db, rows=[('โหล', 12.0)])
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    assert len(_r5(db, _line(pid, 2, 1200.0, unit='โหล'))) == 1          # met, list → flag
    assert _r5(db, _line(pid, 1, 1200.0, unit='โหล')) == []               # not met, list → clean


def test_ungated_promo_unchanged(db):
    pid = _product(db)
    _promo(db, pid)
    assert len(_r5(db, _line(pid, 1, 100.0))) == 1
    assert _r5(db, _line(pid, 1, 95.0)) == []


def test_r5_reads_the_price_slot_not_the_newest_row(db):
    """A newer bundle (qty-slot) promo must not hide the older percent promo
    from R5 — the same slot-aware selection the resolver uses
    (get_active_promos_by_class). Behaviour change, its own commit."""
    pid = _product(db)
    _promo(db, pid)                                                  # percent 5, older
    _promo(db, pid, 'bundle', None, bundle_buy=10, bundle_free=1)    # newer, qty slot
    flags = _r5(db, _line(pid, 1, 100.0))
    assert len(flags) == 1
    assert 'โปร percent' in flags[0]['message_th']
    assert _r5(db, _line(pid, 1, 95.0)) == []                        # control


# ── fix round (/interrogate on the PR 1 diff, item 3) ────────────────────────
# R5 prices the line from the CANONICAL list of the sold unit (a tier first,
# as the resolver does), flags ANY below-list discount on a below-minimum
# line, and still runs when R4's exact-ratio path misses (tier-implied โหล).

def _product_x66(conn):
    """pid 1102's shape: base 54.17/ตัว (×12 = 650.04), a '1 โหล' tier at 670."""
    _pid[0] += 1
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, 'ตัว', 54.17, 30, 1)", (f'r5 x66 #{_pid[0]}',)).lastrowid
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'โหล', 12)",
                 (pid,))
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
                 "VALUES (?, '1 โหล', 670)", (pid,))
    conn.commit()
    return pid


def test_tier_list_below_minimum_promo_price_is_flagged(db):
    """Opus e8: 1 โหล at 649.90 (670 − 3%) below a 3 โหล minimum."""
    pid = _product_x66(db)
    _promo(db, pid, discount_value=3.0, min_qty=3, min_qty_unit='โหล')
    flags = _r5(db, _line(pid, 1, 649.90, unit='โหล'))
    assert len(flags) == 1 and 'ไม่ถึงขั้นต่ำ 3 โหล' in flags[0]['message_th']
    assert _r5(db, _line(pid, 1, 670.0, unit='โหล')) == []                 # list: clean


def test_tier_list_met_minimum_at_list_says_promo_not_used(db):
    """Opus e8: 3 โหล at the tier list 670 met the minimum — the tier is the
    list, not an excuse."""
    pid = _product_x66(db)
    _promo(db, pid, discount_value=3.0, min_qty=3, min_qty_unit='โหล')
    flags = _r5(db, _line(pid, 3, 670.0, unit='โหล'))
    assert len(flags) == 1 and 'ไม่ได้ใช้โปร' in flags[0]['message_th']
    assert _r5(db, _line(pid, 3, 649.90, unit='โหล')) == []                # promo: clean


def test_any_below_list_discount_below_minimum_is_flagged(db):
    """Fable d: 5 pieces at 93 (list 100, promo would be 95)."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    flags = _r5(db, _line(pid, 5, 93.0))
    assert len(flags) == 1 and 'ไม่ถึงขั้นต่ำ 20 อัน' in flags[0]['message_th']


def test_r5_runs_on_a_tier_implied_dozen_line(db):
    """No โหล row (R4 fires: stock will not cut), but the '1 โหล' tier makes
    the dozen measurable — R5 must still judge the promo."""
    _pid[0] += 1
    pid = db.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, 'ดอก', 10, 4, 1)", (f'r5 dozen #{_pid[0]}',)).lastrowid
    db.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
               "VALUES (?, '1 โหล', 120)", (pid,))
    db.commit()
    _promo(db, pid, discount_value=25.0, min_qty=5, min_qty_unit='โหล')
    rules = {f['rule_code'] for f in review_rules._check_row_rules(db, _line(pid, 1, 90.0, unit='โหล'))}
    assert 'R4_UNUSUAL_UNIT' in rules                                         # control
    assert 'ไม่ถึงขั้นต่ำ 5 โหล' in _r5(db, _line(pid, 1, 90.0, unit='โหล'))[0]['message_th']
    assert 'ไม่ได้ใช้โปร' in _r5(db, _line(pid, 5, 120.0, unit='โหล'))[0]['message_th']
    assert _r5(db, _line(pid, 5, 90.0, unit='โหล')) == []


# ── re-review on d0db6aa, finding 5: an unmeasurable line unit says so ───────

def test_unmeasurable_line_unit_gets_its_own_text(db):
    """A 'กล่อง' line priced at its own tier but with no ratio: the line HAS a
    quantity, its unit just cannot be compared with a 5 โหล minimum."""
    pid = _product(db, rows=[('โหล', 12.0)], tiers=[('1 กล่อง', 500.0)])
    _promo(db, pid, min_qty=5, min_qty_unit='โหล')
    flags = _r5(db, _line(pid, 1, 400.0, unit='กล่อง'))
    assert len(flags) == 1
    msg = flags[0]['message_th']
    assert 'แปลงเป็นชิ้นไม่ได้' in msg and '5 โหล' in msg
    assert 'ยังไม่ระบุจำนวน' not in msg
