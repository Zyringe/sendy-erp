"""Nobody types the return-document prefix (SR / GR) except document_kind.

Card D of the round-3 architecture review: 'SR%' / 'GR%' had been re-typed at
~35 sites (sales_filters alone had nine), so a new document prefix was a
many-file edit and the copies had already diverged in NULL handling. The rule
now lives in document_kind.py; this sweep is what keeps it there.

Two shapes are census'd, over string VALUES and calls (comments and docstrings
are not code, so prose that mentions the prefix is fine):
  * a LIKE pattern 'SR%' / 'GR%' inside any string
  * <expr>.startswith('SR' | 'GR' | ('SR', ...)) -- the Python twin

Enumerating `startswith` only for these two literals is deliberate: `'SR'` as
a bare value is also the paid_invoices.doc_kind enum and a display-label key,
which are not prefix tests.

Not seen, by design: report-format PARSERS whose pattern names the document kind as
part of a text layout (parse_weekly._SR_MASTER_RE, models/payments.py's
`(?:IV|SR)\\S+`, the DBF builders' RECTYP tests). They read a report line, they
do not decide what a return is; the one skip-the-returns rule among them
(parse_weekly._SR_DOC_LINE) is routed through document_kind.return_prefix.

Exemptions carry a reason. The HS exclusions ("a cash sale is never a
receivable") are a different question and are not this sweep's business.
"""
import ast
import re

import pytest

from tests import _census

# file -> (exact number of hits it is allowed, why). A COUNT, not a whole-file
# pass, so reverting a migrated line in the same file still goes red (review of
# #678, W2).
EXEMPT = {
    'parse_weekly.py': (1,
        '_SR_MASTER_RE parses the SR credit-note MASTER row of the Express report '
        'layout (SR number, date, customer, ...): a text-format parser, not a '
        'decision about what a return is. The skip-the-returns rule in the same '
        'file, _SR_DOC_LINE, is built from document_kind.return_prefix.'),
    'scripts/parse_express_credit_notes.py': (1,
        '_MAIN_RE parses the GR record row of an Express purchase credit-note '
        'report (report-layout regex in a standalone script).'),
    'scripts/2026_09_19_fix_pack_ratios_592.py': (1,
        'Dated one-off correction script for a measured set (#592); it ran '
        'once and is kept for the record, not maintained.'),
    'scripts/reimport_2026_04_28/import_credit_notes.py': (3,
        'Historical re-import script for 2026-04-28; superseded by the app '
        'importer (inventory_app/import_credit_notes.py), which is migrated.'),
    'scripts/parse_express_purchase_history.py': (2,
        'Standalone parser script for a one-time purchase-history backfill; '
        'runs outside the app and does not import its modules.'),
}

# `SR%` (a LIKE pattern) or `SR\d` (a regex over a report line).
_LIKE_PREFIX = re.compile(r'\b(?:SR|GR)(?:%|\\d)', re.IGNORECASE)
_PREFIX_LITERALS = ('SR', 'GR')


def _hits(src):
    """[(lineno, what)] for every return-prefix test written by hand in `src`."""
    tree = ast.parse(src)
    prose = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in prose and _LIKE_PREFIX.search(node.value)):
            out.append((node.lineno, 'LIKE pattern'))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'startswith' and node.args):
            arg = node.args[0]
            lits = arg.elts if isinstance(arg, ast.Tuple) else [arg]
            if any(isinstance(x, ast.Constant) and x.value in _PREFIX_LITERALS
                   for x in lits):
                out.append((node.lineno, 'startswith'))
    return out


def _census_files():
    return {rel: path for rel, path in _census.py_files(include_scripts=True)}


def test_no_hand_typed_return_prefix_outside_document_kind():
    found = {}
    for rel, path in _census_files().items():
        hits = _hits(_census.read(path))
        if rel in EXEMPT:
            continue        # count pinned by test_exemptions_are_real_and_reasoned
        if hits:
            found[rel] = hits
    assert not found, (
        "These files type the SR/GR return prefix themselves -- call "
        "document_kind.is_return_sql / not_return_sql / is_return instead, "
        "or add an EXEMPT entry saying why:\n  "
        + "\n  ".join(f'{r}: {h}' for r, h in sorted(found.items())))


def test_exemptions_are_real_and_reasoned():
    files = _census_files()
    stale = []
    for rel, (n, why) in EXEMPT.items():
        assert len(why) > 40, f'{rel}: explain WHY it is exempt'
        if rel not in files:
            stale.append(f'{rel} (file no longer exists)')
            continue
        got = len(_hits(_census.read(files[rel])))
        if got != n:
            stale.append(f'{rel} (exempt for {n} hit(s), now has {got})')
    assert not stale, "Fix these EXEMPT entries:\n  " + "\n  ".join(stale)


SEEN = {
    'plain_like':      "q = 'SELECT 1 WHERE doc_base LIKE \"SR%\"'",
    'not_like':        'q = "WHERE doc_no NOT LIKE \'SR%\'"',
    'gr':              'q = "WHERE doc_base LIKE \'GR%\'"',
    'lowercase':       'q = "doc_base like \'sr%\'"',
    'in_fstring':      'q = f"WHERE {p}doc_base LIKE \'SR%\'"',
    'triple_quoted':   'q = """\n  SELECT 1\n  WHERE doc_base LIKE \'SR%\'\n"""',
    'regex_over_a_line': "r = re.compile(r'\\d{2}\\s+SR\\d')",
    'startswith':      "x = doc.startswith('SR')",
    'startswith_gr':   'x = (d or "").startswith("GR")',
    'startswith_tuple': "x = doc.startswith(('IV', 'SR'))",
}
NOT_SEEN = {
    'docstring':       'def f():\n    """doc_base LIKE \'SR%\' is a return."""\n    return 1',
    'module_docstring': '"""NOT LIKE \'SR%\'"""\nx = 1',
    'comment':         "x = 1  # doc_base LIKE 'SR%'",
    'hs_exclusion':    "q = \"doc_base NOT LIKE 'HS%'\"",
    'iv_prefix':       "x = doc.startswith('IV')",
    'enum_value':      "kind = 'SR'",
}


@pytest.mark.parametrize('shape', sorted(SEEN))
def test_the_sweep_sees_every_shape(shape):
    assert _hits(SEEN[shape]), f'{shape}: the sweep is blind to this shape'


@pytest.mark.parametrize('shape', sorted(NOT_SEEN))
def test_the_sweep_ignores_prose_and_other_prefixes(shape):
    assert not _hits(NOT_SEEN[shape]), f'{shape}: false positive'
