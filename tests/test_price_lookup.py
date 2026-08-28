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
import json
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
    # qty=1 โหล = 12 pieces >= bundle_buy 12 (bundle_unit 'ใบ' has no
    # unit_conversions row -> falls back to ratio 1.0, i.e. already pieces)
    # -> the bundle applies (I2, review round 1).
    assert out['answer']['free_units'] == {'buy': 12, 'free': 1, 'unit': 'ใบ', 'applies': True}


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


def test_fixture_discriminates_doc_base_from_doc_no(db, monkeypatch):
    """NOT a standing regression guard (review round 1, I6): this
    monkeypatches evidence_filter with a HAND-BROKEN copy (doc_base
    swapped for doc_no) and asserts the broken copy behaves wrongly. It
    proves the FIXTURE below can tell a doc_base-keyed filter apart from a
    doc_no-keyed one -- i.e. that this fixture is capable of catching that
    specific class of regression IF one is introduced -- not that the
    real `evidence_filter` in price_lookup.py is currently correct. If
    the real function regressed to doc_no-keying, this test would still
    pass (it never calls the real function). The actual standing guards
    that exercise the real, unmodified code are
    test_r4_population_excludes_every_bad_shape and
    test_r4b_base_changed_epoch_window_and_pre_epoch (below) -- those call
    pl.evidence_filter / pl._window directly and would go red on a real
    regression."""
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


def test_fixture_discriminates_epoch_clamp(db, monkeypatch):
    """NOT a standing regression guard (review round 1, I6): this
    monkeypatches `_window` with a HAND-BROKEN copy (the max(epoch, ...)
    clamp removed from both the 365d and 730d steps) and asserts the
    broken copy lets the window reach before the epoch. It proves this
    fixture can tell a clamped `_window` apart from an unclamped one --
    not that the real `_window` in price_lookup.py is currently correct;
    it never calls the real function. The actual standing guard is
    test_r4b_base_changed_epoch_window_and_pre_epoch (above), which calls
    the real, unmodified resolve_price and would go red on a real
    regression."""
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


# ══════════════════════════════════════════════════════════════════════════
# Review round 1 (task-1-report.md fix log) — C1-C3, I1-I5, I7, tier-epoch
# ruling. Each test's finding code is in its name.
# ══════════════════════════════════════════════════════════════════════════

# ── C1: unresolvable caller-asked unit raises, never silently ratio=1.0 ────

def test_c1_unresolvable_unit_raises(db):
    pid = _mk_product(db, "C1 unresolvable unit", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    with pytest.raises(ValueError) as exc_info:
        pl.resolve_price(db, product_id=pid, unit='ลัง', today=TODAY)
    msg = str(exc_info.value)
    assert 'ลัง' in msg
    assert str(pid) in msg

    # control: unit == unit_type never raises, ratio stays trivially 1.0
    out = pl.resolve_price(db, product_id=pid, unit='ตัว', today=TODAY)
    assert out['unit']['ratio'] == 1.0
    assert out['unit']['ratio_source'] == 'none'


# ── C2: promo_stale is None (not True) with no active price promo ──────────

def test_c2_promo_stale_none_without_price_promo(db):
    pid = _mk_product(db, "C2 no promo stale", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    for i in range(3):
        _bill(db, pid=pid, customer_code=f'TST-C2-{i}', customer_name='ลูกค้า C2',
              date_iso=_days_ago(5 + i), qty=1, unit='ตัว', unit_price=150.0, vat_type=1, net=150.0)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['context']['promo_stale'] is None
    assert out['context']['promo_last_used'] is None
    assert 'promo_stale' not in [f['code'] for f in out['flags']]

    # control: the SAME bill shape, but WITH a percent promo -> stale True
    # (list=100, promo=90, all 3 bills at 150 > 90*1.01)
    pid2 = _mk_product(db, "C2 with promo stale control", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid2)
    _promo(db, pid2, promo_type='percent', discount_value=10.0, date_start='2026-06-01', is_active=1)
    for i in range(3):
        _bill(db, pid=pid2, customer_code=f'TST-C2b-{i}', customer_name='ลูกค้า C2b',
              date_iso=_days_ago(5 + i), qty=1, unit='ตัว', unit_price=150.0, vat_type=1, net=150.0)
    out2 = pl.resolve_price(db, product_id=pid2, today=TODAY)
    assert out2['context']['promo_stale'] is True


# ── C3: cost/floor numbers never leak into flag text or anything outside
#        `internal` ───────────────────────────────────────────────────────

def test_c3_cost_never_leaks_outside_internal(db):
    pid = _mk_product(db, "C3 cost leak check", unit_type='ตัว', base=100.0, cost=63.47)
    _clear_pid(db, pid)
    out = pl.resolve_price(db, product_id=pid, extra_disc=0.5, today=TODAY)  # price 50 < cost 63.47
    assert 'below_cost' in [f['code'] for f in out['flags']]
    assert out['internal']['cost_per_unit'] == 63.47
    non_internal = {k: v for k, v in out.items() if k != 'internal'}
    serialized = json.dumps(non_internal, ensure_ascii=False)
    assert '63.47' not in serialized
    # below_cost_by stays where it belongs -- inside internal
    assert out['internal']['below_cost_by'] == round(63.47 - 50.0, 2)


def test_c3_new_floor_flag_text_has_no_number(db):
    pid = _mk_product(db, "C3 new floor text", unit_type='ตัว', base=100.0, cost=10.0)
    _clear_pid(db, pid)
    _bill(db, pid=pid, customer_code='TST-C3nf', customer_name='ลูกค้า C3nf',
          date_iso=_days_ago(30), qty=1, unit='ตัว', unit_price=77.0, vat_type=1, net=77.0)
    out = pl.resolve_price(db, product_id=pid, extra_disc=0.5, today=TODAY)  # price 50 < lowest 77
    nf = next(f for f in out['flags'] if f['code'] == 'new_floor')
    assert '77' not in nf['text']
    assert out['context']['lowest']['cash_per_unit'] == 77.0  # the number still lives here


# ── I1: dozen-only quoting converts qty into the answer unit ───────────────

def test_i1_dozen_only_qty_conversion_whole_dozen(db):
    pid = _mk_product(db, "I1 dozen-only qty whole", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    _tier(db, pid, '1 โหล', 460.0)
    out = pl.resolve_price(db, product_id=pid, qty=12, today=TODAY)
    assert out['answer']['unit'] == 'โหล'
    assert out['answer']['qty'] == 1.0
    assert out['answer']['line_total'] == 460.0
    assert 'pack_only' not in [f['code'] for f in out['flags']]


def test_i1_dozen_only_qty_conversion_fractional_flags_pack_only(db):
    pid = _mk_product(db, "I1 dozen-only qty fractional", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    _tier(db, pid, '1 โหล', 460.0)
    out = pl.resolve_price(db, product_id=pid, qty=15, today=TODAY)
    assert out['answer']['qty'] == 1.25
    assert out['answer']['line_total'] == round(460.0 * 1.25, 2)
    assert 'pack_only' in [f['code'] for f in out['flags']]


# ── I2: bundle multiplier gated on the ask reaching the threshold in pieces ─

def test_i2_bundle_multiplier_gated_on_qty(db):
    pid = _mk_product(db, "I2 bundle gate", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _promo(db, pid, promo_type='bundle', bundle_buy=12, bundle_free=1,
           date_start='2026-06-01', is_active=1)

    # 1 ตัว (1 piece) < 12-piece threshold -> does not apply
    out_piece = pl.resolve_price(db, product_id=pid, unit='ตัว', qty=1, today=TODAY)
    assert out_piece['internal']['cost_side'] == round(10.0, 2)
    assert out_piece['internal']['margin_incl_free_units'] is False
    assert out_piece['answer']['free_units']['applies'] is False

    # 1 โหล (qty=1, asked โหล -> 12 pieces) == 12-piece threshold -> applies
    out_1dz = pl.resolve_price(db, product_id=pid, unit='โหล', qty=1, today=TODAY)
    assert out_1dz['internal']['cost_side'] == round(10.0 * 12 * 13 / 12, 2)
    assert out_1dz['answer']['free_units']['applies'] is True

    # 2 โหล (24 pieces) > threshold -> applies
    out_2dz = pl.resolve_price(db, product_id=pid, unit='โหล', qty=2, today=TODAY)
    assert out_2dz['answer']['free_units']['applies'] is True


# ── I3: below_cost compares against cost_price x ratio, not cost_side ──────

def test_i3_below_cost_compares_against_cost_x_ratio_not_cost_side(db):
    pid = _mk_product(db, "I3 below cost ratio", unit_type='ตัว', base=20.0, cost=10.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    _promo(db, pid, promo_type='bundle', bundle_buy=12, bundle_free=1,
           date_start='2026-06-01', is_active=1)
    # cost_per_unit = 10*12 = 120; cost_side = 120*13/12 = 130 (bundle applies at qty=1 โหล = 12 pieces)
    cust = _mk_customer(db, 'TST-I3a', 'ลูกค้า I3a')
    _clear_customer_pid(db, cust, pid)
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้า I3a',
          date_iso=_days_ago(5), qty=1, unit='โหล', unit_price=125.0, vat_type=1, net=125.0)
    out = pl.resolve_price(db, product_id=pid, customer_code=cust, unit='โหล', qty=1, today=TODAY)
    assert out['answer']['price_per_unit'] == 125.0  # between cost_per_unit(120) and cost_side(130)
    assert out['internal']['cost_per_unit'] == 120.0
    assert out['internal']['cost_side'] == 130.0
    assert 'below_cost' not in [f['code'] for f in out['flags']]

    # control: price BELOW cost_per_unit(120) -> flags
    cust2 = _mk_customer(db, 'TST-I3b', 'ลูกค้า I3b')
    _clear_customer_pid(db, cust2, pid)
    _bill(db, pid=pid, customer_code=cust2, customer_name='ลูกค้า I3b',
          date_iso=_days_ago(5), qty=1, unit='โหล', unit_price=110.0, vat_type=1, net=110.0)
    out2 = pl.resolve_price(db, product_id=pid, customer_code=cust2, unit='โหล', qty=1, today=TODAY)
    assert out2['answer']['price_per_unit'] == 110.0
    assert 'below_cost' in [f['code'] for f in out2['flags']]


# ── I4: latest_evidence ignores future-dated bills ──────────────────────────

def test_i4_latest_evidence_ignores_future_dated_bills(db):
    pid = _mk_product(db, "I4 future bill", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    cust = _mk_customer(db, 'TST-I4', 'ลูกค้า I4')
    _clear_customer_pid(db, cust, pid)
    future_date = (date.fromisoformat(TODAY) + timedelta(days=30)).isoformat()
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้า I4',
          date_iso=future_date, qty=1, unit='ตัว', unit_price=999.0, vat_type=1, net=999.0)
    out = pl.resolve_price(db, product_id=pid, customer_code=cust, today=TODAY)
    assert out['answer']['basis'] != 'last_paid'
    assert out['customer']['last'] is None

    # control: a bill dated exactly TODAY is used
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้า I4',
          date_iso=TODAY, qty=1, unit='ตัว', unit_price=88.0, vat_type=1, net=88.0)
    out2 = pl.resolve_price(db, product_id=pid, customer_code=cust, today=TODAY)
    assert out2['answer']['basis'] == 'last_paid'
    assert out2['answer']['price_per_unit'] == 88.0


# ── I5: epochs_for (the public bulk API) actually exercised + returns ──────

def test_i5_epochs_for_bulk(db):
    pid_with = _mk_product(db, "I5 epoch bulk with", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid_with)
    change_date = _days_ago(10)
    _base_price_history(db, pid_with, changed_at=change_date + " 09:00:00", old=80.0, new=100.0)

    pid_without = _mk_product(db, "I5 epoch bulk without", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid_without)

    result = pl.epochs_for(db, [pid_with, pid_without],
                            {pid_with: 'ตัว', pid_without: 'ตัว'}, today=TODAY)
    assert result[pid_with] == change_date
    assert result[pid_without] is None

    # today is positional-or-keyword, not keyword-only (I5) -- prove it
    result_positional = pl.epochs_for(db, [pid_with], {pid_with: 'ตัว'}, TODAY)
    assert result_positional[pid_with] == change_date


# ── I7: R3 cross-unit conversion, hand-computed oracles with bill_ratio != 1

def test_i7_cross_unit_conversion_dozen_bill_answered_in_piece(db):
    pid = _mk_product(db, "I7 cross unit dozen to piece", unit_type='ตัว', base=30.0, cost=15.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    cust = _mk_customer(db, 'TST-I7a', 'ลูกค้า I7a')
    _clear_customer_pid(db, cust, pid)
    # 1 โหล billed at net 360 (no VAT) -> per piece = 360/12 = 30
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้า I7a',
          date_iso=_days_ago(5), qty=1, unit='โหล', unit_price=360.0, vat_type=1, net=360.0)
    result = pl.latest_evidence(db, pid, cust, _days_ago(365), unit='ตัว', today=TODAY)
    assert result['cash_per_unit'] == 30.00


def test_i7_cross_unit_conversion_piece_bill_answered_in_dozen(db):
    pid = _mk_product(db, "I7 cross unit piece to dozen", unit_type='ตัว', base=30.0, cost=15.0)
    _clear_pid(db, pid)
    _uc(db, pid, 'โหล', 12.0)
    cust = _mk_customer(db, 'TST-I7b', 'ลูกค้า I7b')
    _clear_customer_pid(db, cust, pid)
    # 1 ตัว billed at net 30 -> per โหล = 30*12 = 360
    _bill(db, pid=pid, customer_code=cust, customer_name='ลูกค้า I7b',
          date_iso=_days_ago(5), qty=1, unit='ตัว', unit_price=30.0, vat_type=1, net=30.0)
    result = pl.latest_evidence(db, pid, cust, _days_ago(365), unit='โหล', today=TODAY)
    assert result['cash_per_unit'] == 360.00


# ── Ruling: tier epoch keys on PRICE changes only, not note/sort_order ─────

def test_ruling_tier_epoch_price_only(db):
    pid = _mk_product(db, "Ruling tier epoch price-only", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    tier_id = _tier(db, pid, '1 โหล', 230.0)
    price_change_date = _days_ago(10)
    _stamp_tier_audit(db, tier_id, pid, price_change_date + " 09:00:00")  # INSERT -> always counts

    # A later note-only edit must NOT move the epoch
    note_only_date = _days_ago(3)
    db.execute(
        "INSERT INTO audit_log (table_name, row_id, action, changed_fields, created_at) "
        "VALUES ('product_price_tiers', ?, 'UPDATE', '{\"note\": [null, \"x\"]}', ?)",
        (tier_id, note_only_date + " 09:00:00"))
    db.commit()

    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['window']['from'] == price_change_date  # note-only edit ignored

    # control: a price-changing UPDATE DOES move the epoch
    price_update_date = _days_ago(2)
    db.execute(
        "INSERT INTO audit_log (table_name, row_id, action, changed_fields, created_at) "
        "VALUES ('product_price_tiers', ?, 'UPDATE', '{\"price\": [230.0, 250.0]}', ?)",
        (tier_id, price_update_date + " 09:00:00"))
    db.commit()
    out2 = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out2['window']['from'] == price_update_date


# ══════════════════════════════════════════════════════════════════════════
# Review round 2 — C1 not fully addressed + the ordering regression it
# introduced. Fix shape: tier lookup FIRST, ratio can be None
# ('ratio_source'='unknown'), every ratio-dependent number degrades to
# None/skipped rather than being derived from a fabricated ratio of 1.0.
#
# Fixture shape matches two REAL live products found during round 1's
# report (pid 459 'ลูกรีเวท DOME Sendai 4-4', tier '1 กล่อง'=480; pid 718
# 'บานพับเหล็ก KPS 1.5in', tier '1 โหลคู่'=65) -- base=0, unit_type='ตัว',
# ONE tier whose unit is NOT 'โหล' and has NO unit_conversions row.
# ══════════════════════════════════════════════════════════════════════════

def _mk_box_only_product(db, name, *, tier_price=500.0, cost=2.0):
    """A 459/718-shaped fixture: base=0, unit_type='ตัว', a single tier
    labeled '1 กล่อง' (a box -- no derivable piece count), no
    unit_conversions row at all."""
    pid = _mk_product(db, name, unit_type='ตัว', base=0.0, cost=cost)
    _clear_pid(db, pid)
    _tier(db, pid, '1 กล่อง', tier_price)
    return pid


def test_r2b_a_direct_tier_unit_ask_no_ratio_no_raise(db):
    """(a) ask unit='กล่อง', qty=3 -> no raise, list_for_unit 500, answer.qty
    3, line_total 1500, internal.cost_per_unit is None, no below_cost,
    breadcrumb contains 'กล่อง' and not 'ชิ้น'."""
    pid = _mk_box_only_product(db, "R2b box direct ask")
    out = pl.resolve_price(db, product_id=pid, unit='กล่อง', qty=3, today=TODAY)
    assert out['list']['list_for_unit'] == 500.0
    assert out['list']['list_source'] == 'tier'
    assert out['unit']['ratio'] is None
    assert out['unit']['ratio_source'] == 'unknown'
    assert out['answer']['qty'] == 3
    assert out['answer']['line_total'] == 1500.0
    assert out['internal']['cost_per_unit'] is None
    assert out['internal']['cost_side'] is None
    assert out['internal']['note'] == 'no piece ratio for this pack unit'
    assert 'below_cost' not in [f['code'] for f in out['flags']]
    breadcrumb_text = ' '.join(out['answer']['breadcrumb'])
    assert 'กล่อง' in breadcrumb_text
    assert 'ชิ้น' not in breadcrumb_text


def test_r2b_b_dozen_only_fallback_no_ratio(db):
    """(b) ask unit='ตัว' (default piece), qty=12 -> answers at 500/กล่อง
    (dozen-only fallback), answer.qty is None, line_total is None,
    pack_only flag contains 'กล่อง', internal.cost_per_unit is None, and
    the serialized result contains neither the wrong-multiply '6000' nor
    the raw cost '2.0' anywhere."""
    pid = _mk_box_only_product(db, "R2b box dozen-only fallback")
    out = pl.resolve_price(db, product_id=pid, unit='ตัว', qty=12, today=TODAY)
    assert out['list']['list_source'] == 'dozen-only'
    assert out['answer']['unit'] == 'กล่อง'
    assert out['list']['list_for_unit'] == 500.0
    assert out['answer']['qty'] is None
    assert out['answer']['line_total'] is None
    pack_only = next(f for f in out['flags'] if f['code'] == 'pack_only')
    assert 'กล่อง' in pack_only['text']
    assert out['internal']['cost_per_unit'] is None
    serialized = json.dumps(out, ensure_ascii=False)
    assert '6000' not in serialized   # 12 (pieces) x 500 -- the round-1 bug's wrong line_total
    assert '2.0' not in serialized    # the raw cost_price never leaks out


def test_r2b_c_unresolvable_unit_message_lists_tier_only_units(db):
    """(c) ask unit='ลัง' on the box-only fixture -> still raises, and the
    message lists 'กล่อง' among the units that DO resolve (round 2 fix to
    the C1 error message)."""
    pid = _mk_box_only_product(db, "R2b box unresolvable ask")
    with pytest.raises(ValueError) as exc_info:
        pl.resolve_price(db, product_id=pid, unit='ลัง', today=TODAY)
    assert 'กล่อง' in str(exc_info.value)


def test_r2b_d_control_dozen_tier_unaffected(db):
    """(d) control: the ORIGINAL โหล-tier dozen-only shape (ratio known via
    tier-implied) is completely unaffected by the round-2 restructure --
    keeps its ≈/ชิ้น breadcrumb, internal.cost_per_unit == cost x 12, and
    the round-1 I1 qty conversion (12 ตัว -> 1 โหล)."""
    pid = _mk_product(db, "R2b control dozen tier", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    _tier(db, pid, '1 โหล', 460.0)
    out = pl.resolve_price(db, product_id=pid, qty=12, today=TODAY)
    assert out['unit']['ratio'] == 12.0
    assert out['unit']['ratio_source'] == 'tier-implied'
    assert out['answer']['unit'] == 'โหล'
    assert out['answer']['qty'] == 1.0
    assert out['answer']['line_total'] == 460.0
    assert out['internal']['cost_per_unit'] == round(10.0 * 12, 2)
    assert out['internal']['note'] is None
    breadcrumb_text = ' '.join(out['answer']['breadcrumb'])
    assert 'ชิ้น' in breadcrumb_text


# ══════════════════════════════════════════════════════════════════════════
# Review round 3 — a `fixed` price promo silently dropped when ratio is
# None, but the breadcrumb still announced it (no flag, list.price_promo
# still non-None beside an unchanged list_after_promo).
# ══════════════════════════════════════════════════════════════════════════

def test_r3_fixed_promo_not_convertible_when_ratio_none(db):
    """(1) 459-shaped fixture + fixed promo discount_value=18 -> ratio is
    None (a กล่อง tier, no unit_conversions row) -> the promo cannot be
    converted to a per-กล่อง price. list_after_promo stays the plain tier
    price, price_promo_applied is False, promo_not_convertible flag
    fires, and the breadcrumb does NOT announce 'ราคาพิเศษ'."""
    pid = _mk_box_only_product(db, "R3 box fixed promo not convertible")
    _promo(db, pid, promo_type='fixed', discount_value=18.0, date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, unit='กล่อง', today=TODAY)
    assert out['list']['list_after_promo'] == 500.0
    assert out['list']['price_promo_applied'] is False
    codes = [f['code'] for f in out['flags']]
    assert 'promo_not_convertible' in codes
    breadcrumb_text = ' '.join(out['answer']['breadcrumb'])
    assert 'ราคาพิเศษ' not in breadcrumb_text


def test_r3_percent_promo_still_applies_when_ratio_none(db):
    """(2) control: the SAME box-only product with a PERCENT promo (10%)
    instead -- percent never needs ratio, so it applies normally: 450,
    price_promo_applied True, no promo_not_convertible flag, breadcrumb
    carries the discount line."""
    pid = _mk_box_only_product(db, "R3 box percent promo control")
    _promo(db, pid, promo_type='percent', discount_value=10.0, date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, unit='กล่อง', today=TODAY)
    assert out['list']['list_after_promo'] == 450.0
    assert out['list']['price_promo_applied'] is True
    assert 'promo_not_convertible' not in [f['code'] for f in out['flags']]
    breadcrumb_text = ' '.join(out['answer']['breadcrumb'])
    assert 'ลด' in breadcrumb_text


def test_r3_fixed_promo_applies_normally_when_ratio_known(db):
    """(3) control: the โหล-tier dozen-only product (ratio known, 12) with
    the SAME fixed promo (18) -> applies normally: 18*12=216,
    price_promo_applied True."""
    pid = _mk_product(db, "R3 dozen fixed promo control", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    _tier(db, pid, '1 โหล', 460.0)
    _promo(db, pid, promo_type='fixed', discount_value=18.0, date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['list']['list_after_promo'] == 216.0
    assert out['list']['price_promo_applied'] is True
    assert 'promo_not_convertible' not in [f['code'] for f in out['flags']]


# ══════════════════════════════════════════════════════════════════════════
# PR C / 2e — the call card and the resolver stop disagreeing.
# C1: two shared helpers, tested directly (not only through resolve_price).
# ══════════════════════════════════════════════════════════════════════════

# ── C1a: apply_price_promo (public, pure — no DB needed) ────────────────────

def test_apply_price_promo_none_passthrough():
    assert pl.apply_price_promo(100.0, 1.0, None) == 100.0


def test_apply_price_promo_percent():
    promo = {'promo_type': 'percent', 'discount_value': 10.0}
    assert pl.apply_price_promo(200.0, 1.0, promo) == 180.0


def test_apply_price_promo_fixed_uses_ratio():
    """fixed's discount_value is per-PIECE -- must be multiplied by ratio
    to become the per-answer-unit price (a dozen line: 18/piece * 12)."""
    promo = {'promo_type': 'fixed', 'discount_value': 18.0}
    assert pl.apply_price_promo(240.0, 12.0, promo) == 216.0


def test_apply_price_promo_fixed_ratio_none_left_unapplied():
    """When ratio is unknown (a tier answers the price but no piece
    equivalent is derivable) a fixed promo cannot be converted -- leave
    list_for_unit unchanged rather than guessing ratio=1."""
    promo = {'promo_type': 'fixed', 'discount_value': 18.0}
    assert pl.apply_price_promo(500.0, None, promo) == 500.0


def test_apply_price_promo_mixed_treated_as_percent():
    """A 'mixed' row's discount_value is a PERCENT (see the module
    docstring) -- same branch as 'percent', never needs ratio."""
    promo = {'promo_type': 'mixed', 'discount_value': 20.0}
    assert pl.apply_price_promo(100.0, None, promo) == 80.0


def test_apply_price_promo_bundle_no_discount_value_unchanged():
    """bundle/gift promos (and a 'mixed' row with discount_value NULL)
    never change per-unit price."""
    promo = {'promo_type': 'bundle', 'discount_value': None}
    assert pl.apply_price_promo(100.0, 1.0, promo) == 100.0


# ── C1b: batch_active_promos_by_class (tested directly, then through both
#         resolve_price and call_card._assemble_products) ───────────────────

def test_batch_promos_matches_single_product_get_active_promos_by_class(db):
    """Direct parity check: the batch selector must agree with
    models.promotions.get_active_promos_by_class for the same product —
    same predicate, same per-slot ORDER BY id DESC selection, just batched."""
    pid1 = _mk_product(db, "C1b batch price only", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid1)
    _promo(db, pid1, promo_type='percent', discount_value=10.0, date_start='2026-06-01', is_active=1)

    pid2 = _mk_product(db, "C1b batch qty only", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid2)
    _promo(db, pid2, promo_type='bundle', bundle_buy=10, bundle_free=1,
           date_start='2026-06-01', is_active=1)

    batch = pl.batch_active_promos_by_class(db, [pid1, pid2], TODAY)
    for pid in (pid1, pid2):
        expected = promo_models.get_active_promos_by_class(pid, TODAY, db)
        got = batch[pid]
        assert (got[0]['id'] if got[0] else None) == (expected[0]['id'] if expected[0] else None)
        assert (got[1]['id'] if got[1] else None) == (expected[1]['id'] if expected[1] else None)


def test_batch_promos_qty_never_shadows_price_even_when_created_later(db):
    """C1's headline bug: a price promo created BEFORE a qty promo must
    still win the price slot -- each slot is selected independently
    (ORDER BY id DESC per slot), never one ORDER BY over all promo_types."""
    pid = _mk_product(db, "C1b price then qty", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='percent', discount_value=10.0,
           date_start='2026-06-01', is_active=1)  # created first (lower id)
    _promo(db, pid, promo_type='bundle', bundle_buy=10, bundle_free=1,
           date_start='2026-06-01', is_active=1)  # created after (higher id)

    price_promo, qty_promo = pl.batch_active_promos_by_class(db, [pid], TODAY)[pid]
    assert price_promo is not None and price_promo['promo_type'] == 'percent'
    assert qty_promo is not None and qty_promo['promo_type'] == 'bundle'


def test_batch_promos_no_promo_is_none_pair(db):
    pid = _mk_product(db, "C1b no promo", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    assert pl.batch_active_promos_by_class(db, [pid], TODAY)[pid] == (None, None)


def test_batch_promos_excludes_closed_and_scheduled(db):
    """is_active/date filter applies per-slot in the batch, same as the
    single-product selector -- a date-closed row must not be selected."""
    pid = _mk_product(db, "C1b closed excluded", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='percent', discount_value=10.0,
           date_start='2026-01-01', date_end='2026-02-01', is_active=1)  # closed by date
    price_promo, _qty = pl.batch_active_promos_by_class(db, [pid], TODAY)[pid]
    assert price_promo is None


def test_batch_promos_empty_product_ids_returns_empty_dict(db):
    assert pl.batch_active_promos_by_class(db, [], TODAY) == {}


def test_resolve_price_routes_through_batch_selector(db):
    """The resolver itself is routed through batch_active_promos_by_class
    (not calling get_active_promos_by_class directly any more) -- the
    existing R2/R9 tests already exercise this; this pins the specific
    qty-never-shadows-price case at the resolve_price level too."""
    pid = _mk_product(db, "C1b resolver routing", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    _promo(db, pid, promo_type='percent', discount_value=10.0,
           date_start='2026-06-01', is_active=1)
    _promo(db, pid, promo_type='bundle', bundle_buy=10, bundle_free=1,
           date_start='2026-06-01', is_active=1)
    out = pl.resolve_price(db, product_id=pid, today=TODAY)
    assert out['list']['price_promo']['promo_type'] == 'percent'
    assert out['list']['qty_promo']['promo_type'] == 'bundle'
    assert out['list']['list_after_promo'] == 90.0


# ── C2: epochs_for_pairs — one epoch per (product_id, unit) pair ────────────

def test_epochs_for_pairs_two_units_different_epochs(db):
    """The plan's own C2 test: ONE product at TWO units, each with its own
    tier-driven epoch. unit_by_pid (epochs_for) can only hold one unit per
    product and would overwrite one epoch with the other's -- pairs keep
    them independent."""
    pid = _mk_product(db, "C2 two units", unit_type='ตัว', base=0.0, cost=10.0)
    _clear_pid(db, pid)
    tier_a = _tier(db, pid, '1 โหล', 230.0, sort_order=100)
    tier_b = _tier(db, pid, '1 กล่อง', 500.0, sort_order=200)
    epoch_a = _days_ago(10)
    epoch_b = _days_ago(40)
    _stamp_tier_audit(db, tier_a, pid, epoch_a + " 09:00:00")
    _stamp_tier_audit(db, tier_b, pid, epoch_b + " 09:00:00")

    result = pl.epochs_for_pairs(db, [(pid, 'โหล'), (pid, 'กล่อง')], today=TODAY)
    assert result[(pid, 'โหล')] == epoch_a
    assert result[(pid, 'กล่อง')] == epoch_b


def test_epochs_for_pairs_matches_epochs_for_single_unit(db):
    """Control: for the ordinary one-unit-per-product case, epochs_for_pairs
    must agree with epochs_for exactly (same _epoch_candidates reduction)."""
    pid = _mk_product(db, "C2 parity with epochs_for", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    change_date = _days_ago(10)
    _base_price_history(db, pid, changed_at=change_date + " 09:00:00", old=80.0, new=100.0)

    bulk = pl.epochs_for(db, [pid], {pid: 'ตัว'}, today=TODAY)
    pairs = pl.epochs_for_pairs(db, [(pid, 'ตัว')], today=TODAY)
    assert pairs[(pid, 'ตัว')] == bulk[pid] == change_date


def test_epochs_for_pairs_none_when_no_epoch_source(db):
    pid = _mk_product(db, "C2 no epoch", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(db, pid)
    assert pl.epochs_for_pairs(db, [(pid, 'ตัว')], today=TODAY)[(pid, 'ตัว')] is None
