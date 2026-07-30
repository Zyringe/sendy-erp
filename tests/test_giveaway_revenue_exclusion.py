"""Documents invoiced in error must not count as revenue (migration 142).

Three วรสวัสดิ์ giveaway invoices (IV6900401/402/403, 2026-03-11) were booked
as sales by mistake — goods given away free. Put wrote them off 2026-06-24;
his accountant ruled 2026-07-30 that Express will issue NO credit note, so
Sendy has to carry the correction itself.

Bad debt is the opposite case and is NOT touched: the sale happened, the
customer simply never paid, so revenue stays and the loss lands as an expense.
Only rows flagged `ar_writeoffs.excludes_revenue = 1` are suppressed.

The money-formula rule (see .claude/rules/verification-discipline.md) requires
a number to tie to an INDEPENDENT oracle, not to a re-reading of the formula —
so the reconciliation tests below rebuild the expected figure from raw rows
rather than calling the same helper the code under test uses.
"""
import os
import sqlite3

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')
os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_PASSWORD', 'test-only-admin')

import sales_filters


GIVEAWAY = ('IV6900401', 'IV6900402', 'IV6900403')
BAD_DEBT = ('IV6701775', 'IV6800934', 'IV6801241')   # real sales, unpaid


# ── Schema + data (fixture DB carries the live schema, migration applied) ────

def test_column_exists_and_defaults_to_zero(empty_db_conn):
    cols = {r[1]: r for r in empty_db_conn.execute("PRAGMA table_info(ar_writeoffs)")}
    assert 'excludes_revenue' in cols, "migration 142 not applied to the fixture schema"
    assert cols['excludes_revenue'][4] == '0', "must default to 0 — flagging is opt-in"
    assert cols['excludes_revenue'][3] == 1, "must be NOT NULL (NULL breaks NOT IN)"


def test_only_the_giveaway_docs_are_flagged(live_conn):
    flagged = {r[0] for r in live_conn.execute(
        "SELECT doc_no FROM ar_writeoffs WHERE excludes_revenue = 1")}
    assert flagged == set(GIVEAWAY)
    still_revenue = {r[0] for r in live_conn.execute(
        "SELECT doc_no FROM ar_writeoffs WHERE excludes_revenue = 0 AND type = 'expense'")}
    assert set(BAD_DEBT) <= still_revenue, "bad debt must keep its revenue"


# ── The clause itself ────────────────────────────────────────────────────────

def test_clause_handles_alias_and_bare_table():
    assert 'COALESCE(doc_base, doc_no)' in sales_filters.not_a_sale_clause()
    assert 'COALESCE(st.doc_base, st.doc_no)' in sales_filters.not_a_sale_clause('st')


def test_revenue_filter_keeps_all_three_exclusions():
    f = sales_filters.revenue_filter()
    assert "NOT LIKE 'SR%'" in f
    assert "NOT LIKE 'HS%'" in f
    assert 'excludes_revenue = 1' in f


def test_subquery_source_column_is_not_null(live_conn):
    """If ar_writeoffs.doc_no ever became nullable, `NOT IN (SELECT ...)` would
    evaluate to NULL for every row and ALL revenue would silently read 0."""
    col = {r[1]: r for r in live_conn.execute("PRAGMA table_info(ar_writeoffs)")}['doc_no']
    assert col[3] == 1, "doc_no must stay NOT NULL — see migration 095"


# ── Independent-oracle reconciliation ────────────────────────────────────────

MARCH = ('2026-03-01', '2026-03-31')


def _oracle_march_revenue(conn, *, exclude_giveaway):
    """Rebuild March revenue from raw rows, listing the giveaway doc numbers
    literally instead of consulting ar_writeoffs — an independent path, so a
    bug in the flag or the clause cannot make this agree by construction."""
    skip = " AND doc_base NOT IN ('IV6900401','IV6900402','IV6900403')" if exclude_giveaway else ""
    return conn.execute(f"""
        SELECT ROUND(COALESCE(SUM(net), 0), 2) FROM sales_transactions
         WHERE date_iso >= ? AND date_iso <= ?
           AND doc_base IS NOT NULL
           AND doc_base NOT LIKE 'SR%' AND doc_base NOT LIKE 'HS%'{skip}
    """, MARCH).fetchone()[0]


def test_march_revenue_drops_by_exactly_the_giveaway(live_conn):
    before = _oracle_march_revenue(live_conn, exclude_giveaway=False)
    after = _oracle_march_revenue(live_conn, exclude_giveaway=True)
    giveaway = live_conn.execute(
        "SELECT ROUND(SUM(net), 2) FROM sales_transactions WHERE doc_base IN (?,?,?)",
        GIVEAWAY).fetchone()[0]
    assert round(before - after, 2) == giveaway, "the delta must be the giveaway, nothing else"
    assert giveaway == 154122.80


def test_filter_matches_the_oracle_to_the_satang(live_conn):
    """The shipped clause and the hand-built oracle must land on the same number."""
    via_filter = live_conn.execute(f"""
        SELECT ROUND(COALESCE(SUM(net), 0), 2) FROM sales_transactions
         WHERE date_iso >= ? AND date_iso <= ? AND {sales_filters.revenue_filter()}
    """, MARCH).fetchone()[0]
    assert via_filter == _oracle_march_revenue(live_conn, exclude_giveaway=True)


def test_cogs_is_not_filtered_so_gross_profit_is_right(live_conn):
    """A giveaway's goods left the warehouse, so their cost must stay. Gross
    profit for the month should fall by the full invoiced amount — if COGS were
    filtered too, the margin would be silently handed back."""
    cogs = live_conn.execute("""
        SELECT ROUND(SUM(st.qty * COALESCE(p.cost_price, 0)), 2)
          FROM sales_transactions st LEFT JOIN products p ON p.id = st.product_id
         WHERE st.doc_base IN (?,?,?)""", GIVEAWAY).fetchone()[0]
    assert cogs > 0, "these lines carry real cost — nothing to protect otherwise"

    rev_before = _oracle_march_revenue(live_conn, exclude_giveaway=False)
    rev_after = _oracle_march_revenue(live_conn, exclude_giveaway=True)
    gp_delta = (rev_after - 0) - (rev_before - 0)      # COGS identical both sides
    assert round(gp_delta, 2) == -154122.80


def test_bad_debt_revenue_survives_the_filter(live_conn):
    """The three unpaid-but-real invoices must still be counted."""
    kept = live_conn.execute(f"""
        SELECT ROUND(COALESCE(SUM(net), 0), 2) FROM sales_transactions
         WHERE doc_base IN (?,?,?) AND {sales_filters.revenue_filter()}
    """, BAD_DEBT).fetchone()[0]
    raw = live_conn.execute(
        "SELECT ROUND(SUM(net), 2) FROM sales_transactions WHERE doc_base IN (?,?,?)",
        BAD_DEBT).fetchone()[0]
    assert kept == raw == 38477.00


# ── Cross-page agreement (the invariant that broke on 2026-07-21) ────────────

def test_all_revenue_surfaces_report_the_same_march(live_conn):
    """/revenue, /accounting and the financial-health trend must land on one
    number. They drifted apart once before (the /accounting v2 double-subtraction
    of returns) — this pins them together through the shared filter."""
    import datetime
    import cashflow
    import revenue
    import models.accounting as accounting
    import models.financial_health as financial_health

    oracle = _oracle_march_revenue(live_conn, exclude_giveaway=True)

    rev = revenue.revenue_summary(date_from=MARCH[0], date_to=MARCH[1])['total_revenue']
    acc = accounting.get_accounting_summary(MARCH[0], MARCH[1])['sales_net']
    # /revenue renders this series alongside its own KPI — they were computed
    # by two different filters before migration 142 and disagreed on the page.
    cf = cashflow.revenue_by_month(date_from=MARCH[0], date_to=MARCH[1])[0]['revenue']

    # financial_health joined this set on 2026-07-30 (Put, option ข). It had
    # never filtered returns or opening balances, so its trend read ฿3,688 high
    # on March alone — the last page still disagreeing after the giveaway fix.
    fin = financial_health.get_trailing_months(
        n=1, as_of_date=datetime.date(2026, 4, 15))[0]['revenue']

    assert round(rev, 2) == round(acc, 2) == round(cf, 2) == round(fin, 2) == oracle


def test_cash_in_is_deliberately_not_revenue(live_conn):
    """Guards against someone 'fixing' the /cashflow chart into agreement:
    cash received and revenue earned are different concepts and SHOULD differ."""
    import cashflow
    cash = cashflow.cash_in_by_month(date_from=MARCH[0], date_to=MARCH[1])
    rev = cashflow.revenue_by_month(date_from=MARCH[0], date_to=MARCH[1])
    assert cash and rev
    assert cash[0]['cash_in'] != rev[0]['revenue']


def test_customer_page_drops_the_giveaway(live_conn):
    """วรสวัสดิ์ never bought those goods, so their customer total must fall by
    exactly the giveaway — no more, no less."""
    import models.customers as customers
    summary = customers.get_customer_summary('วรสวัสดิ์ ฮาร์ดแวร์')['summary']
    raw = live_conn.execute(
        "SELECT ROUND(COALESCE(SUM(net), 0), 2) FROM sales_transactions "
        "WHERE customer = ?", ('วรสวัสดิ์ ฮาร์ดแวร์',)).fetchone()[0]
    assert round(raw - summary['total_net'], 2) == 154122.80


# ── Fixtures ─────────────────────────────────────────────────────────────────

_LIVE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'inventory_app', 'instance', 'inventory.db')


@pytest.fixture
def live_conn():
    """Read-only handle on the local DB. The reconciliation tests assert on the
    real วรสวัสดิ์ figures, so they skip where that data isn't present (CI)."""
    if not os.path.exists(_LIVE):
        pytest.skip('live DB not present')
    conn = sqlite3.connect(f'file:{_LIVE}?mode=ro', uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM sales_transactions "
                         "WHERE doc_base IN (?,?,?)", GIVEAWAY).fetchone()[0]
        if not n:
            pytest.skip('giveaway documents not in this DB')
        yield conn
    finally:
        conn.close()


@pytest.fixture
def empty_db_conn(tmp_path):
    """Fresh DB built from data/schema.sql — pins that the regenerated schema
    carries the new column (a stale schema.sql never self-heals)."""
    schema = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'data', 'schema.sql')
    if not os.path.exists(schema):
        pytest.skip('schema.sql not present')
    conn = sqlite3.connect(str(tmp_path / 'fresh.db'))
    conn.executescript(open(schema, encoding='utf-8').read())
    try:
        yield conn
    finally:
        conn.close()
