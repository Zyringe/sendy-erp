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
the exact shape file-level identity missed twice — EXPECTED_SNIPPETS records
the exact MULTISET of synced_to_stock-bearing literal text the scan currently
finds per key, not just a count. A bare count is not enough: swapping one
occurrence for a different, unguarded one inside the same function (delete
one literal, add a new one, net count unchanged) would leave a count-only
check green — this is exactly the class of evasion a fix-round review found
against the first version of this file (task-9-report.md, "Fix round 1").
Comparing the sorted literal text itself closes that hole, and as a bonus
tells the next person WHICH literal changed rather than just "something did".
This is DELIBERATELY brittle: reflowing a SQL string with no behaviour change
will also turn this red. That is the correct trade here — any edit to a
synced_to_stock literal is exactly what a human should be made to look at.
Both dicts are compared against a fresh scan, in both directions, every run.

Bucket (i) "guarded" entries get one more check: the function's actual source
must still contain a call to the shared predicate (is_non_stock_code( or
non_stock_clause() — a "guarded" claim that stops being true (guard deleted,
renamed) turns red even when the synced_to_stock literal itself is untouched.
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

# The exact MULTISET (sorted, duplicates preserved — a plain sorted list
# already compares as a multiset) of synced_to_stock-bearing literal TEXT the
# scan currently finds inside each EXPECTED key's function — not just a
# count. A count lets one occurrence be swapped for a different, unguarded
# one in the same edit (delete + add, net count unchanged) slide through;
# comparing the literal text itself cannot be fooled that way. lineno is
# deliberately excluded (line numbers shift for innocuous reasons; the
# literal text is the thing). Truncated to 80 chars by _scan(), matching
# what it stores — generated by running _scan() itself and hand-copying the
# output (never regenerate this from a live scan at test time — see the
# module docstring: EXPECTED* must stay hardcoded).
EXPECTED_SNIPPETS = {
    ('database.py', 'init_db'): [
        'ADD COLUMN synced_to_stock INTEGER NOT NULL DEFAULT 0',
        'synced_to_stock',
    ],
    ('models/bsn_sync.py', '_sync_bsn_to_stock'): [
        'SET synced_to_stock=1 WHERE id=?',
        'SET synced_to_stock=1 WHERE id=?',
        'WHERE product_id IS NOT NULL AND synced_to_stock = 0',
        'สร้าง transaction ย้อนหลังสำหรับแถว BSN ที่มี product_id แล้ว     แต่ยังไม่ถูก s',
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
        'GROUP BY bsn_code, unit, product_id         UNION ALL         SELECT bsn_code, u',
        'SELECT bsn_code, unit, product_id,                COUNT(*) AS row_count,        ',
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


def test_occurrence_snippets_match_expected_per_function():
    """A NEW (or removed) synced_to_stock statement inside an ALREADY-covered
    function must not slide through just because the (path, function) key is
    already in EXPECTED — this is the identity-granularity fix (rule 1).

    Compares the sorted MULTISET of literal text, not a bare count. A count
    alone lets one occurrence be deleted and a different, unguarded one added
    in the same edit — net count unchanged, count-only check stays green.
    Comparing the text itself cannot be fooled that way, and a mismatch names
    exactly which literal(s) differ (task-9-report.md, "Fix round 1")."""
    found = _scan()
    mismatches = []
    for key, expected_snippets in EXPECTED_SNIPPETS.items():
        actual_snippets = sorted(snip for _, snip in found.get(key, []))
        expected_sorted = sorted(expected_snippets)
        if actual_snippets != expected_sorted:
            mismatches.append(
                f'  {key[0]}::{key[1]}\n'
                f'    expected: {expected_sorted}\n'
                f'    found:    {actual_snippets}')
    assert not mismatches, (
        "Occurrence snippet(s) drifted inside an already-classified function "
        "— a statement was added, removed, or changed. Re-read the function "
        "and update both EXPECTED's reason (if the change affects the guard) "
        "and EXPECTED_SNIPPETS:\n" + "\n".join(mismatches))


def test_expected_dicts_share_the_same_keys():
    """Internal consistency: every EXPECTED entry needs a snippet list, and
    vice versa — a maintenance slip here would silently blind one of the two
    checks above."""
    assert set(EXPECTED) == set(EXPECTED_SNIPPETS), (
        set(EXPECTED) ^ set(EXPECTED_SNIPPETS))


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
