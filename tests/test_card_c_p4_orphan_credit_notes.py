"""Card C, P4 (Put A1, 2026-09-30): a credit note filed without a customer code
belongs to the one code that carries the same exact bill name.

Read-time only, no data written. The PROD shape (plan section 2.3): 21 SR lines, 8 bill
names, each with exactly one coded twin. Fixture, every number hand-counted:

  customer N  code X    IV-N1 2026-01-10  P 10 ตัว ฿1,000
  customer N  NO code   SR-N1 2026-02-01  P  2 ตัว ฿200    -> attaches to X
  customer M  code Y1   IV-M1 2026-01-11  P  5 ตัว ฿500
  customer M  code Y2   IV-M2 2026-01-12  P  5 ตัว ฿600    (M has TWO codes)
  customer M  NO code   SR-M1 2026-02-02  P  1 ตัว ฿50     -> stays an orphan (no guessing)
  customer Q  NO code   SR-Q1 2026-02-03  P  1 ตัว ฿70     -> orphan, no twin at all
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import call_card
import models
import purchase_history as ph
from tests._purchase_history_fixture import add_line, mk_product

N, M, Q = 'ร้านพีสี่ เอ็น', 'ร้านพีสี่ เอ็ม', 'ร้านพีสี่ คิว'
X, Y1, Y2 = 'ZP4X', 'ZP4Y1', 'ZP4Y2'


@pytest.fixture
def conn(empty_db_conn):
    c = empty_db_conn
    p = mk_product(c, 'สินค้าพีสี่')
    for code, name in ((X, N), (Y1, M), (Y2, M)):
        c.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (code, name))
    add_line(c, doc_base='IV-N1', date_iso='2026-01-10', pid=p, qty=10, net=1000, customer=N, code=X)
    add_line(c, doc_base='SR-N1', date_iso='2026-02-01', pid=p, qty=2, net=200, customer=N, code=None)
    add_line(c, doc_base='IV-M1', date_iso='2026-01-11', pid=p, qty=5, net=500, customer=M, code=Y1)
    add_line(c, doc_base='IV-M2', date_iso='2026-01-12', pid=p, qty=5, net=600, customer=M, code=Y2)
    add_line(c, doc_base='SR-M1', date_iso='2026-02-02', pid=p, qty=1, net=50, customer=M, code=None)
    add_line(c, doc_base='SR-Q1', date_iso='2026-02-03', pid=p, qty=1, net=70, customer=Q, code=None)
    c.commit()
    return c


def test_history_of_the_twin_includes_the_credit_note(conn):
    h = ph.history(conn, X)
    assert h['totals']['purchase_total'] == 800            # 1000 - 200
    assert 'SR-N1' in {d['doc_base'] for d in h['documents']}
    assert h['totals']['doc_count'] == 2
    assert h['totals']['purchase_count'] == 1, 'CONTROL: the credit note is still not a purchase'


def test_the_resolved_name_is_no_longer_a_key(conn):
    hs = ph.histories(conn)
    assert N not in hs, 'phantom entry for a name that resolves to a code'
    assert hs[X]['purchase_total'] == 800
    assert ph.history(conn, N)['totals']['doc_count'] == 0


def test_a_name_with_two_codes_stays_an_orphan(conn):
    hs = ph.histories(conn)
    assert hs[M]['purchase_total'] == -50, 'the SR under a two-code name must not be guessed onto a code'
    assert hs[Y1]['purchase_total'] == 500 and hs[Y2]['purchase_total'] == 600
    assert hs[Q]['purchase_total'] == -70, 'CONTROL: a name with no code anywhere is an orphan'


def test_call_universe_drops_only_the_resolved_name(conn):
    keys = {r['customer_code'] for r in call_card.get_call_list(conn, spend_window='all')}
    assert N not in keys
    assert {X, Y1, Y2, M, Q} <= keys, 'CONTROL: every other entry stays'


def test_customers_list_has_no_phantom_row_for_the_resolved_name(conn, monkeypatch):
    monkeypatch.setattr(models.customers, 'get_connection', lambda: conn, raising=True)
    rows, _total = models.get_customers(per_page=1000)
    by_code = {r['customer_code']: r for r in rows}
    assert by_code[X]['total_net'] == 800 and by_code[X]['doc_count'] == 2
    phantom = by_code[None]
    assert phantom['total_net'] == -50 - 70, 'the NULL-code row sums only the unresolved names'
    assert phantom['doc_count'] == 2


def test_call_card_header_matches_the_customer_page(conn):
    card = call_card.get_card(conn, X)['summary']['summary']
    page = ph.history(conn, X)['totals']
    assert card['total_net'] == 800 == page['purchase_total']


# ── P5: the call card agrees with /call (#699 review W4), the empty key (nit) ──

def test_the_card_of_a_two_code_name_is_the_orphan_not_a_guessed_code(conn):
    """/call lists M at -50 (its code-less SR); the card used to resolve the name to
    ONE of its codes (LIMIT 1) and show that code's whole history."""
    listed = ph.histories(conn)[M]['purchase_total']
    shown = call_card.get_card(conn, M)['summary']['summary']['total_net']
    assert listed == -50 and shown == listed


def test_a_second_code_on_a_resolved_name_detaches_the_card_too(conn):
    """N's SR joined X because N carried one code. Give N a second code and the SR is
    an orphan again: /call lists N at -200, and the card of N must say the same."""
    p = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    add_line(conn, doc_base='IV-N2', date_iso='2026-01-20', pid=p, qty=1, net=40,
             customer=N, code='ZP4X2')
    conn.commit()
    assert ph.histories(conn)[N]['purchase_total'] == -200
    assert call_card.get_card(conn, N)['summary']['summary']['total_net'] == -200
    assert call_card.get_card(conn, X)['summary']['summary']['total_net'] == 1000, \
        'CONTROL: X no longer carries the credit note'


def test_the_empty_key_matches_no_row(conn):
    """`history('')` used to return EVERY code-less row (11 docs, -34,686 on PROD)."""
    for key in ('', None):
        h = ph.history(conn, key)
        assert h['totals']['doc_count'] == 0 and h['documents'] == [] and h['products'] == []
        assert ph.totals(conn, key)['doc_count'] == 0
        assert ph.documents(conn, key) == [] and ph.products(conn, key) == []
        assert ph.has_invoiced_in_error(conn, key) is False


# ── P5: speed, pinned as statement counts (a timing assertion would flake) ──────

class _Counting:
    """A connection that records every statement; everything else is the real one."""

    def __init__(self, inner):
        self.inner, self.sql = inner, []

    def execute(self, sql, *args):
        self.sql.append(sql)
        return self.inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.inner, name)


_A1_LOOKUP = 'HAVING COUNT(DISTINCT TRIM(k.customer_code))'    # customer_key_sql's own text


def test_history_looks_the_a1_rule_up_once_not_once_per_statement(conn):
    """#699 review W3: history() issued ~9 statements and each rebuilt the attached-name
    list; PROD went 23 -> 44 ms. Once per call, whatever the statement count."""
    c = _Counting(conn)
    ph.history(c, X)
    assert sum(_A1_LOOKUP in q for q in c.sql) == 1, 'the A1 lookup is not once per call'
    assert len(c.sql) >= 8, 'CONTROL: history() still issues its statements'
    ph.totals(c, X)
    assert sum(_A1_LOOKUP in q for q in c.sql) == 2, 'totals() adds exactly its own lookup'


def test_the_call_card_computes_the_product_rows_once(conn):
    c = _Counting(conn)
    call_card.get_card(c, X)
    assert sum('AS net_counted' in q for q in c.sql) == 1, \
        'get_card read the per-product rows more than once'
