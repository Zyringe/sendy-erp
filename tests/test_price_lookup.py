"""price_lookup.py — the evidence-ordered B2B price resolver (R1-R8).

Every test forces its own state on a throwaway product (+ customer where
needed) inside `tmp_db_conn` (clones the live dev DB WITH data — see
verification-discipline.md: never inherit state, force it). `today` is
pinned to a literal ISO date per test rather than computed relative to
wall-clock date.today(), so the suite is reproducible regardless of when it
runs — every fixture date is written as an explicit offset comment from
that test's chosen `today`.

Ruling P1 (guards a clone WITHOUT migration 176 applied — other machines):
one fixture below (`db`) checks `PRAGMA table_info(promotions)` for the
`source` column and applies data/migrations/176_promo_source_and_dates.sql
if it's missing. Every test in this file uses that fixture, not the bare
tmp_db_conn.
"""
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import price_lookup as pl
from models import promotions as promo_models

MIG_176 = Path(__file__).resolve().parents[1] / 'data/migrations/176_promo_source_and_dates.sql'

SENDAI_BRAND_ID = 3   # เซ็นได — is_own_brand = 1 (verified against the live dev DB)
TOA_BRAND_ID = 6      # TOA / จระเข้ — is_own_brand = 0, used as an own-brand control

_pid_counter = [900000]
_doc_counter = [9900000]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db_conn):
    """tmp_db_conn, guaranteed to have migration 176 (promotions.source +
    date_start stamp) applied — Ruling P1."""
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'source' not in cols:
        tmp_db_conn.executescript(MIG_176.read_text())
        tmp_db_conn.commit()
    return tmp_db_conn


def _mk_product(conn, name, *, unit_type='ตัว', base=100.0, cost=60.0,
                 brand_id=SENDAI_BRAND_ID, active=1):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES (?,?,?,?,?,?)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, brand_id, active),
    )
    conn.commit()
    return cur.lastrowid


def _clear_pid(conn, pid):
    """Force fixture state: wipe every row this pid could already carry from
    the live dev DB clone before a test seeds its own."""
    tier_ids = [r['id'] for r in conn.execute(
        "SELECT id FROM product_price_tiers WHERE product_id = ?", (pid,))]
    if tier_ids:
        qmarks = ",".join("?" * len(tier_ids))
        conn.execute(
            f"DELETE FROM audit_log WHERE table_name = 'product_price_tiers' "
            f"AND row_id IN ({qmarks})", tier_ids)
    for table in ('sales_transactions', 'promotions', 'product_price_tiers',
                  'unit_conversions', 'product_price_history'):
        conn.execute(f"DELETE FROM {table} WHERE product_id = ?", (pid,))
    conn.commit()


def _mk_customer(conn, code, name):
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        (code, name),
    )
    conn.commit()
    return code


def _clear_customer_pid(conn, code, pid):
    conn.execute(
        "DELETE FROM sales_transactions WHERE customer_code = ? AND product_id = ?",
        (code, pid))
    conn.commit()


def _bill(conn, *, pid, customer_code, customer_name, date_iso, doc_base=None,
          suffix=1, qty, unit, unit_price, vat_type, net, discount=None):
    if doc_base is None:
        _doc_counter[0] += 1
        doc_base = f"IV{_doc_counter[0]}"
    doc_no = f"{doc_base}-{suffix}"
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, discount, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, doc_no, doc_base, pid, customer_name, customer_code,
         qty, unit, unit_price, vat_type, discount, net, net),
    )
    conn.commit()
    return doc_no, doc_base


def _tier(conn, pid, qty_label, price, sort_order=100):
    cur = conn.execute(
        "INSERT INTO product_price_tiers (product_id, qty_label, price, sort_order) "
        "VALUES (?,?,?,?)",
        (pid, qty_label, price, sort_order),
    )
    conn.commit()
    return cur.lastrowid


def _stamp_tier_audit(conn, tier_id, pid, created_at):
    """Replace whatever audit_log row the INSERT trigger just wrote for this
    tier with one carrying a controlled created_at — 'seed the audit_log row
    the tier trigger would write', per the R4 4b epoch-source test."""
    conn.execute(
        "DELETE FROM audit_log WHERE table_name = 'product_price_tiers' AND row_id = ?",
        (tier_id,))
    conn.execute(
        "INSERT INTO audit_log (table_name, row_id, action, changed_fields, created_at) "
        "VALUES ('product_price_tiers', ?, 'INSERT', '{}', ?)",
        (tier_id, created_at),
    )
    conn.commit()


def _uc(conn, pid, bsn_unit, ratio):
    conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
        (pid, bsn_unit, ratio),
    )
    conn.commit()


def _promo(conn, pid, *, promo_type, discount_value=None, date_start=None, date_end=None,
           is_active=1, bundle_buy=None, bundle_free=None, bundle_unit=None,
           gift_desc=None, gift_qty=None, source='manual', created_at=None):
    cols = ["product_id", "promo_name", "promo_type", "discount_value", "date_start",
            "date_end", "is_active", "bundle_buy", "bundle_free", "bundle_unit",
            "gift_desc", "gift_qty", "source"]
    vals = [pid, f"test {promo_type}", promo_type, discount_value, date_start,
            date_end, is_active, bundle_buy, bundle_free, bundle_unit,
            gift_desc, gift_qty, source]
    if created_at is not None:
        cols.append("created_at")
        vals.append(created_at)
    qmarks = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO promotions ({','.join(cols)}) VALUES ({qmarks})", vals)
    conn.commit()
    return cur.lastrowid


def _base_price_history(conn, pid, changed_at, old=100.0, new=100.0):
    conn.execute(
        "INSERT INTO product_price_history (product_id, field_name, old_value, new_value, changed_at) "
        "VALUES (?, 'base_sell_price', ?, ?, ?)",
        (pid, old, new, changed_at),
    )
    conn.commit()


TODAY = "2026-08-26"


def _days_ago(n, today=TODAY):
    return (date.fromisoformat(today) - timedelta(days=n)).isoformat()


# ── R1: unit + list ──────────────────────────────────────────────────────────

def test_r1_piece_ask_no_tier_lists_base(db):
    pid = _mk_product(db, "R1 piece", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    out = pl.resolve_price(db, product_id=pid, unit=None, today=TODAY)
    assert out['list']['list_for_unit'] == 100.0
    assert out['list']['list_source'] == 'base×ratio'
    assert out['unit']['ratio'] == 1.0
    assert out['unit']['ratio_source'] == 'none'


def test_r1_dozen_ask_with_ratio_and_tier_equal(db):
    pid = _mk_product(db, "R1 dozen ratio+tier equal", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _tier(db, pid, '1 โหล', 240.0)  # == base(20) x ratio(12)
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['list']['list_for_unit'] == 240.0
    assert out['list']['list_source'] == 'tier'
    assert out['unit']['ratio'] == 12.0
    assert out['unit']['ratio_source'] == 'unit_conversions'
    assert out['list']['tier_equals_base_x_ratio'] is True


def test_r1_dozen_ask_tier_hand_set_not_equal(db):
    pid = _mk_product(db, "R1 dozen tier hand-set", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _tier(db, pid, '1 โหล', 230.0)  # != base(20) x ratio(12) == 240
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['list']['list_for_unit'] == 230.0
    assert out['list']['list_source'] == 'tier'
    assert out['list']['tier_equals_base_x_ratio'] is False


def test_r1_dozen_ask_no_tier_no_unit_conversions_is_tier_implied(db):
    pid = _mk_product(db, "R1 dozen tier-implied", unit_type='ตัว', base=19.17, cost=10.0)
    _clear_pid(db, pid)
    _tier(db, pid, 'โหล', 230.0)  # no unit_conversions row at all
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['unit']['ratio'] == 12.0
    assert out['unit']['ratio_source'] == 'tier-implied'
    assert out['list']['list_for_unit'] == 230.0
    assert out['list']['list_source'] == 'tier'


def test_r1_dozen_only_base_zero_with_tier(db):
    pid = _mk_product(db, "R1 dozen-only", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    _tier(db, pid, '1 โหล', 230.0)
    out = pl.resolve_price(db, product_id=pid, unit=None, today=TODAY)  # asks in ตัว (unit_type)
    assert out['list']['base_per_piece'] == 0.0
    assert out['list']['list_source'] == 'dozen-only'
    assert out['list']['list_for_unit'] == 230.0
    assert out['answer']['unit'] == 'โหล'  # answer switches unit, per R1
    assert out['unit']['asked'] == 'ตัว'   # what was asked stays recorded
    assert any('ขายยกโหล' in line and '19.17' in line for line in out['answer']['breadcrumb'])


def test_r1_unit_alias_normalization(db):
    pid = _mk_product(db, "R1 alias", unit_type='ตัว', base=10.0, cost=5.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _tier(db, pid, '1 โหล', 120.0)
    for alias in ('โหล', '1 โหล', 'หล'):
        out = pl.resolve_price(db, product_id=pid, unit=alias, today=TODAY)
        assert out['unit']['asked'] == 'โหล', f"alias {alias!r} did not normalize"


# ── R2: promos ───────────────────────────────────────────────────────────────

def test_r2_percent_promo_on_dozen(db):
    pid = _mk_product(db, "R2 percent", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _tier(db, pid, '1 โหล', 230.0)
    _promo(db, pid, promo_type='percent', discount_value=10.0,
           date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['list']['list_after_promo'] == 207.0  # 230 * 0.9
    assert out['list']['price_promo']['promo_type'] == 'percent'


def test_r2_fixed_promo_per_piece_on_dozen(db):
    pid = _mk_product(db, "R2 fixed", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _promo(db, pid, promo_type='fixed', discount_value=18.0,
           date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['list']['list_after_promo'] == 216.0  # 18 * 12 (fixed is per PIECE)
    assert out['list']['price_promo']['promo_type'] == 'fixed'


def test_r2_bundle_promo_free_units_price_unchanged(db):
    pid = _mk_product(db, "R2 bundle", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _promo(db, pid, promo_type='bundle', bundle_buy=12, bundle_free=1,
           bundle_unit='ใบ', date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['list']['list_after_promo'] == out['list']['list_for_unit']  # price unchanged
    assert out['answer']['free_units'] == {'buy': 12, 'free': 1, 'unit': 'ใบ'}


def test_r2_price_and_qty_promo_both_returned(db):
    pid = _mk_product(db, "R2 price+qty", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _promo(db, pid, promo_type='fixed', discount_value=18.0,
           date_start='2026-06-01', is_active=1)
    _promo(db, pid, promo_type='bundle', bundle_buy=12, bundle_free=1,
           date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['list']['price_promo'] is not None
    assert out['list']['price_promo']['promo_type'] == 'fixed'
    assert out['list']['qty_promo'] is not None
    assert out['list']['qty_promo']['promo_type'] == 'bundle'


def test_r2_mixed_discount_null_classifies_as_qty_only(db):
    """pid-445 shape: mixed, discount_value NULL, bundle_buy=1/bundle_free=1,
    gift_desc set. Must occupy the QTY slot only (per promo_slot_sql's price
    predicate requiring discount_value IS NOT NULL for 'mixed')."""
    pid = _mk_product(db, "R2 mixed pid445-shape", unit_type='ใบ', base=250.0, cost=150.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='mixed', discount_value=None,
           bundle_buy=1, bundle_free=1, gift_desc='ใบเลื่อยคันธนู 30 นิ้ว',
           date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['list']['price_promo'] is None
    assert out['list']['qty_promo'] is not None
    assert out['list']['qty_promo']['promo_type'] == 'mixed'


# ── R9 (R2 class, listed separately in the brief's test list) ───────────────

def test_r9_mixed_with_both_fields_occupies_both_slots(db):
    pid = _mk_product(db, "R9 mixed both", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='mixed', discount_value=10.0,
           bundle_buy=12, bundle_free=1, date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['list']['price_promo'] is not None
    assert out['list']['price_promo']['promo_type'] == 'mixed'
    assert out['list']['qty_promo'] is not None
    assert out['list']['qty_promo']['promo_type'] == 'mixed'
    assert out['list']['price_promo']['id'] == out['list']['qty_promo']['id']


def test_r9_mixed_discount_null_only_qty_slot(db):
    pid = _mk_product(db, "R9 mixed qty-only", unit_type='ใบ', base=250.0, cost=150.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='mixed', discount_value=None,
           bundle_buy=1, bundle_free=1, gift_desc='ใบเลื่อยคันธนู 30 นิ้ว',
           date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['list']['price_promo'] is None
    assert out['list']['qty_promo'] is not None


# ── R3: answer order ─────────────────────────────────────────────────────────

def test_r3_repeat_pair_in_window_is_last_paid(db):
    pid = _mk_product(db, "R3 last paid", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    cust = _mk_customer(db, 'TST-R3-1', 'ลูกค้าทดสอบ R3-1')
    _clear_customer_pid(db, cust, pid)
    # vat_type=2 bill: cash = net/qty * 1.07 -- independent oracle
    net, qty = 934.579, 10.0
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้าทดสอบ R3-1',
          date_iso=_days_ago(30), qty=qty, unit='ตัว', unit_price=100.0,
          vat_type=2, net=net)
    out = pl.resolve_price(db, product_id=pid, customer_code=cust, today=TODAY)
    expected = round((net / qty) * 1.07, 2)
    assert out['answer']['basis'] == 'last_paid'
    assert out['answer']['price_per_unit'] == expected
    assert out['customer']['last']['in_window'] is True


def test_r3_only_bill_predates_base_change_falls_to_list_and_flags(db):
    pid = _mk_product(db, "R3 predates epoch", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    cust = _mk_customer(db, 'TST-R3-2', 'ลูกค้าทดสอบ R3-2')
    _clear_customer_pid(db, cust, pid)
    _base_price_history(db, pid, changed_at=_days_ago(20) + " 09:00:00", old=80.0, new=100.0)
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้าทดสอบ R3-2',
          date_iso=_days_ago(40), qty=1, unit='ตัว', unit_price=80.0, vat_type=1, net=80.0)
    out = pl.resolve_price(db, product_id=pid, customer_code=cust, today=TODAY)
    assert out['answer']['basis'] == 'list_after_promo'
    assert out['customer']['last'] is not None
    assert out['customer']['last']['in_window'] is False
    codes = [f['code'] for f in out['flags']]
    assert 'price_changed_since_last' in codes


def test_r3_no_customer_falls_to_list(db):
    pid = _mk_product(db, "R3 no customer", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    out = pl.resolve_price(db, product_id=pid, customer_code=None, today=TODAY)
    assert out['answer']['basis'] == 'list_after_promo'
    assert out['customer'] is None


def test_r3_extra_disc_applies_when_no_last_paid(db):
    pid = _mk_product(db, "R3 extra disc", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    out = pl.resolve_price(db, product_id=pid, extra_disc=0.2, today=TODAY)
    assert out['answer']['basis'] == 'list_after_promo_extra'
    assert out['answer']['price_per_unit'] == 80.0  # 100 * 0.8


# ── R4: population + window ──────────────────────────────────────────────────

def test_r4_population_excludes_every_bad_shape(db):
    pid = _mk_product(db, "R4 population", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)

    # The live dev DB independently carries real ar_writeoffs rows for
    # IV6900401/402/403 (the วรสวัสดิ์ giveaway write-off happens to reuse
    # the exact same doc numbers as the cost-basis dummy invoices). Left in
    # place, case 5 below would be excluded by the write-off mechanism
    # regardless of the dummy-doc-base clause under test here, silently
    # testing the wrong thing. Clear them on this throwaway clone so case 5
    # isolates the dummy-doc-base exclusion specifically.
    db.execute("DELETE FROM ar_writeoffs WHERE doc_no IN ('IV6900401','IV6900402','IV6900403')")
    db.commit()

    included_count = 0

    # 1) marketplace row -- excluded
    _bill(db, pid=pid, customer_code='หน้าร้านS', customer_name='หน้าร้านS',
          date_iso=_days_ago(10), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)

    # 2) doc_base in ar_writeoffs with excludes_revenue=1 -- excluded
    _, wo_base = _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
                        date_iso=_days_ago(11), qty=1, unit='ตัว', unit_price=100.0,
                        vat_type=1, net=100.0)
    db.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, writeoff_date, excludes_revenue) "
        "VALUES (?, 'TST-R4', 100, 'expense', ?, 1)", (wo_base, _days_ago(11)))
    db.commit()

    # 2b) control doc_base in ar_writeoffs with excludes_revenue=0 -- INCLUDED
    _, wo_base_ctl = _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
                            date_iso=_days_ago(12), qty=1, unit='ตัว', unit_price=100.0,
                            vat_type=1, net=100.0)
    db.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, writeoff_date, excludes_revenue) "
        "VALUES (?, 'TST-R4', 100, 'writeback', ?, 0)", (wo_base_ctl, _days_ago(12)))
    db.commit()
    included_count += 1

    # 3) doc_base 'SR...' -- excluded
    _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
          date_iso=_days_ago(13), doc_base='SR9900001', qty=1, unit='ตัว',
          unit_price=100.0, vat_type=1, net=100.0)

    # 4) doc_base 'HS...' -- excluded
    _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
          date_iso=_days_ago(14), doc_base='HS9900001', qty=1, unit='ตัว',
          unit_price=100.0, vat_type=1, net=100.0)

    # 5) cost-basis dummy invoice -- excluded
    _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
          date_iso=_days_ago(15), doc_base='IV6900401', suffix=7, qty=1, unit='ตัว',
          unit_price=100.0, vat_type=1, net=100.0)

    # 6) qty <= 0 -- excluded
    _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
          date_iso=_days_ago(16), qty=0, unit='ตัว', unit_price=100.0, vat_type=1, net=0.0)

    # 7) unit with no ratio row -- INCLUDED in n_bills, but unratioed
    _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
          date_iso=_days_ago(17), qty=1, unit='กระสอบ', unit_price=50.0, vat_type=1, net=50.0)
    included_count += 1

    # 8) a normal, fully-valid bill -- INCLUDED, and it's the cheapest -> lowest
    _bill(db, pid=pid, customer_code='TST-R4', customer_name='ลูกค้า R4',
          date_iso=_days_ago(18), qty=1, unit='ตัว', unit_price=70.0, vat_type=1, net=70.0)
    included_count += 1

    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['window']['n_bills'] == included_count == 3
    assert out['context']['lowest'] is not None
    assert out['context']['lowest']['cash_per_unit'] == 70.0
    assert out['window']['n_unratioed'] == 1


def test_r4_population_break_it_once_doc_no_instead_of_doc_base(db, monkeypatch):
    """Break-it-once: replace doc_base with doc_no in evidence_filter and
    confirm the writeoff + dummy-invoice cases go green-for-the-wrong-reason
    -- i.e. this guard test goes RED when the doc_base keying is removed."""
    pid = _mk_product(db, "R4 break-it-once", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _, wo_base = _bill(db, pid=pid, customer_code='TST-R4B', customer_name='ลูกค้า R4B',
                        date_iso=_days_ago(11), qty=1, unit='ตัว', unit_price=100.0,
                        vat_type=1, net=100.0)
    db.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, writeoff_date, excludes_revenue) "
        "VALUES (?, 'TST-R4B', 100, 'expense', ?, 1)", (wo_base, _days_ago(11)))
    db.commit()
    _bill(db, pid=pid, customer_code='TST-R4B', customer_name='ลูกค้า R4B',
          date_iso=_days_ago(12), doc_base='IV6900401', suffix=3, qty=1, unit='ตัว',
          unit_price=100.0, vat_type=1, net=100.0)

    def _broken_filter(alias):
        import sales_filters
        p = f'{alias}.' if alias else ''
        return (
            f"{sales_filters.revenue_filter(alias).replace('doc_base', 'doc_no')} "
            f"AND {p}qty > 0 AND {p}net > 0 "
            f"AND {p}customer NOT LIKE 'หน้าร้าน%' "
            f"AND {p}doc_no NOT IN ('IV6900401-3','IV6900402','IV6900403')"
        )

    monkeypatch.setattr(pl, 'evidence_filter', _broken_filter)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    # With doc_no keying, the writeoff doc_no ('...-1', not the base) no
    # longer matches the write-off subquery's base-form doc_no, and the
    # dummy-invoice's suffix-1 doc_no doesn't match the -3 line above either
    # -- both wrongly stay IN. n_bills should be 2 (both rows leak back in),
    # not the correct 0.
    assert out['window']['n_bills'] != 0, "broken filter did not leak the excluded rows back in (test invalid)"


def test_r4b_base_changed_epoch_window_and_pre_epoch(db):
    pid = _mk_product(db, "R4b base changed epoch", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    change_date = _days_ago(10)
    _base_price_history(db, pid, changed_at=change_date + " 09:00:00", old=80.0, new=100.0)
    # 1 bill after the change
    _bill(db, pid=pid, customer_code='TST-R4b', customer_name='ลูกค้า R4b',
          date_iso=_days_ago(5), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)
    # 5 bills before the change (at the old price -- must NOT feed lowest/answer)
    for i in range(5):
        _bill(db, pid=pid, customer_code='TST-R4b', customer_name='ลูกค้า R4b',
              date_iso=_days_ago(20 + i), qty=1, unit='ตัว', unit_price=50.0, vat_type=1, net=50.0)

    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['window']['from'] == change_date
    assert out['window']['n_bills'] == 1
    assert out['window']['widened_to_24m'] is True
    assert len(out['context']['pre_epoch']) == 3
    assert out['answer']['basis'] == 'list_after_promo'
    assert out['context']['lowest']['cash_per_unit'] == 100.0  # the post-change bill, not 50


def test_r4b_no_epoch_widens_to_24m(db):
    pid = _mk_product(db, "R4b no epoch widen", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    # 2 bills in the last 12 months
    for i in range(2):
        _bill(db, pid=pid, customer_code='TST-R4b2', customer_name='ลูกค้า R4b2',
              date_iso=_days_ago(30 + i), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)
    # 4 more bills 13-24 months back
    for i in range(4):
        _bill(db, pid=pid, customer_code='TST-R4b2', customer_name='ลูกค้า R4b2',
              date_iso=_days_ago(400 + i * 10), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)

    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['window']['widened_to_24m'] is True
    assert out['window']['n_bills'] == 6


def test_r4b_epoch_source_dozen_only_tier_changed(db):
    pid = _mk_product(db, "R4b tier changed", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    tier_id = _tier(db, pid, '1 โหล', 230.0)
    change_date = _days_ago(10)
    _stamp_tier_audit(db, tier_id, pid, change_date + " 09:00:00")

    out = pl.resolve_price(db, product_id=pid, today=TODAY)  # asks ตัว -> dozen-only -> โหล
    assert out['window']['from'] == change_date


def test_r4b_epoch_source_promo_ended_no_replacement(db):
    pid = _mk_product(db, "R4b promo ended", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    end_date = _days_ago(5)
    _promo(db, pid, promo_type='percent', discount_value=10.0,
           date_start='2026-06-01', date_end=end_date, is_active=1)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    expected_from = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
    assert out['window']['from'] == expected_from


def test_r4b_base_change_datetime_boundary_bill_in_window(db):
    pid = _mk_product(db, "R4b datetime boundary", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _base_price_history(db, pid, changed_at='2026-08-17 13:32:37', old=80.0, new=100.0)
    _bill(db, pid=pid, customer_code='TST-R4b4', customer_name='ลูกค้า R4b4',
          date_iso='2026-08-17', qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['window']['from'] == '2026-08-17'
    assert out['window']['n_bills'] == 1  # the boundary bill IS included, not dropped


def test_r4b_break_it_once_max_clamp_removed(db, monkeypatch):
    """Break-it-once: remove the max(epoch, calendar-cap) clamp in the
    widening step and confirm the epoch's floor is violated (the widened
    window reaches BEFORE the epoch, which R4 explicitly forbids)."""
    pid = _mk_product(db, "R4b break clamp", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    change_date = _days_ago(10)
    _base_price_history(db, pid, changed_at=change_date + " 09:00:00", old=80.0, new=100.0)
    _bill(db, pid=pid, customer_code='TST-R4b5', customer_name='ลูกค้า R4b5',
          date_iso=_days_ago(5), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)

    def _broken_window(conn, product_id, epoch, today):
        today_d = date.fromisoformat(today)
        from_365 = (today_d - timedelta(days=365)).isoformat()  # NO max(epoch, ...) clamp
        n_bills = len(pl._evidence_rows(conn, product_id, from_365, today))
        if n_bills < 3:
            from_730 = (today_d - timedelta(days=730)).isoformat()  # NO clamp here either
            n_bills = len(pl._evidence_rows(conn, product_id, from_730, today))
            return from_730, n_bills, True
        return from_365, n_bills, False

    monkeypatch.setattr(pl, '_window', _broken_window)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['window']['from'] < change_date, \
        "expected the clamp-removed window to reach before the epoch (test invalid otherwise)"


# ── R5: flags ────────────────────────────────────────────────────────────────

def test_r5_below_cost_flag(db):
    pid = _mk_product(db, "R5 below cost", unit_type='ตัว', base=100.0, cost=90.0)
    _clear_pid(db, pid)
    out = pl.resolve_price(db, product_id=pid, extra_disc=0.2, today=TODAY)  # 100*0.8=80 < cost 90
    codes = [f['code'] for f in out['flags']]
    assert 'below_cost' in codes
    # control: no extra_disc -> price 100 > cost 90 -> no flag
    ctl = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert 'below_cost' not in [f['code'] for f in ctl['flags']]


def test_r5_new_floor_flag(db):
    pid = _mk_product(db, "R5 new floor", unit_type='ตัว', base=100.0, cost=10.0)
    _clear_pid(db, pid)
    _bill(db, pid=pid, customer_code='TST-R5a', customer_name='ลูกค้า R5a',
          date_iso=_days_ago(30), qty=1, unit='ตัว', unit_price=90.0, vat_type=1, net=90.0)
    out = pl.resolve_price(db, product_id=pid, extra_disc=0.5, today=TODAY)  # 50 < lowest 90
    codes = [f['code'] for f in out['flags']]
    assert 'new_floor' in codes
    ctl = pl.resolve_price(db, product_id=pid, today=TODAY)  # 100 > lowest 90 -> no flag
    assert 'new_floor' not in [f['code'] for f in ctl['flags']]


def test_r5_price_changed_since_last_flag(db):
    pid = _mk_product(db, "R5 price changed", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    cust = _mk_customer(db, 'TST-R5b', 'ลูกค้า R5b')
    _clear_customer_pid(db, cust, pid)
    _base_price_history(db, pid, changed_at=_days_ago(20) + " 09:00:00", old=80.0, new=100.0)
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้า R5b',
          date_iso=_days_ago(40), qty=1, unit='ตัว', unit_price=80.0, vat_type=1, net=80.0)
    out = pl.resolve_price(db, product_id=pid, customer_code=cust, today=TODAY)
    codes = [f['code'] for f in out['flags']]
    assert 'price_changed_since_last' in codes
    # control: no customer -> flag cannot fire
    ctl = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert 'price_changed_since_last' not in [f['code'] for f in ctl['flags']]


def test_r5_own_brand_hint_flag(db):
    pid = _mk_product(db, "R5 own brand hint", unit_type='ตัว', base=100.0, cost=60.0,
                       brand_id=SENDAI_BRAND_ID)
    _clear_pid(db, pid)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)  # basis=list_after_promo, price=100=list
    codes = [f['code'] for f in out['flags']]
    assert 'own_brand_hint' in codes
    # control: 3rd-party brand -> no hint even at the same price ratio
    pid_ctl = _mk_product(db, "R5 3rd party control", unit_type='ตัว', base=100.0, cost=60.0,
                           brand_id=TOA_BRAND_ID)
    _clear_pid(db, pid_ctl)
    ctl = pl.resolve_price(db, product_id=pid_ctl, today=TODAY)
    assert 'own_brand_hint' not in [f['code'] for f in ctl['flags']]


def test_r5_own_brand_hint_suppressed_on_last_paid(db):
    pid = _mk_product(db, "R5 own brand suppressed", unit_type='ตัว', base=100.0, cost=60.0,
                       brand_id=SENDAI_BRAND_ID)
    _clear_pid(db, pid)
    cust = _mk_customer(db, 'TST-R5c', 'ลูกค้า R5c')
    _clear_customer_pid(db, cust, pid)
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้า R5c',
          date_iso=_days_ago(10), qty=1, unit='ตัว', unit_price=99.0, vat_type=1, net=99.0)
    out = pl.resolve_price(db, product_id=pid, customer_code=cust, today=TODAY)
    assert out['answer']['basis'] == 'last_paid'
    assert 'own_brand_hint' not in [f['code'] for f in out['flags']]


def test_r5_window_widened_flag(db):
    pid = _mk_product(db, "R5 window widened", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    for i in range(2):
        _bill(db, pid=pid, customer_code='TST-R5d', customer_name='ลูกค้า R5d',
              date_iso=_days_ago(30 + i), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)
    for i in range(4):
        _bill(db, pid=pid, customer_code='TST-R5d', customer_name='ลูกค้า R5d',
              date_iso=_days_ago(400 + i * 10), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert 'window_widened' in [f['code'] for f in out['flags']]

    pid_ctl = _mk_product(db, "R5 window not widened control", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid_ctl)
    for i in range(3):
        _bill(db, pid=pid_ctl, customer_code='TST-R5d2', customer_name='ลูกค้า R5d2',
              date_iso=_days_ago(30 + i), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)
    ctl = pl.resolve_price(db, product_id=pid_ctl, today=TODAY)
    assert 'window_widened' not in [f['code'] for f in ctl['flags']]


def test_r5_promo_stale_flag(db):
    pid = _mk_product(db, "R5 promo stale", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='percent', discount_value=10.0,
           date_start='2026-06-01', is_active=1)  # promo price = 90
    _bill(db, pid=pid, customer_code='TST-R5e', customer_name='ลูกค้า R5e',
          date_iso=_days_ago(10), qty=1, unit='ตัว', unit_price=100.0, vat_type=1, net=100.0)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert 'promo_stale' in [f['code'] for f in out['flags']]

    pid_ctl = _mk_product(db, "R5 promo not stale control", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid_ctl)
    _promo(db, pid_ctl, promo_type='percent', discount_value=10.0,
           date_start='2026-06-01', is_active=1)
    _bill(db, pid=pid_ctl, customer_code='TST-R5e2', customer_name='ลูกค้า R5e2',
          date_iso=_days_ago(10), qty=1, unit='ตัว', unit_price=90.0, vat_type=1, net=90.0)
    ctl = pl.resolve_price(db, product_id=pid_ctl, today=TODAY)
    assert 'promo_stale' not in [f['code'] for f in ctl['flags']]


# ── R6: promo evidence ───────────────────────────────────────────────────────

def test_r6_all_bills_above_promo_is_stale(db):
    pid = _mk_product(db, "R6 all above", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='percent', discount_value=10.0, date_start='2026-06-01', is_active=1)
    for i in range(3):
        _bill(db, pid=pid, customer_code=f'TST-R6a{i}', customer_name='ลูกค้า R6a',
              date_iso=_days_ago(5 + i), qty=1, unit='ตัว', unit_price=95.0, vat_type=1, net=95.0)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['context']['promo_stale'] is True
    assert out['context']['promo_last_used'] is None


def test_r6_one_bill_at_or_below_not_stale(db):
    pid = _mk_product(db, "R6 one at or below", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='percent', discount_value=10.0, date_start='2026-06-01', is_active=1)
    _bill(db, pid=pid, customer_code='TST-R6b1', customer_name='ลูกค้า R6b',
          date_iso=_days_ago(20), qty=1, unit='ตัว', unit_price=95.0, vat_type=1, net=95.0)
    match_date = _days_ago(5)
    _bill(db, pid=pid, customer_code='TST-R6b2', customer_name='ลูกค้า R6b',
          date_iso=match_date, qty=1, unit='ตัว', unit_price=90.0, vat_type=1, net=90.0)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['context']['promo_stale'] is False
    assert out['context']['promo_last_used'] == match_date


def test_r6_zero_bills_neutral(db):
    pid = _mk_product(db, "R6 zero bills", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='percent', discount_value=10.0, date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['context']['promo_stale'] is None
    assert out['context']['promo_last_used'] is None
    assert 'promo_stale' not in [f['code'] for f in out['flags']]


# ── R8 / test 6b: margin incl. free units ────────────────────────────────────

def test_r8_bundle_multiplier_on_margin(db):
    pid = _mk_product(db, "R8 bundle margin", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _promo(db, pid, promo_type='bundle', bundle_buy=12, bundle_free=1,
           date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, unit='โหล', today=TODAY)
    assert out['internal']['cost_side'] == round(10.0 * 12 * 13 / 12, 2)  # cost x 13
    assert out['internal']['margin_incl_free_units'] is True

    pid_ctl = _mk_product(db, "R8 bundle margin control", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid_ctl)
    _uc(db, pid_ctl, 'โหล', 12.0)
    ctl = pl.resolve_price(db, product_id=pid_ctl, unit='โหล', today=TODAY)
    assert ctl['internal']['cost_side'] == round(10.0 * 12, 2)  # plain cost x 12
    assert ctl['internal']['margin_incl_free_units'] is False


# ── find_products / find_customers ──────────────────────────────────────────

def test_find_products_multi_token_and(db):
    pid = _mk_product(db, "มือจับ 600 AC", unit_type='ตัว', base=50.0, cost=30.0)
    _clear_pid(db, pid)
    results = pl.find_products(db, "มือจับ 600 AC")
    assert any(r['id'] == pid for r in results)


def test_find_customers_exact_code(db):
    results = pl.find_customers(db, "01อ35")
    assert any(r['code'] == '01อ35' for r in results)


# ── purity guard ─────────────────────────────────────────────────────────────

def test_resolve_price_writes_nothing(db):
    pid = _mk_product(db, "purity guard", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    before = db.total_changes
    pl.resolve_price(db, product_id=pid, today=TODAY)
    assert db.total_changes == before
    assert db.in_transaction is False

    # control: a deliberate write DOES bump total_changes -- proves the
    # counter can move, so the assertion above is not vacuous.
    db.execute("UPDATE products SET updated_at = updated_at WHERE id = ?", (pid,))
    db.commit()
    assert db.total_changes > before


def test_resolve_price_readonly_connection_ok(tmp_db):
    """Run the same call through a PRAGMA query_only=1 connection, where any
    write would raise -- an additional pure-function guard beyond
    total_changes. (No CLI module exists yet -- Phase 1b -- so this opens
    its own read-only connection on the same tmp DB file rather than
    referencing a not-yet-built CLI.)"""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        pid = _mk_product(conn, "readonly guard", unit_type='ตัว', base=100.0, cost=60.0)
        _clear_pid(conn, pid)
        conn.execute("PRAGMA query_only = 1")
        out = pl.resolve_price(conn, product_id=pid, today=TODAY)
        assert out['answer']['price_per_unit'] == 100.0
    finally:
        conn.close()
