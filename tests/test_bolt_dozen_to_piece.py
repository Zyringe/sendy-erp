"""scripts/2026_08_17_bolt_dozen_to_piece.py — โหล -> ตัว for pids 559/561/562.

A dated one-off that must never run twice is MORE in need of a test, not less:
it mutates products, the stock ledger and the cost ledger, and its first
version shipped to a dev DB with a note that asserted "2 ตัว @ ฿55" on a row
holding 24 ตัว @ ฿4.5833 — false audit evidence produced by the step meant to
keep the record honest. Codex caught it; these tests pin what it got wrong.

The script is driven as a subprocess against a `tmp_db` clone, i.e. exactly the
way a human runs it, so the argument parsing, the exit codes and the
transaction boundary are all in scope.

    ~/.virtualenvs/erp/bin/python -m pytest tests/test_bolt_dozen_to_piece.py -q
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "2026_08_17_bolt_dozen_to_piece.py"
PIDS = (559, 561, 562)
SOURCE = 'script:2026_08_17_bolt_dozen_to_piece'

# (pid, old stock in โหล, old cost, old base, new base) — Put 2026-08-17
EXPECTED = {
    559: dict(stock=2, cost=55.0, base=75.0, new_base=7.0),
    561: dict(stock=1, cost=55.0, base=75.0, new_base=7.0),
    562: dict(stock=4, cost=65.0, base=95.0, new_base=8.0),
}


def run(db, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), '--db', str(db), *args],
        capture_output=True, text=True)


def fingerprint(db):
    """Every product's money + stock columns. Any drift anywhere shows up here."""
    c = sqlite3.connect(db)
    return c.execute(
        "SELECT p.id, p.unit_type, p.cost_price, p.base_sell_price, p.opening_cost, "
        "p.low_stock_threshold, COALESCE(s.quantity,0) FROM products p "
        "LEFT JOIN stock_levels s ON s.product_id=p.id ORDER BY p.id").fetchall()


@pytest.fixture
def db(tmp_db):
    """A disposable clone that still holds the un-converted pre-state."""
    c = sqlite3.connect(tmp_db)
    for pid in PIDS:
        row = c.execute("SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            pytest.skip(f"pid {pid} absent from this DB")
        if row[0] != 'โหล':
            pytest.skip(f"pid {pid} is already {row[0]} — clone is post-conversion")
    return tmp_db


# ── preconditions ─────────────────────────────────────────────────────────────

def test_the_threshold_choice_has_no_default(db):
    """low_stock_threshold changes physical meaning on conversion, so the script
    must refuse to pick for you rather than silently keeping or scaling."""
    r = run(db)
    assert r.returncode != 0
    assert '--threshold' in r.stderr


def test_a_second_application_refuses_and_writes_nothing(db):
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    after_first = fingerprint(db)

    r = run(db, '--threshold', 'keep', '--apply')
    assert r.returncode == 2, r.stdout
    assert 'already ตัว' in r.stdout
    assert fingerprint(db) == after_first, "the refused run still wrote"


def test_drift_in_a_dependency_refuses(db):
    """The v1 script asserted none of this and was safe only by luck."""
    c = sqlite3.connect(db)
    c.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
              "VALUES (559,'1 โหล',75)")
    c.commit()
    before = fingerprint(db)

    r = run(db, '--threshold', 'keep', '--apply')
    assert r.returncode == 2, r.stdout
    assert 'product_price_tiers' in r.stdout
    assert fingerprint(db) == before


def test_a_target_with_sales_history_refuses(db):
    """Batch 2's hard case: quoted history is in dozens, so a blanket x12 would
    rewrite accounting history."""
    c = sqlite3.connect(db)
    c.execute("INSERT INTO sales_transactions (date_iso, doc_no, product_id, qty, unit, "
              "unit_price, net) VALUES ('2026-01-01','TEST-1',559,1,'โหล',75,75)")
    c.commit()
    r = run(db, '--threshold', 'keep', '--apply')
    assert r.returncode == 2, r.stdout
    assert 'sales_transactions' in r.stdout


# ── rehearsal ─────────────────────────────────────────────────────────────────

def test_a_rehearsal_changes_nothing(db):
    before = fingerprint(db)
    r = run(db, '--threshold', 'keep')
    assert r.returncode == 0, r.stdout
    assert 'REHEARSAL' in r.stdout
    assert fingerprint(db) == before


# ── the conversion itself ─────────────────────────────────────────────────────

def test_the_converted_values_are_exact(db):
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    c = sqlite3.connect(db)
    for pid, e in EXPECTED.items():
        unit, cost, base, thr = c.execute(
            "SELECT unit_type, cost_price, base_sell_price, low_stock_threshold "
            "FROM products WHERE id=?", (pid,)).fetchone()
        stock = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                          (pid,)).fetchone()[0]
        assert unit == 'ตัว'
        assert stock == e['stock'] * 12
        assert cost == pytest.approx(e['cost'] / 12)
        assert base == e['new_base']
        assert thr == 10, "keep must leave the threshold number alone"


def test_stock_value_is_conserved(db):
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    c = sqlite3.connect(db)
    for pid, e in EXPECTED.items():
        cost = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        stock = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                          (pid,)).fetchone()[0]
        assert stock * cost == pytest.approx(e['stock'] * e['cost'], abs=0.005)


def test_stock_equals_the_sum_of_the_ledger(db):
    """Value conservation alone would pass even if stock and the ledger drifted
    apart together."""
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    c = sqlite3.connect(db)
    for pid in PIDS:
        stock = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                          (pid,)).fetchone()[0]
        led = c.execute("SELECT SUM(quantity_change) FROM transactions WHERE product_id=?",
                        (pid,)).fetchone()[0]
        assert stock == pytest.approx(led)


def test_the_dozen_price_survives_as_a_catalogue_tier(db):
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    c = sqlite3.connect(db)
    for pid, e in EXPECTED.items():
        rows = c.execute("SELECT qty_label, price FROM product_price_tiers "
                         "WHERE product_id=?", (pid,)).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == '1 โหล'
        assert rows[0][1] == pytest.approx(e['base'])


def test_the_cost_ledger_note_is_truthful(db):
    """THE regression this file exists for.

    v1 produced "ยอดยกมา 2 ตัว @ 55.00 บาท/ตัว" on a row holding 24 ตัว @
    4.5833 — it swapped the unit words and left the numbers. The note must
    carry the ORIGINAL text and a conversion record whose numbers match the
    structured row on BOTH sides.
    """
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    c = sqlite3.connect(db)
    for pid, e in EXPECTED.items():
        note, qty, unit_cost = c.execute(
            "SELECT note, qty_change, unit_cost FROM product_cost_ledger "
            "WHERE product_id=?", (pid,)).fetchone()
        assert 'แปลงหน่วย' in note
        # the original survives
        assert f"@ {e['cost']:.2f} บาท/โหล" in note
        # and both sides of the conversion record match the row
        assert f"{e['stock']:g} โหล" in note
        assert f"{qty:g} ตัว" in note
        assert f"{unit_cost:.6f}/ตัว" in note
        # the exact v1 lie must not be reconstructible
        assert f"{e['stock']:g} ตัว @ ฿{e['cost']:.2f}" not in note


def test_every_price_change_is_attributed(db):
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    c = sqlite3.connect(db)
    for pid in PIDS:
        rows = c.execute(
            "SELECT field_name, source FROM product_price_history WHERE product_id=? "
            "AND date(changed_at)=date('now','localtime')", (pid,)).fetchall()
        assert len(rows) == 2, rows       # cost_price and base_sell_price
        assert {r[0] for r in rows} == {'cost_price', 'base_sell_price'}
        for field, src in rows:
            assert src == SOURCE, f"{field} unattributed"


def test_the_source_context_is_reset_afterwards(db):
    """Left set, the NEXT unrelated price edit anywhere would be stamped with
    this script's name."""
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    c = sqlite3.connect(db)
    assert c.execute("SELECT source FROM price_change_source WHERE id=1").fetchone()[0] is None


def test_no_product_outside_the_plan_moves(db):
    before = {r[0]: r for r in fingerprint(db) if r[0] not in PIDS}
    assert run(db, '--threshold', 'keep', '--apply').returncode == 0
    after = {r[0]: r for r in fingerprint(db) if r[0] not in PIDS}
    assert after == before


def test_the_scale_mode_is_wired_even_though_put_chose_keep(db):
    """CONTROL: `keep` leaving the threshold at 10 would look identical to the
    flag doing nothing at all."""
    assert run(db, '--threshold', 'scale', '--apply').returncode == 0
    c = sqlite3.connect(db)
    for pid in PIDS:
        thr = c.execute("SELECT low_stock_threshold FROM products WHERE id=?",
                        (pid,)).fetchone()[0]
        assert thr == 120
