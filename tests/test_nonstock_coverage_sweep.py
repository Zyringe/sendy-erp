"""Every occurrence of synced_to_stock must be classified.

Occurrence identity is (path, function, SQL operation) — NEVER the file. Finding
the guard token somewhere in bsn_sync.py is not evidence that every query in
bsn_sync.py is safe; that is how the /unit-conversions seam was missed twice.

⛔ EXPECTED below is HARDCODED. Never regenerate it from the scan — auto-
generation admits a new consumer silently, which is the exact failure this
test exists to prevent. A new occurrence turns this red until a human
classifies it.

An allowlist entry is a claim that a BEHAVIOUR test backs it up; the named
test must exist (checked below — a citation to a file/function that doesn't
exist fails the sweep too).

IDENTITY GRANULARITY: EXPECTED is keyed (relative path, enclosing function),
matching design §7(a)'s own worked example. To still catch a NEW (or removed)
synced_to_stock statement appearing INSIDE an already-classified function —
the exact shape file-level identity missed twice — EXPECTED_SEQUENCE records
the exact source-ORDERED sequence of synced_to_stock-bearing literal text the
scan currently finds per key (sorted by lineno, ties broken by scan-visit
order), not a bare count and not a sorted multiset. A bare count is not
enough: swapping one occurrence for a different, unguarded one inside the
same function (delete one literal, add a new one, net count unchanged) left
a count-only check green (task-9-report.md, "Fix round 1"). A sorted-multiset
fix closed THAT hole but is still blind to occurrence IDENTITY: when a
function contains a genuine text DUPLICATE (bsn_sync.py::_sync_bsn_to_stock
has two identical 'SET synced_to_stock=1 WHERE id=?' literals), removing one
copy and inserting a new, unguarded write using the SAME exact text leaves
the sorted multiset byte-identical (task-9-report.md, "Fix round 2"). Source
order NARROWS this hole but does NOT close it in general: it only catches an
insertion that lands in a DIFFERENT relative sorted-by-line slot than the
occurrence it replaced. A same-edit swap of a text-IDENTICAL occurrence can
still land in the SAME slot and stay invisible — see "WHAT THIS SWEEP CANNOT
CATCH" below for the live, reachable-today instance. (An earlier version of
this comment overclaimed that any insertion "before the guard" necessarily
shifts the sequence; that is false whenever an earlier, unrelated literal —
e.g. this function's own SELECT — already sits between the guard and the
duplicate being replaced. Corrected in "Fix round 3".) Both dicts are
compared against a fresh scan, in both directions, every run. This is
DELIBERATELY brittle where it DOES bite: reflowing a SQL string, or
reordering two DIFFERENT (non-duplicate) statements with no behaviour
change, will also turn this red. That is the correct trade here — any edit
to a synced_to_stock literal, or to where one sits relative to its
DIFFERENT-text neighbours, is exactly what a human should be made to look
at.

Bucket (i) "guarded" entries get one more check: the function's actual source
must still contain a call to the shared predicate (is_non_stock_code( or
non_stock_clause() — a "guarded" claim that stops being true (guard deleted,
renamed) turns red even when the synced_to_stock literal itself is untouched.

WHAT THIS SWEEP CANNOT CATCH (documented limitation, not a TODO — team-lead
decision, task-9-report.md "Fix round 2": this sweep's threat model is
ACCIDENT, not adversary; a static text/order net that a sufficiently precise
same-edit swap can defeat is an acceptable, DOCUMENTED limit, not worth
trading legibility for AST-path fingerprinting or per-occurrence hashing):
  - A same-edit SWAP of a text-IDENTICAL occurrence, when the replacement
    lands in the SAME relative sorted-by-line position the deleted one
    vacated. Ordered comparison only catches a position CHANGE; if a
    function has two byte-identical synced_to_stock literals, deleting one
    and inserting a new, unguarded occurrence using the exact same text
    compares equal whenever the insertion point still sorts into that same
    slot. This is reachable TODAY, no contrived text collision required:
    _sync_bsn_to_stock already carries two identical
    'SET synced_to_stock=1 WHERE id=?' literals (bsn_sync.py:203 and :334).
    Deleting the line-203 one and inserting an unguarded copy of the same
    text anywhere between the line-170 SELECT and the surviving line-334
    UPDATE — e.g. immediately inside `for row in rows:`, before the
    is_non_stock_code guard — leaves BOTH the multiset and the ordered
    sequence unchanged, because the guard sits AFTER that SELECT, not
    before it: "before the guard" is a weaker constraint than "before every
    earlier literal", so it does not force a different sorted slot. Proved
    by construction (not by a single test run) in task-9-report.md "Fix
    round 3" — an earlier round of this file claimed order-based comparison
    closes any before-the-guard insertion; that claim was wrong and is
    corrected there.
  - It has NO control-flow awareness. It tracks the ORDER of
    synced_to_stock-bearing literals among THEMSELVES, not each literal's
    position relative to the guard call that is supposed to protect it. The
    guard call itself (is_non_stock_code(...)/non_stock_clause(...)) never
    contains the substring 'synced_to_stock', so it is invisible to this
    scanner entirely. Relocating an EXISTING guarded write to before its
    function's guard check (or moving the guard check to after a write it
    used to protect) changes NOTHING this sweep looks at — same literal
    text, same relative order among literals, guard token still present
    somewhere in the function's source (the positive-control check in
    test_every_guarded_entry_still_calls_the_shared_predicate only checks
    the token EXISTS in the function, not where). Verified genuinely green
    on a scratch copy, reported honestly rather than silently left unproven
    (task-9-report.md "Fix round 2", variant 2).
  - It is per-(path, function) and per-literal; it says nothing about
    whether a NEW function that itself calls an EXISTING guarded function
    is safe by construction, only about literals matching what a human
    already read and classified.
"""
import ast
import os
import re

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

MODULE_LEVEL = '<module level>'

# ── EXPECTED ─────────────────────────────────────────────────────────────────
# (relative path, enclosing function): reason. One entry per (path, function)
# group the scan currently finds. Typed by hand from reading every occurrence
# — see task-9-report.md for the full classification writeup.
EXPECTED = {
    ('database.py', 'init_db'):
        'allowlisted: schema DDL only (ALTER TABLE ADD COLUMN synced_to_stock '
        '... DEFAULT 0) — defines the column, never branches or filters on its '
        'value, so it is not a consumer of the non-stock semantics at all '
        '(tests/test_fresh_db_build.py::test_init_db_from_empty_completes)',

    ('models/bsn_sync.py', '_sync_bsn_to_stock'):
        'guarded: is_non_stock_code check at the top of the row loop skips a '
        'non-stock line before any INSERT/UPDATE '
        '(tests/test_nonstock_line_sync.py::test_non_stock_line_creates_no_ledger_row, '
        'tests/test_nonstock_line_sync.py::test_non_stock_row_survives_pass_2_rebuild)',

    ('models/bsn_sync.py', 'dismiss_pending_unit_conversion'):
        'guarded: function-level rejection (non_stock_clause-narrowed protected-row '
        'check) refuses the WHOLE group before any DELETE, and the DELETE itself is '
        'separately narrowed by non_stock_clause '
        '(tests/test_nonstock_dismiss_guard.py::test_dismiss_refuses_to_delete_non_stock_rows, '
        'tests/test_nonstock_dismiss_guard.py::test_dismiss_refuses_a_MIXED_group_all_or_nothing)',

    ('models/bsn_sync.py', 'get_pending_unit_conversions'):
        'guarded: both UNION branches filtered by non_stock_clause() '
        '(tests/test_nonstock_dismiss_guard.py::test_non_stock_rows_absent_from_pending_unit_conversions)',

    ('models/ecommerce_overview.py', '_sold_since_by_pid'):
        'guarded: query filtered by non_stock_clause(\'st\') so a discount/shipping '
        'line never reads as marketplace units sold '
        '(tests/test_nonstock_readers.py::test_marketplace_sold_ignores_a_discount_line)',

    ('models/imports.py', 'import_weekly'):
        'guarded: is_non_stock_code(product_code_raw) drives the revenue-preserving '
        'import branch, keeping the row even when the mapping still says is_ignored=1 '
        '(tests/test_nonstock_line_sync.py::test_non_stock_line_is_imported_as_revenue, '
        'tests/test_nonstock_line_sync.py::test_non_stock_row_survives_pass_2_rebuild)',

    ('models/mapping.py', 'get_pending_split_mappings'):
        'guarded: both UNION branches filtered by non_stock_clause() '
        '(tests/test_nonstock_readers.py::test_split_mappings_excludes_non_stock)',

    ('models/reconcile.py', MODULE_LEVEL):
        'allowlisted: _CAS_FIELDS is a column list feeding the CAS snapshot SELECT — '
        'carries the value through, does not filter/gate. If dropped, _ledger_check\'s '
        "r.get('synced_to_stock') would silently read None for every row "
        '(tests/test_nonstock_readers.py::test_reconcile_accepts_a_doc_containing_a_non_stock_line)',

    ('models/reconcile.py', '_ledger_check'):
        'allowlisted: CAS+ledger-verified against Express, independent of '
        'synced_to_stock semantics (two independent reviewers confirmed per the '
        'plan). Treats "unsynced AND no matching ledger row" as a valid state for '
        'ANY row, not special-cased for non-stock — which is exactly the '
        'permanent state _sync_bsn_to_stock leaves a non-stock row in '
        '(tests/test_nonstock_readers.py::test_reconcile_accepts_a_doc_containing_a_non_stock_line, '
        'tests/test_reconcile_scan.py::test_unsynced_line_has_no_ledger_row_apply_does_not_invent_one, '
        'tests/test_reconcile_scan.py::test_unsynced_line_with_a_stray_ledger_row_refuses)',

    ('models/reconcile.py', '_cas_compare'):
        'allowlisted: generic staleness/CAS-drift check — synced_to_stock is one '
        'of several tracked fields (id, product_id, customer, ref_invoice) '
        'compared identically, no non-stock-specific branching. CAVEAT: the cited '
        'test exercises the same loop via ref_invoice drift, not synced_to_stock '
        'itself — a generic-mechanism pin, not a field-specific one '
        '(tests/test_reconcile_scan.py::test_post_scan_drift_refuses_apply)',

    ('models/stock_filters.py', MODULE_LEVEL):
        "allowlisted: module DOCSTRING prose only (\"every reader of "
        "synced_to_stock has to agree\") — not executable code, never reads or "
        "writes the column. NOT EXECUTABLE — no behaviour test applies.",

    ('models/bsn_sync.py', 'update_unit_conversion_ratio'):
        'allowlisted: resets synced_to_stock=0 for the WHOLE product then '
        'replays through the real _sync_bsn_to_stock (same chokepoint guard as '
        'above) — no non-stock-aware code of its own. Traced by two independent '
        'reviewers per the design, and now DEMONSTRATED (not just traced): '
        'break-it-once against is_non_stock_code inside _sync_bsn_to_stock turns '
        'this red '
        '(tests/test_unit_ratio_rebuild.py::test_ratio_edit_survives_a_non_stock_row_on_the_same_product)',

    ('models/mapping.py', 'repoint_bsn_code'):
        'allowlisted: resets synced_to_stock=0 for every AFFECTED product (not '
        "just the moved bsn_code's — see the function's own docstring), deletes "
        'their ledger, replays through the real _sync_bsn_to_stock (same '
        'chokepoint guard). Demonstrated via break-it-once, same as above '
        '(tests/test_repoint_bsn_code.py::test_repoint_survives_a_non_stock_row_sharing_the_source_product)',

    ('models/mapping.py', '_repoint_rows'):
        'allowlisted: nested helper inside repoint_bsn_code doing the actual '
        'UPDATE ... synced_to_stock=0 — covered by the same replay and the same '
        'test as its enclosing function '
        '(tests/test_repoint_bsn_code.py::test_repoint_survives_a_non_stock_row_sharing_the_source_product)',

    ('models/bsn_sync.py', '_synced_source_ids'):
        'allowlisted: read-only set builder, only caller is '
        'update_unit_conversion_ratio; selects WHERE synced_to_stock=1 — a '
        'non-stock row is PERMANENTLY 0, so it can never appear in this set '
        'regardless of the replay outcome '
        '(tests/test_unit_ratio_rebuild.py::test_ratio_edit_survives_a_non_stock_row_on_the_same_product '
        "exercises update_unit_conversion_ratio's full path, which calls this "
        'function twice)',
}

# The exact source-ORDERED SEQUENCE (sorted by lineno; ties broken by scan-
# visit order — occurs only within one multi-interpolation f-string, e.g.
# get_pending_split_mappings' two {non_stock} slots, where Python 3.9's
# JoinedStr AST gives every constant segment the SAME outer lineno) of
# synced_to_stock-bearing literal TEXT inside each EXPECTED key's function.
# Order matters, NOT just membership: a genuine text duplicate (the two
# identical 'SET synced_to_stock=1 WHERE id=?' literals in
# _sync_bsn_to_stock) means a sorted-multiset comparison cannot tell "delete
# copy A, insert an unguarded copy of the same text elsewhere" from a no-op
# — see the module docstring and task-9-report.md "Fix round 2". Comparing
# the SEQUENCE catches that WHEN the insertion lands in a different relative
# sorted-by-line slot than the deletion vacated — but NOT when it lands in
# the SAME slot (e.g. an insertion still sorts between the same two
# neighbouring literals the deleted duplicate sat between). That residual
# gap is real, reachable today via this exact function, and documented (not
# silently left) in the module docstring's "WHAT THIS SWEEP CANNOT CATCH"
# and in task-9-report.md "Fix round 3".
# Truncated to 80 chars by _scan(), matching what it stores — generated by
# running _scan() itself and hand-copying the output, never regenerated at
# test time (see the module docstring: EXPECTED* must stay hardcoded).
EXPECTED_SEQUENCE = {
    ('database.py', 'init_db'): [
        'synced_to_stock',
        'ADD COLUMN synced_to_stock INTEGER NOT NULL DEFAULT 0',
    ],
    ('models/bsn_sync.py', '_sync_bsn_to_stock'): [
        'สร้าง transaction ย้อนหลังสำหรับแถว BSN ที่มี product_id แล้ว     แต่ยังไม่ถูก s',
        'WHERE product_id IS NOT NULL AND synced_to_stock = 0',
        'SET synced_to_stock=1 WHERE id=?',
        'SET synced_to_stock=1 WHERE id=?',
    ],
    ('models/bsn_sync.py', '_synced_source_ids'): [
        'The exact `(table, row id)` pairs for `product_id` that currently hold a     led',
        'WHERE product_id=? AND synced_to_stock=1',
    ],
    ('models/bsn_sync.py', 'dismiss_pending_unit_conversion'): [
        'Delete all synced_to_stock=0 rows for (product_id, bsn_unit) from both     ledge',
        'WHERE product_id=? AND unit=? AND synced_to_stock=0   AND NOT (',
        'WHERE product_id=? AND unit=? AND synced_to_stock=0 AND',
    ],
    ('models/bsn_sync.py', 'get_pending_unit_conversions'): [
        'SELECT t.product_id, t.bsn_unit, p.product_name, p.unit_type,                t.r',
    ],
    ('models/bsn_sync.py', 'update_unit_conversion_ratio'): [
        'SET synced_to_stock=0 WHERE product_id=?',
    ],
    ('models/ecommerce_overview.py', '_sold_since_by_pid'): [
        'AND NOT (st.synced_to_stock = 1 AND st.customer IN (',
    ],
    ('models/imports.py', 'import_weekly'): [
        'SET synced_to_stock=0 WHERE product_id IN (',
    ],
    ('models/mapping.py', '_repoint_rows'): [
        'SET product_id=?, synced_to_stock=0 WHERE id=?',
    ],
    ('models/mapping.py', 'get_pending_split_mappings'): [
        'SELECT bsn_code, unit, product_id,                COUNT(*) AS row_count,        ',
        'GROUP BY bsn_code, unit, product_id         UNION ALL         SELECT bsn_code, u',
    ],
    ('models/mapping.py', 'repoint_bsn_code'): [
        "Canonical single bsn_code re-point — moves the mapping AND the code's     FULL h",
        'SET synced_to_stock=0 WHERE product_id IN (',
    ],
    ('models/reconcile.py', MODULE_LEVEL): [
        'synced_to_stock',
    ],
    ('models/reconcile.py', '_cas_compare'): [
        'synced_to_stock',
    ],
    ('models/reconcile.py', '_ledger_check'): [
        'synced_to_stock',
    ],
    ('models/stock_filters.py', MODULE_LEVEL): [
        'Canonical answer to "does this BSN line move stock?".  WHY A CONSTANT AND NOT A ',
    ],
}

# Sentinel for the rare allowlist entry where NO behaviour test can apply
# (pure comment/docstring prose, never executed) — rule 5 says to admit this
# honestly rather than invent a citation. Checked for exactly, so a lazy
# "no test" excuse elsewhere can't hide behind it.
_NOT_EXECUTABLE = 'NOT EXECUTABLE — no behaviour test applies.'

# Bucket (i): functions expected to call the shared predicate INLINE. Checked
# against the function's actual source segment below — a positive control so
# a "guarded" claim silently going stale (guard deleted/renamed) also turns
# this red, not just the literal disappearing.
GUARDED_KEYS = {
    ('models/bsn_sync.py', '_sync_bsn_to_stock'),
    ('models/bsn_sync.py', 'dismiss_pending_unit_conversion'),
    ('models/bsn_sync.py', 'get_pending_unit_conversions'),
    ('models/ecommerce_overview.py', '_sold_since_by_pid'),
    ('models/imports.py', 'import_weekly'),
    ('models/mapping.py', 'get_pending_split_mappings'),
}
GUARD_TOKENS = ('is_non_stock_code(', 'non_stock_clause(')

_TEST_CITATION = re.compile(r'tests/(test_[\w]+)\.py::(test_[\w]+)')


# ── Scanner ──────────────────────────────────────────────────────────────────

def _iter_py_files():
    for root, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'instance', 'static', '.git')]
        for name in files:
            if name.endswith('.py'):
                path = os.path.join(root, name)
                yield os.path.relpath(path, APP).replace(os.sep, '/'), path


def _enclosing_function(tree, lineno):
    """Innermost FunctionDef/AsyncFunctionDef whose span contains `lineno`,
    or None (module level). Nested functions (e.g. mapping.py's _repoint_rows,
    defined inside repoint_bsn_code) resolve to the INNER def, matching the
    design's "enclosing function" identity."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, 'end_lineno', None)
            if end is None or not (node.lineno <= lineno <= end):
                continue
            if best is None or (node.lineno >= best.lineno and end <= best.end_lineno):
                best = node
    return best


def _scan():
    """{(rel_path, func_name): [(lineno, snippet), ...]} for every string
    literal (incl. f-string constant segments — ast.walk descends into
    JoinedStr.values) containing 'synced_to_stock', anywhere under
    inventory_app/."""
    occurrences = {}
    for rel, path in _iter_py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        if 'synced_to_stock' not in src:
            continue
        tree = ast.parse(src, filename=path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if 'synced_to_stock' not in node.value:
                continue
            func = _enclosing_function(tree, node.lineno)
            key = (rel, func.name if func else MODULE_LEVEL)
            snippet = node.value.strip().replace('\n', ' ')[:80]
            occurrences.setdefault(key, []).append((node.lineno, snippet))
    return occurrences


def _function_source(rel_path, func_name):
    """Source segment for `func_name` in `rel_path` (or the whole file's
    source for MODULE_LEVEL) — used for the guard-token positive control."""
    path = os.path.join(APP, rel_path)
    with open(path, encoding='utf-8') as f:
        src = f.read()
    if func_name == MODULE_LEVEL:
        return src
    tree = ast.parse(src, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(src, node) or ''
    return ''


# ── Tests ────────────────────────────────────────────────────────────────────

def test_every_occurrence_is_classified_or_allowlisted():
    """Both directions: a new (path, function) key not in EXPECTED, or an
    EXPECTED key the scan no longer finds, both fail loud."""
    found = _scan()
    found_keys = set(found)
    expected_keys = set(EXPECTED)

    new = found_keys - expected_keys
    gone = expected_keys - found_keys

    def _describe(key):
        hits = found.get(key, [])
        lines = '; '.join(f'L{ln}: {snip!r}' for ln, snip in hits)
        return f'  {key[0]}::{key[1]} — {lines or "(no longer found)"}'

    assert not new, (
        "New synced_to_stock occurrence(s) with no EXPECTED entry — classify "
        "each as guarded (cite the inline predicate + its behaviour test) or "
        "allowlisted (cite the reason + its behaviour test):\n" +
        "\n".join(_describe(k) for k in sorted(new)))
    assert not gone, (
        "EXPECTED entry(ies) whose occurrence has disappeared — either the "
        "consumer was deleted/moved (update task-9-report.md) or something "
        "else is wrong; remove the stale entry only once you've checked which:\n"
        + "\n".join(_describe(k) for k in sorted(gone)))


def test_occurrence_sequence_matches_expected_per_function():
    """A NEW (or removed) synced_to_stock statement inside an ALREADY-covered
    function must not slide through just because the (path, function) key is
    already in EXPECTED — this is the identity-granularity fix (rule 1).

    Compares the source-ORDERED SEQUENCE of literal text, not a sorted
    multiset. A sorted multiset is blind to a genuine text duplicate: delete
    one copy, insert a new unguarded copy of the SAME text elsewhere — the
    multiset is unchanged (task-9-report.md, "Fix round 2"). Comparing order
    catches that WHEN the insertion lands in a different relative sorted-by-
    line slot than the deletion vacated. It does NOT catch a same-slot swap
    (e.g. an insertion that still sorts between the same two neighbouring,
    DIFFERENT-text literals the deleted duplicate sat between) — a real,
    documented residual gap, not silently assumed closed (module docstring's
    "WHAT THIS SWEEP CANNOT CATCH"; task-9-report.md "Fix round 3"). A
    mismatch, when this test DOES catch one, still names exactly which
    position/literal differs."""
    found = _scan()
    mismatches = []
    for key, expected_seq in EXPECTED_SEQUENCE.items():
        actual_seq = [snip for _, snip in sorted(found.get(key, []), key=lambda t: t[0])]
        if actual_seq != expected_seq:
            mismatches.append(
                f'  {key[0]}::{key[1]}\n'
                f'    expected: {expected_seq}\n'
                f'    found:    {actual_seq}')
    assert not mismatches, (
        "Occurrence sequence drifted inside an already-classified function — "
        "a statement was added, removed, changed, or REORDERED relative to "
        "its neighbours. Re-read the function and update both EXPECTED's "
        "reason (if the change affects the guard) and EXPECTED_SEQUENCE:\n"
        + "\n".join(mismatches))


def test_expected_dicts_share_the_same_keys():
    """Internal consistency: every EXPECTED entry needs a sequence, and vice
    versa — a maintenance slip here would silently blind one of the two
    checks above."""
    assert set(EXPECTED) == set(EXPECTED_SEQUENCE), (
        set(EXPECTED) ^ set(EXPECTED_SEQUENCE))


def test_every_allowlist_entry_names_a_real_test():
    """An allowlist/guard entry is a claim that a behaviour test backs it up.
    Extract every 'tests/test_X.py::test_Y' citation from the reason string
    and verify that file+function actually exist — a citation to a deleted or
    renamed test is as bad as no citation."""
    problems = []
    for key, reason in EXPECTED.items():
        citations = _TEST_CITATION.findall(reason)
        if not citations:
            if _NOT_EXECUTABLE in reason:
                continue  # honest "no test can apply" — see docstring
            problems.append(f'{key}: reason names no tests/test_*.py::test_* citation')
            continue
        for test_file, test_func in citations:
            path = os.path.join(os.path.dirname(__file__), f'{test_file}.py')
            if not os.path.exists(path):
                problems.append(f'{key}: cites {test_file}.py which does not exist')
                continue
            with open(path, encoding='utf-8') as f:
                src = f.read()
            if f'def {test_func}(' not in src:
                problems.append(f'{key}: cites {test_file}.py::{test_func} — no such def')
    assert not problems, "\n".join(problems)


def test_every_guarded_entry_still_calls_the_shared_predicate():
    """Positive control for bucket (i): the function's actual source must
    still contain is_non_stock_code( or non_stock_clause( — a 'guarded' claim
    silently going stale (guard deleted/renamed elsewhere) turns this red
    even when the synced_to_stock literal itself never moved."""
    missing = []
    for key in GUARDED_KEYS:
        assert key in EXPECTED, f'{key} listed in GUARDED_KEYS but not in EXPECTED'
        src = _function_source(*key)
        if not any(tok in src for tok in GUARD_TOKENS):
            missing.append(f'{key[0]}::{key[1]} — lost its is_non_stock_code/non_stock_clause call')
    assert not missing, "\n".join(missing)


def test_guarded_keys_is_a_subset_of_expected():
    assert GUARDED_KEYS <= set(EXPECTED), GUARDED_KEYS - set(EXPECTED)
