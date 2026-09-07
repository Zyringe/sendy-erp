"""Every place that hardcodes an Express report-type key derives from the
registry, or is listed here with a reason.

Why this exists: the report *type* is a real concept with no module. The
vocabulary — sales / purchase / payments_in / payments_out / credit_notes_ar /
credit_notes_ap / ar_snapshot / ap_snapshot / unknown — is spelled out by hand
in several places that must agree, with nothing pinning them equal.

The drift has already cost real money-path damage once, and `import_router.py`
records it in a comment: removing a type from the dropdown while the detector
still emitted it made the browser fall through to the FIRST <option> ('ขาย'),
so a ลูกหนี้คงค้าง report would have been fed to the SALES importer — which
writes sales_transactions and moves stock (Codex review, 2026-08-22).

An architecture review (2026-09-04, card 10) counted seven such lists. A live
enumeration on 2026-09-07 found an EIGHTH the review missed, already
inconsistent when found:

    vat_book_builder.py:303-312   reads per_type['credit_notes_ar']['upserted']
                                  while every sibling key reads ['imported'],
                                  and names its outputs 'sales_imported' /
                                  'purchase_imported' but plain 'payments_in'.

Reading code file-by-file is what missed it. This test does the sweep
mechanically, so the next person who adds a report type (or forgets a surface)
is told at CI time instead of by a wrong number on a page.

WHY THE SWEEP USES SIX KEYS AND NOT ALL NINE
--------------------------------------------
Measured 2026-09-07 over inventory_app/**.py, counting only real string
literals (tokenize, so comments and docstrings do not count):

    payments_in / payments_out            3 files each
    credit_notes_ar / credit_notes_ap     3 files each
    ar_snapshot / ap_snapshot             4 files each
    ---------------------------------------------------
    sales                                11 files
    purchase                              8 files
    unknown                               9 files

`sales`, `purchase` and `unknown` are ordinary words in this codebase — a BSN
weekly file_type, a sales blueprint, a generic sentinel. Sweeping on them would
flag a dozen unrelated files, and the allowlist needed to silence them is
exactly the kind of long list that real drift hides inside. So the sweep runs on
the SIX DISCRIMINATING keys, which nothing else in the app uses.

That is a deliberate trade, and here is why it still catches what matters: a new
hand-maintained list of report types cannot avoid naming the payments and
credit-note types — a "list of report types" holding only sales and purchase is
not one. The six keys are the tripwire; the registry assertion below keeps all
nine honest.

WHAT THIS SWEEP CANNOT SEE, said out loud so nobody reads it as total coverage:
  - a surface that hardcodes ONLY 'sales' / 'purchase' / 'unknown'
  - keys assembled at runtime (f-strings, ''.join, dict comprehension)
  - non-.py sources: the Jinja template builds the type <select>, covered
    separately by tests/test_retired_report_types.py
  - keys arriving from the DB or an operator POST
The enforcement is the registry being the only declaration; this sweep only
moves a forgotten surface's failure into CI.

The allowlist is the record of DELIBERATE exceptions. Adding an entry is fine —
silently leaving a surface undeclared is not. Each entry pins the exact KEYS
that file may hardcode, not merely the file: a file being "known" is not the
same as a new literal inside it being declared.
"""
import os
import tokenize

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

# The full vocabulary the registry must declare. 'unknown' is part of it: it is
# what detect_express_report returns for a file it cannot classify, and the
# dropdown carries it as a real selectable option.
KEYS = frozenset({
    'sales', 'purchase', 'payments_in', 'payments_out',
    'credit_notes_ar', 'credit_notes_ap', 'ar_snapshot', 'ap_snapshot',
    'unknown',
})

# The subset the file sweep runs on — see the docstring for the measurement.
SWEPT_KEYS = frozenset({
    'payments_in', 'payments_out',
    'credit_notes_ar', 'credit_notes_ap',
    'ar_snapshot', 'ap_snapshot',
})

# The single declaration. Every other reader derives from it.
REGISTRY = 'report_types.py'

# path -> (keys it may hardcode, why)
ALLOWED = {
    'import_router.py': (
        frozenset({'payments_in', 'payments_out', 'credit_notes_ar',
                   'credit_notes_ap', 'ar_snapshot', 'ap_snapshot'}),
        'DISPATCH AND RESULT ASSEMBLY, not a list of the vocabulary. Every '
        'LIST here now derives from the registry (RETIRED_REPORT_TYPES, '
        '_EXPRESS_KIND, and the detector walk; the retired REASON moved to '
        'report_types.retired_reason_for, per type). What '
        'remains is code that must name the type it is calling an importer '
        'for: commit_file/preview_file branch to different importers with '
        "different arguments, and commit_express_dbf's return dict binds each "
        'key to its own computed variable and mixes THREE vocabularies '
        '(report types, Express registers, plus reconcile/drift). Putting '
        'those callables in the registry would invert the import direction '
        'for no gain. The key set is pinned exactly, so wiring a NEW type in '
        'here fails this test until the registry is updated too.'),
    'blueprints/bsn.py': (
        frozenset({'payments_in', 'payments_out', 'credit_notes_ar',
                   'credit_notes_ap', 'ar_snapshot', 'ap_snapshot'}),
        '_express_dbf_summary_message builds a Thai one-liner in which every '
        'type has its own word (ขาย / รับชำระ / ลดหนี้ขาย ...), so the message '
        'cannot be generated from the registry without inventing a label '
        'field nothing else wants. It reads per_type[key][field] by hand, '
        'which is the SAME imported-vs-upserted trap that was live in '
        'vat_book_builder - so the field-choice test below pins it to the '
        'registry instead. The labels dict and the removals_ok list are gone; '
        'both read the registry now.'),
    'express_registers.py': (
        frozenset({'ar_snapshot', 'ap_snapshot'}),
        'A DIFFERENT record for a DIFFERENT concept that legitimately shares '
        'two keys. Register(key, label, stale_hint, snapshot_required) '
        'describes an Express REGISTER fed by the daily DBF zip; its six keys '
        'are ar_snapshot, ap_snapshot, billing_notes, bank_cheques, '
        'sales_orders, general_ledger. The overlap is exactly the two types '
        'that were RETIRED as report types BECAUSE they moved to the register '
        'path — so the two records are the two sides of that move, not a '
        'duplication. ReportType.retired_reason points at the register; do '
        'not merge them.'),
}


def _py_files():
    for root, dirs, names in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'instance', 'static')]
        for n in names:
            if n.endswith('.py'):
                yield os.path.join(root, n)


def _string_literals(path):
    """Every STRING token's value. tokenize is used rather than a regex so a
    key named inside a comment or a docstring does not register as a hardcoded
    literal — the revenue-filter sweep next door uses the same tool for the
    same reason. (This is not pedantry: it is what proved express_dbf_source.py
    only MENTIONS payments_out in prose and does not dispatch on it.)"""
    out = set()
    with open(path, 'rb') as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.STRING:
                    body = tok.string.strip()
                    for q in ("'''", '"""', "'", '"'):
                        if body.startswith(q) and body.endswith(q) and len(body) > len(q):
                            out.add(body[len(q):-len(q)])
                            break
        except (tokenize.TokenError, SyntaxError, UnicodeDecodeError):
            pass
    return out


def _rel(path):
    return os.path.relpath(path, APP)


def test_registry_module_declares_every_report_type():
    """The registry names all nine keys — including the three the file sweep
    deliberately skips, which is what keeps skipping them safe."""
    import report_types

    declared = {rt.key for rt in report_types.REPORT_TYPES}
    assert declared == set(KEYS), (
        f'registry drift: missing {sorted(set(KEYS) - declared)}, '
        f'unexpected {sorted(declared - set(KEYS))}')


def test_sweep_can_actually_see_a_key():
    """CONTROL. If the scanner stops finding literals at all, this goes red
    first, so a silently-broken scan can never read as a clean sweep.

    It survives its own success on purpose: the registry keeps declaring all
    nine keys as literals no matter how the rest of the refactor lands.
    """
    registry = os.path.join(APP, REGISTRY)
    assert os.path.exists(registry), f'{REGISTRY} does not exist'
    found = _string_literals(registry) & KEYS
    assert found == set(KEYS), (
        f'scanner read only {sorted(found)} out of {sorted(KEYS)} from the '
        'registry — the tokenizer, not the codebase, is what is broken')


def test_no_undeclared_report_type_literals():
    """No file outside the registry hardcodes a discriminating report-type key
    unless the allowlist pins that exact key with a reason."""
    scanned = 0
    offenders = {}
    for path in _py_files():
        rel = _rel(path)
        if rel == REGISTRY:
            continue
        scanned += 1
        found = _string_literals(path) & SWEPT_KEYS
        if not found:
            continue
        permitted = ALLOWED.get(rel, (frozenset(), ''))[0]
        undeclared = found - permitted
        if undeclared:
            offenders[rel] = sorted(undeclared)

    # Count first, property second: an empty scan would make the assertion
    # below vacuously true and read as coverage.
    assert scanned > 50, f'sweep only reached {scanned} files — it did not run'

    assert not offenders, (
        'these files hardcode report-type keys that nothing declares:\n' +
        '\n'.join(f'  {f}: {ks}' for f, ks in sorted(offenders.items())) +
        f'\n\nEither read them from {REGISTRY}, or add the file to ALLOWED '
        'with the exact keys and a written reason.')


def test_allowlist_entries_all_carry_a_reason():
    """An allowlist entry without a reason is an oversight wearing a costume."""
    for rel, (keys, reason) in ALLOWED.items():
        assert keys, f'{rel}: allowlisted with no keys — remove the entry instead'
        assert keys <= SWEPT_KEYS, (
            f'{rel}: allowlists {sorted(keys - SWEPT_KEYS)}, which the sweep '
            'does not look at — the entry does nothing')
        assert len(reason) > 40, f'{rel}: reason too thin to be a decision'


def test_allowlist_has_no_stale_entries():
    """A file that no longer hardcodes a key must leave the allowlist, or the
    list slowly becomes a place where real drift can hide."""
    stale = {}
    for rel, (keys, _reason) in ALLOWED.items():
        path = os.path.join(APP, rel)
        if not os.path.exists(path):
            stale[rel] = 'file is gone'
            continue
        unused = keys - _string_literals(path)
        if unused:
            stale[rel] = f'no longer hardcodes {sorted(unused)}'
    assert not stale, ('stale allowlist entries:\n' +
                       '\n'.join(f'  {f}: {w}' for f, w in sorted(stale.items())))


def test_summary_message_field_choices_match_the_registry():
    """bsn._express_dbf_summary_message reads per_type[key][field] by hand.

    It is allowlisted above because the Thai wording cannot be generated from
    the registry, but the FIELD choice is exactly what drifted in
    vat_book_builder: every type reports 'imported' except credit_notes_ar,
    which reports 'upserted'. This pins the hand-written choices so the two
    cannot part without a test going red.
    """
    import inspect
    import re

    import report_types
    from blueprints import bsn

    src = inspect.getsource(bsn._express_dbf_summary_message)
    want = report_types.dbf_count_fields()

    # One key and one count-field on the same line is the shape both variants
    # use: per_type['k']['f'] and (per_type.get('k') or {}).get('f').
    found = {}
    for line in src.splitlines():
        if 'per_type' not in line:
            continue
        keys = [k for k in re.findall(r"'([a-z_]+)'", line) if k in want]
        fields = [f for f in re.findall(r"'(imported|upserted)'", line)]
        if len(keys) == 1 and len(fields) == 1:
            found[keys[0]] = fields[0]

    # CONTROL: if the parse stops matching, this says so rather than letting an
    # empty result pass as agreement.
    assert len(found) >= 6, (
        f'only parsed {sorted(found)} out of the summary message — the parse, '
        'not the code, is what changed; fix this test before trusting it')

    wrong = {k: (f, want[k]) for k, f in found.items() if f != want[k]}
    assert not wrong, (
        'the summary message reads a different count field than the registry '
        'declares:\n' +
        '\n'.join(f'  {k}: reads {got!r}, registry says {exp!r}'
                   for k, (got, exp) in sorted(wrong.items())))
