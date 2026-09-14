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

⚠ Deliberately NOT in this module: Express's document rounding (ปัดขึ้น 2
ตำแหน่ง on a quotation's unit ex-VAT price, see
.claude/rules/quoting-and-pricing.md). The raw carve-out is here (section C);
rounding it for a printed line is the renderer's rule. `sales_doc.html`'s VAT
line is a third thing again, the tax itself: it reads `VAT_RATE` since #485.
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


def test_the_multiplier_is_derived_from_the_rate_and_stays_bit_identical():
    """#485: the rate is the source and the multiplier is built from it, and
    the multiplier must stay the exact double the literal 1.07 was, or
    cash_sql's text and every SUM over it could move. Only this direction is
    safe: 1.07 - 1 is 0.07000000000000006, which can shift a VAT line at a
    half-satang boundary."""
    assert vat_math.VAT_RATE == 0.07
    assert vat_math.VAT_MULTIPLIER == 1 + vat_math.VAT_RATE
    assert vat_math.VAT_MULTIPLIER.hex() == (1.07).hex()


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


# ── C · the inverse: cash → net ──────────────────────────────────────────────
#
# models/vat_sub.py::compute_badge carved VAT out of a VAT-inclusive price to
# compare it against a book cost kept ex-VAT. It was left out of this module on
# the grounds that the carve-out "rounds differently — ปัดขึ้น 2 decimals". That
# is true of the QUOTATION renderer (the skill's express_ex_vat, which reproduces
# how a human keys a line into Express); it is not true of compute_badge, which
# divided, divided again by unit_ratio, and compared with `>` — no rounding
# anywhere in the path. So the raw carve-out belongs here with its twin.
# ⚠ #485 deleted compute_badge (no production caller; the live badge is the JS
# in templates/vat_sub/product_view.html, which divides by VAT_MULTIPLIER), so
# nothing in production calls net_from_cash now.

@pytest.mark.parametrize('cash,expected', [
    (107.0, 100.0),
    (0.0, 0.0),
    (-53.5, -50.0),          # a credit line keeps its sign, like cash_from_net
])
def test_net_from_cash_carves_the_vat_back_out(cash, expected):
    assert vat_math.net_from_cash(cash) == pytest.approx(expected)


def test_net_from_cash_passes_none_through():
    """Same NULL contract as cash_from_net — "no data" is not ฿0.00."""
    assert vat_math.net_from_cash(None) is None


def test_net_from_cash_round_trips_with_cash_from_net():
    assert vat_math.net_from_cash(vat_math.cash_from_net(16.5, 2)) == pytest.approx(16.5)


def test_net_from_cash_does_not_apply_express_document_rounding():
    """The line Express PRINTS for ฿16.50 incl. VAT is 15.43 (ปัดขึ้น, keyed by
    a human). This function is the raw quotient — 15.4205… — because its caller
    compares against a cost, it does not print a document. Anything that renders
    a quotation must round for itself; that rule lives with the renderer."""
    assert vat_math.net_from_cash(16.50) == pytest.approx(16.50 / 1.07)
    assert round(vat_math.net_from_cash(16.50), 2) == 15.42      # not 15.43


# ── D · the money gate: one real invoice, to the satang ──────────────────────
#
# IV6901440 · 27/08/2569 · แยก VAT. Read from PROD 2026-09-09: doc_base
# 'IV6901440' is 4 lines, every one vat_type=2. The paper bill says ฿14,209.29.
# Characterisation, not a new behaviour — it pins the arithmetic to a document
# a human can hold, which nothing else in the suite does.

IV6901440_NETS = [5540.25, 4221.65, 1406.99, 2110.82]


def test_a_real_vat_invoice_reconciles_to_the_satang():
    total_net = sum(IV6901440_NETS)
    assert total_net == pytest.approx(13279.71)
    assert round(vat_math.cash_from_net(total_net, 2), 2) == 14209.29


def test_vat_lands_on_the_invoice_total_not_on_each_line():
    """Rounding each line's cash before summing over-collects by one satang.

    Express rounds the line NET (already stored that way) and computes one VAT
    line from the total. A caller that rounds per line stops matching the bill.
    """
    per_line = sum(round(vat_math.cash_from_net(n, 2), 2) for n in IV6901440_NETS)
    assert round(per_line, 2) == 14209.30
    assert round(per_line, 2) != 14209.29


def test_the_invoice_page_prints_the_paper_bills_vat_line(tmp_db):
    """#485: the same bill through the page that shows it. /sales/doc's VAT
    line takes its rate from vat_math now; the paper says VAT ฿929.58 and
    ฿14,209.29 in total.

    The four lines are seeded under a doc_base the dev DB cannot hold (asserted
    absent first), so nothing is inherited. Cells are read by position among
    the tfoot's money cells, not by their labels, which #495 may reword."""
    import re
    import sqlite3
    from app import app as flask_app

    doc = 'IVVATLINE485'
    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_no LIKE ?",
                            (doc + '-%',)).fetchone()[0] == 0, 'fixture would inherit rows'
        conn.executemany(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, customer, qty,"
            " unit, unit_price, vat_type, total, net, synced_to_stock)"
            " VALUES ('2026-08-27', ?, ?, 'ลูกค้าทดสอบ', 1, 'ตัว', ?, 2, ?, ?, 0)",
            [('{}-{}'.format(doc, i), doc, n, n, n)
             for i, n in enumerate(IV6901440_NETS, 1)])
        conn.commit()
    finally:
        conn.close()

    flask_app.config['TESTING'] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    resp = client.get('/sales/doc/' + doc)
    assert resp.status_code == 200
    tfoot = re.search(r'<tfoot>(.*?)</tfoot>', resp.get_data(as_text=True), re.S).group(1)
    money = re.findall(r'<td class="text-end[^"]*">\s*([\d,]+\.\d\d)\s*</td>', tfoot)
    # รวมทั้งสิ้น (total, net) · VAT · ยอดรวมรวม VAT
    assert money == ['13,279.71', '13,279.71', '929.58', '14,209.29']
