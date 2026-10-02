"""#673 — resolve_price with a minimum-quantity price promo.

What the plan fixes (projects/promo-min-qty/promo-min-qty-plan.md, "Decisions
already taken" + "/interrogate verdict"):
  - `list_after_promo` IS the gated number; `list.price_promo` stays the offered
    promo (display), `list.price_promo_in_effect` is the one priced.
  - flags promo_min_not_met / promo_min_unconvertible / promo_min_missing /
    last_paid_was_min_promo; the breadcrumb names the minimum.
  - `qty=None` = no quantity → `qty_unknown`; explicit qty 1 stays `not_met`.
  - last_paid (Put A): ask below the minimum + the in-window last bill MET it →
    the list answers, flagged; an unrelated last bill still answers.
  - evidence window (Put A): a gated promo starts a price epoch only for asks
    that meet its minimum (start AND end).
  - R6 compares only bills whose own line met the minimum.

Every test forces its own rows on throwaway products and customers.
"""
import os
from datetime import date, timedelta

import pytest

import price_lookup as pl

MIG_199 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'migrations', '199_promo_min_qty.sql')
TODAY = '2026-10-02'
_pid = [975000]
_doc = [9975000]


def _ago(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()


@pytest.fixture
def db(tmp_db_conn):
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'min_qty' not in cols:
        tmp_db_conn.executescript(open(MIG_199, encoding='utf-8').read())
        tmp_db_conn.commit()
    return tmp_db_conn


def _product(conn, unit_type='อัน', base=100.0, cost=60.0, *, rows=(), tiers=()):
    _pid[0] += 1
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, brand_id, "
        "is_active) VALUES (?, ?, ?, ?, 6, 1)",
        (f'min-qty #{_pid[0]}', unit_type, base, cost)).lastrowid
    for unit, ratio in rows:
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                     (pid, unit, ratio))
    for label, price in tiers:
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                     (pid, label, price))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?",
                        (pid,)).fetchone()[0] == 0
    return pid


def _promo(conn, pid, *, discount_value=5.0, promo_type='percent', min_qty=None,
           min_qty_unit=None, bundle_condition=None, date_start=None, date_end=None):
    cur = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
        "date_start, date_end, is_active, bundle_condition, min_qty, min_qty_unit, source) "
        "VALUES (?, 'test min', ?, ?, ?, ?, 1, ?, ?, ?, 'manual')",
        (pid, promo_type, discount_value, date_start, date_end, bundle_condition,
         min_qty, min_qty_unit))
    conn.commit()
    return cur.lastrowid


def _customer(conn, code):
    conn.execute("INSERT INTO customers (code, name) VALUES (?, ?) "
                 "ON CONFLICT(code) DO UPDATE SET name = excluded.name", (code, f'ร้าน {code}'))
    conn.commit()
    return code


def _bill(conn, pid, *, date_iso, qty, unit, price, customer='TST673-X'):
    _doc[0] += 1
    doc_base = f'IV{_doc[0]}'
    net = round(price * qty, 2)
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
        (date_iso, f'{doc_base}-1', doc_base, pid, f'ร้าน {customer}', customer, qty, unit,
         price, net, net))
    conn.commit()


def _codes(out):
    return [f['code'] for f in out['flags']]


def _flag(out, code):
    hits = [f for f in out['flags'] if f['code'] == code]
    assert len(hits) == 1, _codes(out)
    return hits[0]['text']


def rp(conn, pid, **kw):
    kw.setdefault('today', TODAY)
    return pl.resolve_price(conn, product_id=pid, **kw)


# ── acceptance: 5% min 20 อัน (MACOH shape) ──────────────────────────────────

def test_macoh_19_is_list_20_is_discounted(db):
    pid = _product(db, rows=[('ตัว', 1.0)])
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')

    below = rp(db, pid, qty=19)
    assert below['answer']['price_per_unit'] == 100.0
    assert below['answer']['line_total'] == 1900.0
    assert below['list']['list_after_promo'] == 100.0
    assert below['list']['price_promo_applied'] is False
    assert below['list']['price_promo_in_effect'] is None
    assert below['list']['price_promo']['min_qty'] == 20.0     # still offered (display)
    assert below['list']['promo_gate'] == 'not_met'
    assert '20' in _flag(below, 'promo_min_not_met')
    assert any('≥ 20 อัน' in line for line in below['answer']['breadcrumb'])

    met = rp(db, pid, qty=20)
    assert met['answer']['price_per_unit'] == 95.0
    assert met['answer']['line_total'] == 1900.0
    assert met['list']['price_promo_applied'] is True
    assert met['list']['promo_gate'] == 'met'
    assert met['list']['price_promo_in_effect']['id'] == met['list']['price_promo']['id']
    assert not [c for c in _codes(met) if c.startswith('promo_min')]
    assert any('≥ 20 อัน' in line for line in met['answer']['breadcrumb'])


def test_explicit_qty_1_is_not_met(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    out = rp(db, pid, qty=1)
    assert out['list']['promo_gate'] == 'not_met'
    assert out['answer']['price_per_unit'] == 100.0


# ── acceptance: 25% min 5 โหล on a piece product (pid 307 shape) ─────────────

def test_pid307_59_vs_60_pieces(db):
    pid = _product(db, unit_type='ดอก', base=10.0, cost=4.0, tiers=[('1 โหล', 120.0)])
    _promo(db, pid, discount_value=25.0, min_qty=5, min_qty_unit='โหล')
    assert rp(db, pid, qty=59)['answer']['price_per_unit'] == 10.0
    assert rp(db, pid, qty=60)['answer']['price_per_unit'] == 7.5
    dozen = rp(db, pid, unit='โหล', qty=5)
    assert dozen['list']['list_for_unit'] == 120.0
    assert dozen['answer']['price_per_unit'] == 90.0
    assert rp(db, pid, unit='โหล', qty=4)['answer']['price_per_unit'] == 120.0


def test_x66_200ml_35_vs_36(db):
    pid = _product(db, unit_type='ตัว', base=60.0, cost=40.0, rows=[('โหล', 12.0)],
                   tiers=[('1 โหล', 670.0)])
    _promo(db, pid, discount_value=3.0, min_qty=3, min_qty_unit='โหล')
    assert rp(db, pid, qty=35)['answer']['price_per_unit'] == 60.0
    assert rp(db, pid, qty=36)['answer']['price_per_unit'] == 58.2


# ── fail closed ──────────────────────────────────────────────────────────────

def test_unconvertible_minimum_unit_is_not_applied_and_flagged(db):
    pid = _product(db)
    _promo(db, pid, min_qty=1, min_qty_unit='ลัง')
    out = rp(db, pid, qty=10_000)
    assert out['answer']['price_per_unit'] == 100.0
    assert out['list']['promo_gate'] == 'unconvertible'
    assert 'ลัง' in _flag(out, 'promo_min_unconvertible')


def test_label_without_number_is_not_applied_and_flagged(db):
    pid = _product(db)
    _promo(db, pid, bundle_condition='ยกลัง')
    out = rp(db, pid, qty=10_000)
    assert out['answer']['price_per_unit'] == 100.0
    assert out['list']['promo_gate'] == 'missing'
    assert 'ยกลัง' in _flag(out, 'promo_min_missing')


def test_no_quantity_is_qty_unknown(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    out = rp(db, pid, qty=None)
    assert out['list']['promo_gate'] == 'qty_unknown'
    assert out['answer']['price_per_unit'] == 100.0
    assert out['answer']['qty'] is None and out['answer']['line_total'] is None
    # own code (/interrogate on the PR 1 diff, item 8): "not met" is a claim the
    # resolver cannot make without a quantity
    assert 'ยังไม่ระบุจำนวน' in _flag(out, 'promo_min_qty_unknown')
    assert 'promo_min_not_met' not in _codes(out)


def test_asked_unit_without_a_ratio_is_qty_unknown(db):
    """A 'กล่อง' tier with no unit_conversions row: the resolver answers at the
    tier's own price (ratio unknown) — the gate cannot count pieces, so the
    promo is not applied."""
    pid = _product(db, tiers=[('1 กล่อง', 1000.0)])
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    out = rp(db, pid, unit='กล่อง', qty=5)
    assert out['unit']['ratio'] is None
    assert out['list']['promo_gate'] == 'qty_unknown'
    assert out['answer']['price_per_unit'] == 1000.0


# ── regression: a promo without a minimum ────────────────────────────────────

@pytest.mark.parametrize('qty', [None, 1, 19, 1000])
def test_ungated_promo_prices_as_before(db, qty):
    pid = _product(db)
    _promo(db, pid, discount_value=10.0)
    out = rp(db, pid, qty=qty)
    assert out['answer']['price_per_unit'] == 90.0
    assert out['list']['promo_gate'] == 'none'
    assert out['list']['price_promo_applied'] is True
    assert not [c for c in _codes(out) if c.startswith('promo_min') or c == 'last_paid_was_min_promo']


# ── last_paid (Put, 2026-10-02, A) ───────────────────────────────────────────

def test_last_bill_met_the_minimum_ask_below_answers_list(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    cust = _customer(db, 'TST673-LP1')
    _bill(db, pid, date_iso=_ago(10), qty=20, unit='อัน', price=95.0, customer=cust)

    below = rp(db, pid, customer_code=cust, qty=5)
    assert below['answer']['basis'] == 'list_after_promo'
    assert below['answer']['price_per_unit'] == 100.0
    assert 'last_paid_was_min_promo' in _codes(below)
    # fix round: the gated-price bill is skipped, not reported as this ask's
    # last price; the flag carries it (doc, date, ฿95)
    assert below['customer']['last'] is None
    assert '95' in _flag(below, 'last_paid_was_min_promo')

    # control: the same ask AT the minimum answers the customer's own bill
    met = rp(db, pid, customer_code=cust, qty=20)
    assert met['answer']['basis'] == 'last_paid'
    assert met['answer']['price_per_unit'] == 95.0
    assert 'last_paid_was_min_promo' not in _codes(met)


def test_last_bill_unrelated_to_the_promo_still_answers(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    cust = _customer(db, 'TST673-LP2')
    _bill(db, pid, date_iso=_ago(10), qty=5, unit='อัน', price=98.0, customer=cust)
    out = rp(db, pid, customer_code=cust, qty=5)
    assert out['answer']['basis'] == 'last_paid'
    assert out['answer']['price_per_unit'] == 98.0
    assert 'last_paid_was_min_promo' not in _codes(out)


def test_last_bill_before_the_promo_started_is_not_the_promo(db):
    """A big bill from before the gated promo existed did not 'get the gated
    price': it still answers a below-minimum ask."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(5))
    cust = _customer(db, 'TST673-LP3')
    _bill(db, pid, date_iso=_ago(30), qty=50, unit='อัน', price=97.0, customer=cust)
    out = rp(db, pid, customer_code=cust, qty=5)
    assert out['answer']['basis'] == 'last_paid'
    assert out['answer']['price_per_unit'] == 97.0
    assert 'last_paid_was_min_promo' not in _codes(out)


# ── evidence window (Put, 2026-10-02, A) ─────────────────────────────────────

def test_gated_promo_start_is_an_epoch_only_for_asks_that_meet_it(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(10))
    cust = _customer(db, 'TST673-EW1')
    _bill(db, pid, date_iso=_ago(30), qty=5, unit='อัน', price=99.0, customer=cust)

    below = rp(db, pid, customer_code=cust, qty=5)
    assert below['window']['reason'] != 'promo_start'
    assert below['answer']['basis'] == 'last_paid'          # window untouched
    assert below['answer']['price_per_unit'] == 99.0

    met = rp(db, pid, customer_code=cust, qty=20)
    assert met['window']['reason'] == 'promo_start'
    assert met['window']['from'] == _ago(10)
    assert met['answer']['basis'] == 'list_after_promo'
    assert met['answer']['price_per_unit'] == 95.0
    assert 'price_changed_since_last' in _codes(met)


def test_ungated_promo_start_is_an_epoch_for_every_ask(db):
    """CONTROL for the test above: without a minimum the start cuts the window
    whatever the quantity (behaviour before #673)."""
    pid = _product(db)
    _promo(db, pid, date_start=_ago(10))
    out = rp(db, pid, qty=5)
    assert out['window']['reason'] == 'promo_start'


def test_closed_gated_promo_end_is_an_epoch_only_for_asks_that_meet_it(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(40), date_end=_ago(6))
    assert rp(db, pid, qty=5)['window']['reason'] != 'promo_end'
    met = rp(db, pid, qty=20)
    assert met['window']['reason'] == 'promo_end'
    assert met['window']['from'] == _ago(5)


# ── R6: only bills that met the minimum are promo evidence ───────────────────

def test_r6_ignores_bills_below_the_minimum(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    for i in range(3):
        _bill(db, pid, date_iso=_ago(5 + i), qty=5, unit='อัน', price=100.0,
              customer=f'TST673-R6{i}')
    met = rp(db, pid, qty=20)
    assert met['context']['promo_stale'] is None                  # nothing comparable
    assert 'promo_stale' not in _codes(met)
    below = rp(db, pid, qty=5)
    assert below['context']['promo_stale'] is None                # promo not in effect

    # a qualifying bill AT the promo price is evidence the promo is used
    _bill(db, pid, date_iso=_ago(2), qty=20, unit='อัน', price=95.0, customer='TST673-R6q')
    met2 = rp(db, pid, qty=20)
    assert met2['context']['promo_stale'] is False
    assert met2['context']['promo_last_used'] == _ago(2)


def test_r6_control_ungated_promo_counts_every_bill(db):
    pid = _product(db)
    _promo(db, pid, date_start=_ago(60))
    for i in range(3):
        _bill(db, pid, date_iso=_ago(5 + i), qty=5, unit='อัน', price=100.0,
              customer=f'TST673-R6c{i}')
    out = rp(db, pid, qty=20)
    assert out['context']['promo_stale'] is True


# ── the quote CLI passes "no quantity" through ───────────────────────────────

def _cli():
    import importlib.util
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'scripts', 'price_lookup_cli.py')
    spec = importlib.util.spec_from_file_location('price_lookup_cli_673', script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_line_without_qty_has_no_quantity(db):
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน')
    cli = _cli()
    no_qty = cli._resolve_line(db, {'product_id': pid}, TODAY)['result']
    assert no_qty['list']['promo_gate'] == 'qty_unknown'
    assert no_qty['answer']['qty'] is None
    assert no_qty['answer']['price_per_unit'] == 100.0
    # control: an explicit qty that meets the minimum is priced with the promo
    met = cli._resolve_line(db, {'product_id': pid, 'qty': 20}, TODAY)['result']
    assert met['answer']['price_per_unit'] == 95.0


# ── fix round (/interrogate on the PR 1 diff, item 1 + Put Q6) ───────────────
# A bill "got the gated price" iff its line met the minimum of a gated price
# promo active on ITS date (open or closed now) AND its cash/piece is at or
# under that promo's price × 1.01. Only such bills are kept from answering an
# ask that does not meet that promo — last_paid and lowest alike.

def test_own_deal_above_the_promo_price_still_answers_a_small_ask(db):
    """Opus e2 S1: 24 อัน at the customer's own ฿98 (promo would be ฿95) is not
    the gated price; it keeps answering an ask of 10."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    cust = _customer(db, 'TST673-OD')
    _bill(db, pid, date_iso=_ago(10), qty=24, unit='อัน', price=98.0, customer=cust)
    out = rp(db, pid, customer_code=cust, qty=10)
    assert (out['answer']['basis'], out['answer']['price_per_unit']) == ('last_paid', 98.0)
    assert 'last_paid_was_min_promo' not in _codes(out)


def test_closed_gated_promo_bill_does_not_answer_a_small_ask(db):
    """Opus e3: the promo ran and closed; a 24 อัน bill at its ฿95 must not make
    an ask of 10 cheaper (฿95) than an ask of 24 (list after the promo ended)."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60), date_end=_ago(8))
    cust = _customer(db, 'TST673-CL')
    _bill(db, pid, date_iso=_ago(20), qty=24, unit='อัน', price=95.0, customer=cust)
    small = rp(db, pid, customer_code=cust, qty=10)
    big = rp(db, pid, customer_code=cust, qty=24)
    assert small['answer']['price_per_unit'] == 100.0
    assert small['answer']['basis'] == 'list_after_promo'
    assert 'last_paid_was_min_promo' in _codes(small)
    assert big['answer']['price_per_unit'] == 100.0


def test_lowest_for_a_small_ask_ignores_bills_at_the_gated_price(db):
    """Fable c: 'ต่ำสุดที่ใครเคยได้' for a 5-piece ask must not be a carton price."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    _bill(db, pid, date_iso=_ago(15), qty=20, unit='อัน', price=95.0, customer='TST673-L1')
    _bill(db, pid, date_iso=_ago(12), qty=5, unit='อัน', price=100.0, customer='TST673-L2')
    assert rp(db, pid, qty=5)['context']['lowest']['cash_per_unit'] == 100.0
    assert rp(db, pid, qty=20)['context']['lowest']['cash_per_unit'] == 95.0   # control


def test_met_ask_with_last_bill_below_minimum_at_list_answers_the_promo(db):
    """Put Q6 (2026-10-02): the ask meets the minimum, the customer's last bill
    was a small one at list → the promo price, flagged."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    cust = _customer(db, 'TST673-Q6')
    _bill(db, pid, date_iso=_ago(10), qty=5, unit='อัน', price=100.0, customer=cust)
    out = rp(db, pid, customer_code=cust, qty=20)
    assert out['answer']['price_per_unit'] == 95.0
    assert out['answer']['basis'] == 'list_after_promo'
    assert 'last_paid_below_min' in _codes(out)


def test_met_ask_lower_own_deal_still_wins(db):
    """Put Q6: a customer whose own small-order deal is under the promo price
    keeps it."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    cust = _customer(db, 'TST673-Q6b')
    _bill(db, pid, date_iso=_ago(10), qty=5, unit='อัน', price=93.0, customer=cust)
    out = rp(db, pid, customer_code=cust, qty=20)
    assert (out['answer']['basis'], out['answer']['price_per_unit']) == ('last_paid', 93.0)
    assert 'last_paid_below_min' not in _codes(out)


def test_no_quantity_and_a_gated_last_bill_never_says_not_met(db):
    """Opus/Fable e: with no quantity the flags must not claim the ask is
    below the minimum."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    cust = _customer(db, 'TST673-NQ')
    _bill(db, pid, date_iso=_ago(10), qty=20, unit='อัน', price=95.0, customer=cust)
    out = rp(db, pid, customer_code=cust, qty=None)
    assert 'promo_min_not_met' not in _codes(out)
    text = _flag(out, 'last_paid_was_min_promo')
    assert 'ยังไม่ระบุจำนวน' in text and 'ไม่ถึง' not in text


def test_gated_last_bill_answers_list_even_with_an_older_own_bill(db):
    """Put's last_paid rule (A): the latest in-window bill met the minimum at
    the gated price, today's ask is below it → LIST answers. The resolver must
    not fall back to an OLDER bill (lead repro, Opus e2/e3: ฿385.2 from a
    2025 bill, above the ฿350 list)."""
    pid = _product(db)
    _promo(db, pid, min_qty=20, min_qty_unit='อัน', date_start=_ago(60))
    cust = _customer(db, 'TST673-OLD')
    _bill(db, pid, date_iso=_ago(200), qty=5, unit='อัน', price=110.0, customer=cust)
    _bill(db, pid, date_iso=_ago(10), qty=20, unit='อัน', price=95.0, customer=cust)
    for q in (10, None):
        out = rp(db, pid, customer_code=cust, qty=q)
        assert out['answer']['price_per_unit'] == 100.0, q
        assert out['answer']['basis'] == 'list_after_promo', q
        assert 'last_paid_was_min_promo' in _codes(out), q
        assert out['customer']['last'] is None, q   # the older bill does not stand in


def test_closed_label_without_number_promo_end_is_an_epoch_for_every_ask(db):
    """Prod shape (pid 544 / 27ส009, measured 2026-10-02): a closed ยกลัง promo
    with no number was priced for EVERYONE until #673, so its end is a price
    regime change for every ask. Treating it as gated widened the window and
    let a pre-end bill answer (฿646 instead of list ฿680)."""
    pid = _product(db)
    _promo(db, pid, bundle_condition='ยกลัง', date_start=None, date_end=_ago(8))
    cust = _customer(db, 'TST673-LBL')
    _bill(db, pid, date_iso=_ago(100), qty=1, unit='อัน', price=95.0, customer=cust)
    for q in (None, 1, 5, 1000):
        out = rp(db, pid, customer_code=cust, qty=q)
        assert out['window']['reason'] == 'promo_end', q
        assert out['window']['from'] == _ago(7), q
        assert out['answer']['price_per_unit'] == 100.0, q
        assert out['answer']['basis'] == 'list_after_promo', q
