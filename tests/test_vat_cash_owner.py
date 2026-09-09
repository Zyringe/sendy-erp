"""Card 2 of the 2026-09-08 architecture review — the VAT-to-cash rule has no module.

`net` is always ex-VAT. What the customer actually PAID is `net × 1.07` when
`vat_type = 2`, and `net` otherwise. sendy_erp/CLAUDE.md calls this "Idiom เดียว
ทั้ง codebase" — but it was written out by hand at 23 call sites in 4 spellings
(spaced / unspaced / aliased / `AS spend`), and `tests/test_vat_math.py` imports
nothing from the app: it re-types the SQL and checks its own typing, so deleting
`* 1.07` from a production query left the suite green.

That rule getting inverted once already cost real money visibility: before
2026-05-19 the docs had it backwards, payments_alloc/cashflow used a bare
SUM(net), and every fully-paid `แยก VAT` bill read as "จ่ายเกิน 7%" — about
฿446k of fake customer credit.

⚠ Deliberately NOT in this module: `vat_sub.py`'s `price / 1.07` and the two
templates that do the same. That is the INVERSE direction (carving VAT out of a
VAT-inclusive price) and it rounds differently — ปัดขึ้น 2 ตำแหน่ง per Express,
see .claude/rules/quoting-and-pricing.md. Same constant, different rule.
`sales_doc.html`'s `total_net * 0.07` is a third thing again (the VAT line).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import vat_math


# ── A · the Python side ──────────────────────────────────────────────────────

@pytest.mark.parametrize('net,vat_type,expected', [
    (100.0, 2, 107.0),          # แยก VAT — the only type that adds 7%
    (100.0, 1, 100.0),          # ไม่บวก VAT ตอนเก็บเงิน
    (100.0, 0, 100.0),          # ยกเว้น VAT
    (0.0,   2, 0.0),
    (-50.0, 2, -53.5),          # credit / return lines keep their sign
])
def test_cash_from_net(net, vat_type, expected):
    assert vat_math.cash_from_net(net, vat_type) == pytest.approx(expected)


def test_cash_from_net_does_not_round():
    """Callers round where they always did — some at the row, some after SUM().
    Rounding inside would silently move every one of them."""
    assert vat_math.cash_from_net(0.015, 2) == 0.015 * 1.07


def test_cash_from_net_passes_none_through():
    """SQL yields NULL for a NULL net; the Python twin must not invent 0.0."""
    assert vat_math.cash_from_net(None, 2) is None
    assert vat_math.cash_from_net(None, 1) is None


def test_unknown_vat_type_is_treated_as_no_vat():
    """The SQL is `CASE WHEN vat_type=2 ... ELSE net END` — everything that is
    not 2 falls through. Pin it so a new code cannot silently start adding VAT."""
    assert vat_math.cash_from_net(100.0, 3) == 100.0
    assert vat_math.cash_from_net(100.0, None) == 100.0


# ── B · the SQL side ─────────────────────────────────────────────────────────

def test_cash_sql_unaliased():
    assert vat_math.cash_sql() == "CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END"


def test_cash_sql_prefixes_every_column():
    got = vat_math.cash_sql('st')
    assert got == "CASE WHEN st.vat_type = 2 THEN st.net * 1.07 ELSE st.net END"
    assert 'st.vat_type' in got and 'st.net' in got
    assert ' net ' not in got.replace('st.net', 'X'), 'an unprefixed column leaked'


def test_cash_sql_runs_and_matches_the_literal_it_replaces(tmp_db_conn):
    """Equivalence against the exact string the 15 SQL sites used to carry."""
    literal = "CASE WHEN vat_type=2 THEN net*1.07 ELSE net END"
    rows = tmp_db_conn.execute(
        f"SELECT SUM({vat_math.cash_sql()}) a, SUM({literal}) b FROM sales_transactions"
    ).fetchone()
    assert rows['a'] is not None, 'control: the table must have rows'
    assert rows['a'] == rows['b']


# ── C · the two spellings must not drift ─────────────────────────────────────

def test_sql_and_python_agree_on_every_row(tmp_db_conn):
    """One rule, two languages. This is the guard the old test could not be:
    it executes the app's SQL and calls the app's Python on the same rows."""
    rows = tmp_db_conn.execute(
        f"SELECT net, vat_type, ({vat_math.cash_sql()}) AS sql_cash "
        "FROM sales_transactions WHERE net IS NOT NULL"
    ).fetchall()
    assert len(rows) > 1000, f'control: expected a real dataset, got {len(rows)}'
    assert any(r['vat_type'] == 2 for r in rows), 'control: need แยก VAT rows'
    assert any(r['vat_type'] != 2 for r in rows), 'control: need non-VAT rows'
    bad = [(r['net'], r['vat_type']) for r in rows
           if vat_math.cash_from_net(r['net'], r['vat_type']) != r['sql_cash']]
    assert bad == [], f'{len(bad)} rows disagree, e.g. {bad[:3]}'
