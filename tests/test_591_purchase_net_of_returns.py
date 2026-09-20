"""#591 — ยอดซื้อ (what we bought from a supplier) is NET of returns.

The bug: GR (ใบลดหนี้ซื้อ, purchase return/credit note) rows are stored with
POSITIVE net/qty by the DBF importer — same convention as SR on the sales
side — and every purchase_transactions aggregate summed them straight in, so
returns were ADDED instead of subtracted. Measured on prod: 2026 Jan-Sep read
฿699,280.59 raw vs ฿693,428.57 net of returns; /supplier/ไพบูลย์'s header read
฿462,473 against the true ฿426,473.

Every expected figure below is worked by hand from the seeded rows, never
recomputed the way the code does. tmp_db_conn clones the live dev DB WITH its
data, so each test deletes its own supplier's rows first.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

SUPPLIER = 'ทดสอบซัพพลายเออร์591'
SUPPLIER_CODE = 'T591SUP'


def _reset(conn):
    conn.execute("DELETE FROM purchase_transactions WHERE supplier = ?", (SUPPLIER,))


def _line(conn, doc_base, *, net, qty, date_iso, unit_price=None, unit='ตัว', pid=None):
    """One purchase line in the real shape: doc_no == doc_base for a
    single-line document (verified against prod's GR rows, none of which
    carry a '-N' suffix). A return is stored exactly like the real ones:
    positive qty and net."""
    conn.execute(
        "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id, "
        " supplier, supplier_code, qty, unit, unit_price, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, doc_base, doc_base, pid, SUPPLIER, SUPPLIER_CODE, qty, unit,
         unit_price if unit_price is not None else net / qty, net, net))


# Fixture, worked by hand:
#   RR59101  2026-01-10  real purchase     net +1,000  qty +10
#   RR59102  2026-02-10  real purchase     net +2,000  qty +20
#   GR59103  2026-02-15  return of some of RR59102's goods  net +500  qty +5
#             (stored POSITIVE, as the real importer writes it)
# ยอดซื้อ (net of returns) = 1,000 + 2,000 − 500 = 2,500 ; qty = 10 + 20 − 5 = 25
EXPECTED_NET = 2500.0
EXPECTED_QTY = 25.0


def _seed(conn):
    _reset(conn)
    _line(conn, 'RR59101', net=1000, qty=10, date_iso='2026-01-10')
    _line(conn, 'RR59102', net=2000, qty=20, date_iso='2026-02-10')
    _line(conn, 'GR59103', net=500, qty=5, date_iso='2026-02-15')
    conn.commit()


# ── The sign rule, one function at a time ────────────────────────────────────

def test_supplier_summary_subtracts_the_return(tmp_db_conn):
    conn = tmp_db_conn
    _seed(conn)

    import models
    data = models.get_supplier_summary(SUPPLIER)

    assert data['summary']['doc_count'] == 3          # CONTROL: all 3 docs reached the aggregate
    assert data['summary']['total_net'] == pytest.approx(EXPECTED_NET), (
        f'GR59103 (฿500) must be SUBTRACTED: 1,000 + 2,000 − 500 = 2,500, '
        f"not {1000 + 2000 + 500} (added)")
    assert data['summary']['total_qty'] == pytest.approx(EXPECTED_QTY)


def test_supplier_summary_monthly_charts_the_return_month_lower(tmp_db_conn):
    conn = tmp_db_conn
    _seed(conn)

    import models
    data = models.get_supplier_summary(SUPPLIER)
    months = {m['month']: m['total_net'] for m in data['monthly']}
    assert months == pytest.approx({'2026-01': 1000.0, '2026-02': 1500.0}), (
        "February must net the return: 2,000 − 500 = 1,500, not 2,500 (added)")


def test_supplier_summary_docs_list_shows_the_return_negative(tmp_db_conn):
    """The 'docs' list on the supplier page must sum to the SAME total as the
    header — that invariant (broken on the sales side pre-#494: header
    ฿94,140.00 vs its own doc list ฿50,460.00) is exactly what a per-document
    negation is for."""
    conn = tmp_db_conn
    _seed(conn)

    import models
    data = models.get_supplier_summary(SUPPLIER)
    by_doc = {d['doc_no']: d['total_net'] for d in data['docs']}
    assert by_doc == pytest.approx({
        'RR59101': 1000.0, 'RR59102': 2000.0, 'GR59103': -500.0,
    }), 'GR59103 must render as a NEGATIVE document total'
    assert sum(by_doc.values()) == pytest.approx(data['summary']['total_net']), (
        'the doc list must sum to the same figure as the header')


def test_supplier_summary_top_products_subtracts_the_return(tmp_db_conn):
    conn = tmp_db_conn
    _reset(conn)
    # product_id left NULL (FK-enforced column): both lines group under the
    # same (NULL product_id, NULL product_name_raw) key, which is all this
    # test needs — one product row to check the net/qty on.
    _line(conn, 'RR59101', net=1000, qty=10, date_iso='2026-01-10')
    _line(conn, 'GR59103', net=500, qty=5, date_iso='2026-02-15')
    conn.commit()

    import models
    data = models.get_supplier_summary(SUPPLIER)
    assert len(data['top_products']) == 1
    row = data['top_products'][0]
    assert row['total_net'] == pytest.approx(500.0), (
        f"top_products must net the return: 1,000 − 500 = 500, got {row['total_net']}")
    assert row['total_qty'] == pytest.approx(5.0)


def test_get_suppliers_list_subtracts_the_return(tmp_db_conn):
    conn = tmp_db_conn
    _seed(conn)

    import models
    rows, _total = models.get_suppliers(search=SUPPLIER)
    assert len(rows) == 1
    assert rows[0]['total_net'] == pytest.approx(EXPECTED_NET)


def test_get_purchases_summary_subtracts_the_return_across_all_suppliers(tmp_db_conn):
    """The /purchases page total is NOT supplier-scoped — it must still net a
    return that happens to be the only row from this supplier in range. Only
    the DELTA our fixture contributes is asserted, since the cloned dev DB
    carries other real rows in the same date range."""
    conn = tmp_db_conn
    _seed(conn)
    import models

    with_fixture = models.get_purchases_summary(date_from='2026-01-01', date_to='2026-02-28')
    jan_with_fixture = models.get_purchases_summary(date_from='2026-01-01', date_to='2026-01-31')

    _reset(conn)
    conn.commit()
    without_fixture = models.get_purchases_summary(date_from='2026-01-01', date_to='2026-02-28')
    jan_without_fixture = models.get_purchases_summary(date_from='2026-01-01', date_to='2026-01-31')

    # CONTROL: a January-only range must contribute exactly RR59101, not
    # RR59102/GR59103 which land in February.
    assert jan_with_fixture['total_net'] - jan_without_fixture['total_net'] == pytest.approx(1000.0)

    delta = with_fixture['total_net'] - without_fixture['total_net']
    assert delta == pytest.approx(EXPECTED_NET), (
        f'the fixture must contribute exactly ฿2,500 net of its own return, got {delta}')


def test_trade_dashboard_purchases_and_gross_profit_subtract_the_return(tmp_db_conn):
    conn = tmp_db_conn
    _seed(conn)

    import models
    with_fixture = models.get_trade_dashboard(date_from='2026-01-01', date_to='2026-02-28')
    _reset(conn)
    conn.commit()
    without_fixture = models.get_trade_dashboard(date_from='2026-01-01', date_to='2026-02-28')

    net_delta = with_fixture['purchases']['total_net'] - without_fixture['purchases']['total_net']
    qty_delta = with_fixture['purchases']['total_qty'] - without_fixture['purchases']['total_qty']
    assert net_delta == pytest.approx(EXPECTED_NET)
    assert qty_delta == pytest.approx(EXPECTED_QTY)

    # gross_profit = sales − purchases: a return SUBTRACTED from purchases
    # must RAISE gross_profit relative to the "returns added" bug (a supplier
    # return does not reduce our margin the way a bigger purchase would).
    gp_delta = with_fixture['gross_profit'] - without_fixture['gross_profit']
    assert gp_delta == pytest.approx(-EXPECTED_NET), (
        'gross_profit must move by exactly -net_of_returns purchases (sales unchanged)')


# ── The totals say they are net of returns (#626 review) ─────────────────────
#
# Put's call: keep the sign as-is (totals net of returns, individual document
# rows still show their face amount), but the two summary cards must SAY they
# are net of returns — otherwise a GR document listed at its face amount
# right next to a card that already subtracted it reads as a contradiction.

_NET_OF_RETURNS_CAPTION = 'หักใบลดหนี้ซื้อ (GR) แล้ว'


def _admin_client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'
    return c


def test_purchases_page_labels_the_total_as_net_of_returns(tmp_db_conn):
    """/purchases is NOT supplier-scoped, so its total includes whatever else
    the cloned dev DB holds in this date range — compute the expected total
    the same way the route does, rather than assuming it equals our fixture
    alone (see test_get_purchases_summary_subtracts_the_return_across_all_suppliers)."""
    conn = tmp_db_conn
    _seed(conn)
    import models
    expected = models.get_purchases_summary(date_from='2026-01-01', date_to='2026-02-28')
    c = _admin_client()

    html = c.get('/purchases?date_from=2026-01-01&date_to=2026-02-28').get_data(as_text=True)
    # CONTROL: this is really today's (netted) total, not a stale/empty page.
    assert f"{expected['total_net']:,.2f}" in html, 'the netted total itself did not render'
    assert _NET_OF_RETURNS_CAPTION in html, (
        'the /purchases summary card must say it is net of returns, so a GR '
        'row shown at its face amount in the table below is not read as a '
        'contradiction')


def test_supplier_page_labels_the_total_as_net_of_returns(tmp_db_conn):
    conn = tmp_db_conn
    _seed(conn)
    c = _admin_client()

    from urllib.parse import quote
    html = c.get(f'/supplier/{quote(SUPPLIER)}').get_data(as_text=True)
    # CONTROL: this is really this supplier's page, not a not-found/empty one.
    assert f"{EXPECTED_NET:,.2f}" in html, 'the netted ยอดซื้อรวม did not render'
    assert _NET_OF_RETURNS_CAPTION in html, (
        'the supplier page header must say it is net of returns')
