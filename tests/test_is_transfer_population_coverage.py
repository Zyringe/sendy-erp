"""Every reader of `cashbook_accounts.is_transfer` is declared, with the
population it reads.

Why this exists (#594, ADR 0017): account `904` loses its `is_transfer` flag
and KEEPS `is_active = 0`. From that moment two different questions about the
same row give two different answers, and the flag no longer tells them apart:

  OPERATING  "which money actually happened?"  `is_transfer = 0`, and NO
             is_active clause. A retired account's history is still history —
             that is the ฿205,278.91 ADR 0017 recovers. Every P&L-shaped read.
  PAYABLE    "where may NEW money go?"  `is_active = 1` AND not a transfer
             account. After the flip `is_active = 1` is the ONLY thing keeping
             904 out of a pay-from picker or a salary/commission posting.
  EXPECTED   "which accounts should have been keyed this month?"  the
             incomplete-month reading. Same predicate as PAYABLE, a different
             question: a retired account must never be expected, or every
             month after it closed reads as incomplete forever.
  LABEL      reads the flag off rows it already selected, only to sort, badge
             or split them. Decides no population.
  WRITER     sets the flag (the admin accounts form).
  ONE_OFF    a dated script under scripts/ that reads the flag in its own
             precondition before a single run (e.g. #589's commission
             back-record). Declared so the census stays complete; no probe,
             because nothing calls it after it has run.

The failure this guards is a site silently moving between OPERATING and
PAYABLE: an OPERATING read that gains `is_active = 1` drops 904's recovered
expense straight back out of the statement, and a PAYABLE read that loses it
opens 904 as a payment target. Both look like a one-word tidy.

This file is the CENSUS: every site, how many times it reads the flag, and
which population it answers. A count cannot tell OPERATING from PAYABLE (both
read the flag once — rule "A test that cannot fail" #9 in the brain repo's
verification-discipline.md), so the WHICH half is behavioural and lives in
test_594_account_populations.py: each OPERATING / PAYABLE / EXPECTED site
here must name the probe there that pins it, and that file drives the real
function against a retired account and a conduit account.

⚠ What this census CANNOT see, so a green run says nothing about them:
  - a population decided by a CALLER's argument (`_get_monthly_summary`'s
    `exclude_transfer`, `get_active_cashbook_accounts`' `non_transfer_only`) —
    the probes pin the argument the real caller passes, not every call
  - the flag read positionally (`row[7]`) or through `SELECT *` into a dict
    whose key is then built at run time
  - HTML outside Jinja delimiters: the admin form's `<select name=
    "is_transfer">` is prose to this sweep (its POST handler is counted)
  - anything outside inventory_app/ and scripts/
"""
import ast
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(_ROOT, 'inventory_app')
SCRIPTS = os.path.join(_ROOT, 'scripts')
TEMPLATES = os.path.join(APP, 'templates')

_TOKEN = re.compile(r'\bis_transfer\b')
_SQL_COMMENT = re.compile(r'--[^\n]*|/\*.*?\*/', re.DOTALL)
_JINJA_COMMENT = re.compile(r'\{#.*?#\}', re.DOTALL)
_JINJA_EXPR = re.compile(r'\{\{.*?\}\}|\{%.*?%\}', re.DOTALL)

OPERATING, PAYABLE, EXPECTED, LABEL, WRITER, ONE_OFF = (
    'OPERATING', 'PAYABLE', 'EXPECTED', 'LABEL', 'WRITER', 'ONE_OFF')

# site -> (reads of the flag, population, the probe(s) pinning it, why).
# LABEL / WRITER sites carry no probe: they decide no population.
ALLOWED = {
    # ── OPERATING: money that happened. is_transfer = 0, NO is_active. ──
    'models/accounting.py::get_accounting_summary': (
        2, OPERATING, ('accounting_expenses', 'accounting_prior_period'),
        '/accounting ค่าใช้จ่ายดำเนินงาน and ค่าใช้จ่ายของงวดก่อน: two queries, '
        'ONE population split by belongs_to_period (mig 187). This is where '
        "904's 2026-02 +29,678.91 / 2026-03 +8,100 / 2026-01 prior-period "
        '167,500.00 land (ADR 0017).'),
    'models/financial_health.py::_trailing_overhead': (
        1, OPERATING, ('financial_health_overhead',),
        "break-even's trailing-3-month non-salary opex. get_break_even() is "
        'only called for today, whose window (Jun-Aug 2026) holds no 904 row, '
        'so the live page does not move; an as_of inside Jan-Apr 2026 does.'),
    'blueprints/cashbook.py::_get_monthly_summary': (
        1, OPERATING, ('cashbook_monthly',),
        "/cashbook trend chart + สรุปรายเดือน. The account clause is built "
        'from exclude_transfer; the dashboard passes True.'),
    'blueprints/cashbook.py::_get_category_summary': (
        1, OPERATING, ('cashbook_category',),
        '/cashbook category summary + doughnut, all-time and month-scoped.'),
    'blueprints/cashbook.py::_get_tag_summary': (
        1, OPERATING, ('cashbook_tag',),
        '/cashbook ผู้ใช้-tag summary (tagged expense rows only).'),
    'blueprints/cashbook.py::_expense_by_category_range': (
        1, OPERATING, ('cashbook_range',),
        '/cashbook overspend flags: this month vs last, per category.'),
    'blueprints/cashbook.py::_get_detail_rows': (
        1, OPERATING, ('cashbook_detail',),
        '/cashbook drill-down behind every summary figure above.'),
    'blueprints/cashbook.py::_get_operating_totals': (
        1, OPERATING, ('cashbook_headline',),
        '/cashbook headline รายรับรวม / รายจ่ายรวม / สุทธิเดือนนี้ and the '
        'transfer-category figure beside them (#594). Same population as the '
        'category summary so the cards and the breakdown cannot disagree; '
        'the per-account table stays active-only.'),
    # ── EXPECTED: who should have keyed this month. ──
    'models/accounting.py::_incomplete_months': (
        1, EXPECTED, ('accounting_expected',),
        'เดือนที่ข้อมูลยังไม่ครบ: an account is expected when is_active = 1 '
        'AND is_transfer = 0 and it carried expense in >=3 of the previous 6 '
        'months. 904 stays out because it is inactive, not because of the flag.'),
    # ── PAYABLE: where new money may go. is_active = 1 AND not a transfer. ──
    'blueprints/commission_bp.py::commission_record_payout': (
        1, PAYABLE, ('commission_route',),
        "/commission/payout's up-front account check, before any payout row "
        'is written.'),
    'commission.py::record_payout': (
        1, PAYABLE, ('commission_record_payout',),
        'the commission payout write path itself (is_active checked in Python '
        'just above the flag).'),
    'hr.py::post_salary_payment': (
        1, PAYABLE, ('salary_pay_event',),
        "the salary pay-event ('จ่ายแล้ว') write path (is_active checked in "
        'Python just above the flag).'),
    'hr_queries.py::get_active_cashbook_accounts': (
        1, PAYABLE, ('pay_from_picker',),
        'the pay-from pickers (salary pay-event, commission payout, employee '
        'default pay account) with non_transfer_only=True; is_active = 1 is '
        'unconditional.'),
    # ── LABEL: reads the flag to sort / badge / split, decides no rows. ──
    'blueprints/cashbook.py::_get_accounts_with_totals': (
        2, LABEL, (),
        "/cashbook per-account table: SELECTs the flag and sorts by it. Its "
        'population is `a.is_active = 1`, not the flag, so 904 is not listed '
        'before or after the flip. The headline totals used to be summed from '
        'this table and so missed a closed account; since #594 they come from '
        '_get_operating_totals instead.'),
    'blueprints/cashbook.py::dashboard': (
        2, LABEL, (),
        '/cashbook op/transfer split of the rows _get_accounts_with_totals '
        'already chose.'),
    'blueprints/admin.py::cashbook_account_list': (
        1, LABEL, (),
        '/cashbook-accounts sort order: transfer accounts listed last.'),
    'templates/admin_cashbook_accounts.html': (
        3, LABEL, (),
        '/cashbook-accounts ประเภท column (ดำเนินการ vs พักเงิน/โอน) and the '
        "edit form's selected option. 904 moves from the transfer group to "
        'the operating group here (ADR 0017).'),
    'templates/cashbook/account_ledger.html': (
        1, LABEL, (),
        "/cashbook/account/<id>'s บัญชีพักเงิน/โอน badge."),
    # ── WRITER: the admin form that sets the flag. ──
    'blueprints/admin.py::_cashbook_form_fields': (
        2, WRITER, (),
        "parses the form's is_transfer <select> into 0/1."),
    'blueprints/admin.py::cashbook_account_new': (
        2, WRITER, (),
        'INSERT of a new account with the parsed flag.'),
    'blueprints/admin.py::cashbook_account_edit': (
        2, WRITER, (),
        'UPDATE of an account with the parsed flag.'),
}

POPULATIONS = (OPERATING, PAYABLE, EXPECTED, LABEL, WRITER, ONE_OFF)
PROBED = (OPERATING, PAYABLE, EXPECTED)


# ── reading the flag out of Python ───────────────────────────────────────────

def _prose_ids(tree):
    """ids of every bare string statement (docstrings included): prose."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            for sub in ast.walk(node.value):
                out.add(id(sub))
    return out


def _count(node):
    """Reads of the flag carried by ONE node: a string value (SQL comments
    stripped) or an attribute access."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(_TOKEN.findall(_SQL_COMMENT.sub(' ', node.value)))
    if isinstance(node, ast.Attribute) and node.attr == 'is_transfer':
        return 1
    return 0


def _py_reads(src):
    """{qualname: reads} for one Python source."""
    tree = ast.parse(src)
    prose = _prose_ids(tree)
    out = {}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child.name])
                continue
            if id(child) not in prose:
                n = _count(child)
                if n:
                    key = '.'.join(scope) or '<module>'
                    out[key] = out.get(key, 0) + n
            visit(child, scope)

    visit(tree, [])
    return out


def _template_reads(src):
    """Reads inside Jinja delimiters only; {# #} comments and HTML text are
    prose."""
    src = _JINJA_COMMENT.sub(' ', src)
    return sum(len(_TOKEN.findall(m)) for m in _JINJA_EXPR.findall(src))


def _census():
    found = {}
    for base, prefix in ((APP, ''), (SCRIPTS, 'scripts/')):
        for root, _dirs, names in os.walk(base):
            if any(part in root for part in ('__pycache__', 'instance', 'static')):
                continue
            for n in names:
                path = os.path.join(root, n)
                rel = prefix + os.path.relpath(path, base).replace(os.sep, '/')
                if n.endswith('.py'):
                    with open(path, encoding='utf-8') as f:
                        for func, k in _py_reads(f.read()).items():
                            found[f'{rel}::{func}'] = k
                elif n.endswith('.html') and path.startswith(TEMPLATES):
                    with open(path, encoding='utf-8') as f:
                        k = _template_reads(f.read())
                    if k:
                        found[rel] = k
    return found


# ── the census ───────────────────────────────────────────────────────────────

def test_every_is_transfer_reader_is_declared():
    found = _census()
    declared = {site: n for site, (n, _p, _probes, _why) in ALLOWED.items()}
    undeclared = {s: n for s, n in found.items() if declared.get(s) != n}
    stale = {s: n for s, n in declared.items() if found.get(s) != n}
    assert not undeclared and not stale, (
        'A reader of cashbook_accounts.is_transfer changed. Declare it in '
        'ALLOWED with its read count and population — and if it decides which '
        'money counts (OPERATING) or where money may go (PAYABLE), add a '
        'probe for it in test_594_account_populations.py.\n'
        f'  found but not declared (or count differs): {undeclared}\n'
        f'  declared but not found at that count: {stale}')


def test_the_census_is_not_empty():
    """CONTROL: a sweep that found nothing would satisfy the census above
    only if ALLOWED were emptied with it — pin the size so it cannot."""
    found = _census()
    assert len(found) == len(ALLOWED) == 21, sorted(found)


@pytest.mark.parametrize('site', sorted(ALLOWED))
def test_every_entry_names_a_population_and_a_reason(site):
    _n, population, probes, why = ALLOWED[site]
    assert population in POPULATIONS, site
    assert len(why) > 40, f'{site}: say WHAT question it answers'
    if population in PROBED:
        assert probes, f'{site}: a {population} site needs a behavioural probe'
    else:
        assert not probes, f'{site}: a {population} site decides no population'


def test_every_probed_site_has_its_probe():
    """The census and the behavioural half cannot drift: every probe named
    here exists there under the SAME population, and every probe there is
    named here."""
    from tests import test_594_account_populations as behaviour
    named = {p: pop for (_n, pop, probes, _w) in ALLOWED.values() for p in probes}
    assert set(named) == set(behaviour.PROBES), (
        f'named here only: {set(named) - set(behaviour.PROBES)}; '
        f'probed there only: {set(behaviour.PROBES) - set(named)}')
    mismatched = {p: (pop, behaviour.PROBES[p][0]) for p, pop in named.items()
                  if behaviour.PROBES[p][0] != pop}
    assert not mismatched, f'probe population differs from the site it pins: {mismatched}'


# ── the sweep's own coverage: one rogue source per shape ─────────────────────

READ_SHAPES = {
    'sql where': 'def f(c):\n    return c.execute("SELECT 1 FROM a WHERE a.is_transfer = 0")\n',
    'f-string':  'def f(c, x):\n    return c.execute(f"SELECT {x} FROM a WHERE is_transfer=0")\n',
    'concat':    'def f(c):\n    return c.execute("SELECT 1 FROM a " +\n        "WHERE is_transfer = 0")\n',
    'augassign': 'def f(c):\n    sql = "SELECT 1 FROM a"\n    sql += " AND is_transfer = 0"\n    return sql\n',
    'subscript': 'def f(r):\n    return r["is_transfer"] == 1\n',
    'get':       "def f(r):\n    return r.get('is_transfer')\n",
    'attribute': 'def f(r):\n    return r.is_transfer\n',
    'module':    'Q = "SELECT 1 FROM a WHERE is_transfer = 0"\n',
    'method':    'class R:\n    def f(self, c):\n        return c.execute("SELECT is_transfer FROM a")\n',
}

NOT_READS = {
    'docstring':        'def f():\n    """filters is_transfer = 0"""\n    return 1\n',
    'python comment':   'def f():\n    # is_transfer = 0\n    return 1\n',
    'sql line comment': 'def f(c):\n    return c.execute("SELECT 1 -- was is_transfer = 0\\n FROM a")\n',
    'sql block comment': 'def f(c):\n    return c.execute("SELECT 1 /* is_transfer */ FROM a")\n',
    'longer name':      'def f(r):\n    return r["non_is_transfer_only"]\n',
}


@pytest.mark.parametrize('shape', sorted(READ_SHAPES))
def test_the_sweep_sees_every_read_shape(shape):
    assert sum(_py_reads(READ_SHAPES[shape]).values()) == 1, f'{shape}: unswept'


@pytest.mark.parametrize('shape', sorted(NOT_READS))
def test_the_sweep_ignores_prose(shape):
    assert _py_reads(NOT_READS[shape]) == {}, f'{shape}: false positive'


def test_stripping_a_sql_comment_keeps_the_code_beside_it():
    """CONTROL for the comment cases: a stripper that ate the whole string
    would pass them while blinding the sweep."""
    src = 'def f(c):\n    return c.execute("SELECT 1 -- note\\n WHERE is_transfer = 0")\n'
    assert _py_reads(src) == {'f': 1}


def test_the_sweep_scopes_reads_to_their_function():
    src = ('def a(c):\n    return c.execute("WHERE is_transfer = 0")\n'
           'def b(r):\n    return r["is_transfer"]\n')
    assert _py_reads(src) == {'a': 1, 'b': 1}


@pytest.mark.parametrize('src, n', [
    ('{% if a.is_transfer %}x{% endif %}', 1),
    ('{{ a.is_transfer }}', 1),
    ('<select name="is_transfer">', 0),
    ('ไม่นับบัญชีพักเงิน (is_transfer)', 0),
    ('{# a.is_transfer #}{% if a.is_transfer %}{% endif %}', 1),
])
def test_the_template_sweep_reads_jinja_only(src, n):
    assert _template_reads(src) == n
