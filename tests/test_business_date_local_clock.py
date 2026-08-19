"""A BUSINESS date must come from the Bangkok clock, never SQLite's bare UTC one.

`app.py` sets TZ='ICT-7' + time.tzset() at import, so inside the running app
`date('now','localtime')`, `date.today()` and `datetime.now()` are all Bangkok.
Bare `date('now')` ignores that entirely and is ALWAYS UTC — so between 00:00
and 07:00 Bangkok it writes YESTERDAY's date. Two sites did that, both on
money-adjacent columns:

  * models/commission.py — `commission_overrides.effective_from` decides which
    sales an override applies to, i.e. what a rep is paid.
  * models/conversions.py — `conversion_cost_log.event_date` is the date the
    WACC walk costs the converted output on.

WHY THE TZ GYMNASTICS: the bug only shows during a 7-hour window, so a test
that just compares against today's date passes ~71% of the day with the bug
still in place — a textbook test-that-cannot-fail. Instead we force the process
into an offset whose LOCAL date provably differs from the UTC date right now
(+/-20h, picked from the current UTC hour so one of them always crosses
midnight), then assert the stored value is the LOCAL one. That fails
deterministically at any hour when the code reads the UTC clock.
"""
import datetime
import sqlite3
import time

import pytest

import models


# -- the deterministic clock skew --------------------------------------------

def _skewed_tz():
    """A POSIX TZ whose local date differs from the UTC date RIGHT NOW.

    POSIX TZ sign is inverted: 'XXX-20' is UTC+20. +20h rolls the date forward
    when the UTC hour is >= 4; -20h rolls it back when the UTC hour is < 20.
    Those two ranges cover all 24 hours, so one always applies.
    """
    utc_hour = datetime.datetime.utcnow().hour
    return 'XXX-20' if utc_hour >= 4 else 'XXX+20'


@pytest.fixture
def skewed_clock(monkeypatch):
    """Run the test under a TZ where local-date != UTC-date, and hand back both.

    Yields (utc_date_iso, local_date_iso) -- asserted to differ, so the test
    cannot silently degrade into comparing a date against itself.
    """
    monkeypatch.setenv('TZ', _skewed_tz())
    time.tzset()
    probe = sqlite3.connect(':memory:')
    utc_date = probe.execute("SELECT date('now')").fetchone()[0]
    local_date = probe.execute("SELECT date('now','localtime')").fetchone()[0]
    probe.close()
    # The whole test rests on these being different. If the skew ever stops
    # working (a libc that rejects the offset), fail loudly here rather than
    # letting every assertion below pass for the wrong reason.
    assert utc_date != local_date, (
        'clock skew did not take effect: both dates are %s' % utc_date)
    try:
        yield utc_date, local_date
    finally:
        time.tzset()          # monkeypatch restores TZ; re-read it


# -- site 1: commission_overrides.effective_from -----------------------------

def test_commission_override_defaults_to_the_bangkok_date(empty_db_conn,
                                                          skewed_clock):
    utc_date, local_date = skewed_clock
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, unit_type) VALUES (901, 'x', 'ตัว')")
    empty_db_conn.commit()

    res = models.create_commission_override(
        {'scope': 'product', 'product_id': '901',
         'rate_kind': 'fixed', 'fixed_per_unit': '5', 'is_active': '1'})
    assert res['ok'], res['error']

    rows = empty_db_conn.execute(
        "SELECT effective_from FROM commission_overrides WHERE id = ?",
        (res['id'],)).fetchall()
    assert len(rows) == 1                       # control: the row exists at all
    assert rows[0]['effective_from'] == local_date, (
        'effective_from should be the Bangkok date %s, got %s (UTC date is %s)'
        % (local_date, rows[0]['effective_from'], utc_date))


def test_commission_override_still_honours_an_explicit_date(empty_db_conn,
                                                            skewed_clock):
    """The fix must not swallow an operator-supplied date -- COALESCE's whole job."""
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, unit_type) VALUES (902, 'y', 'ตัว')")
    empty_db_conn.commit()

    res = models.create_commission_override(
        {'scope': 'product', 'product_id': '902',
         'rate_kind': 'fixed', 'fixed_per_unit': '5', 'is_active': '1',
         'effective_from': '2026-01-15'})
    assert res['ok'], res['error']
    row = empty_db_conn.execute(
        "SELECT effective_from FROM commission_overrides WHERE id = ?",
        (res['id'],)).fetchone()
    assert row['effective_from'] == '2026-01-15'


# -- site 2: conversion_cost_log.event_date ----------------------------------

def _seed_conversion(conn):
    conn.execute("INSERT INTO products (id, product_name, unit_type)"
                 " VALUES (910, 'pack', 'แผง')")
    conn.execute("INSERT INTO products (id, product_name, unit_type)"
                 " VALUES (920, 'loose', 'ตัว')")
    conn.execute("INSERT INTO transactions(product_id, txn_type, quantity_change,"
                 " unit_mode, reference_no, note) VALUES (910,'IN',5,'unit','SEED','seed')")
    cur = conn.execute("INSERT INTO conversion_formulas(name, output_product_id,"
                       " output_qty, is_active) VALUES ('unpack', 920, 2, 1)")
    fid = cur.lastrowid
    conn.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id,"
                 " quantity) VALUES (?, 910, 1)", (fid,))
    conn.commit()
    return fid


def test_conversion_cost_log_event_date_is_the_bangkok_date(empty_db_conn,
                                                            skewed_clock):
    utc_date, local_date = skewed_clock
    fid = _seed_conversion(empty_db_conn)

    ok, msg, _ = models.run_conversion(fid, multiplier=2, run_token='tok-tz-1')
    assert ok, msg

    rows = empty_db_conn.execute(
        "SELECT event_date FROM conversion_cost_log").fetchall()
    assert len(rows) == 1                       # control: the run logged exactly one
    assert rows[0]['event_date'] == local_date, (
        'event_date should be the Bangkok date %s, got %s (UTC date is %s)'
        % (local_date, rows[0]['event_date'], utc_date))
