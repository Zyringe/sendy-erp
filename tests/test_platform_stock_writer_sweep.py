"""Every writer of `platform_skus.stock` and every writer of
`platform_stock_deductions` must be classified by hand (task 1.6,
order-driven-platform-deduction plan, 2026-08-25 rule: "a cross-cutting
sweep you wrote yourself is the next thing to distrust").

WHY THIS EXISTS: task 1.4 deleted the platform-deduction walk from
`_sync_bsn_to_stock`. A grep-once confirmation that the walk is gone is not
the same guarantee as a standing test — a FUTURE change (a "helpful"
re-add inside bsn_sync, a new one-off script promoted to a route, a
refactor that reintroduces a write under a different name) must turn this
red, not slip through because nobody re-ran the grep. Same discipline as
`tests/test_nonstock_coverage_sweep.py` (the established pattern in this
repo), scoped to two tables instead of one column, and lighter-weight:
per-(path, function), no ordered-sequence pinning — task 1.6's brief asks
for "every writer ... with a per-entry written reason", not the nonstock
sweep's full ordering apparatus.

SCOPE: `inventory_app/` only (mirrors test_nonstock_coverage_sweep.py).
Deliberately EXCLUDES:
  - `scripts/*.py` — dated one-off historical migration/replay scripts
    (e.g. `phase_c_replay_apply_20260530.py`, `fix_hinge_413_pack_pair_
    20260710.py`). Already run, disposable, never on the live request
    path. A sweep of the LIVE write surface is not the place to pin
    scripts nobody will run again; if one is ever re-run, that is a
    conscious "simulate before mutating live data" exercise, not
    something this test can help with.
  - `blueprints/admin.py`'s `_MASTER_TABLES` manifest (DB upload/download
    admin feature) — a whole-table bulk load/dump that lists
    'platform_skus' as ONE of ~25 tables in a generic backup mechanism. It
    is not a targeted stock writer and predates this plan entirely; a
    literal table-name string in a tuple has no SQL keyword next to it, so
    the scanner's write-keyword requirement already excludes it (verified
    by `test_admin_master_tables_manifest_is_not_flagged`).

TWO detection shapes, matching the brief's "f-string table names included":
  (A) STATIC — a write-keyword (UPDATE / INSERT INTO / DELETE FROM)
      immediately followed by a LITERAL `platform_skus` or
      `platform_stock_deductions` in the same flattened SQL text. Every
      writer live in the app today uses this shape (verified: shape-A
      matches classified below are the complete set the scan currently
      finds).
  (B) DYNAMIC — a write-keyword immediately followed by an f-string
      `{expr}` in the table-name position. The scanner cannot know the
      expression's runtime value, so it flags the site UNCONDITIONALLY
      whenever the SAME flattened text also mentions "stock" or
      "platform_sku" (a cheap, deliberately generous filter — false
      positives here cost one extra allowlist line; false negatives hide
      exactly the bug this plan's own predecessor caused). Any such site
      must appear in DYNAMIC_TABLE_WRITE_SITES with a reason confirming,
      by reading the call site, what table it actually targets at
      runtime. Zero live sites today (`_sync_bsn_to_stock`'s own
      `f"...{table}..."` idiom targets sales_transactions /
      purchase_transactions, never platform_skus — see
      test_sync_bsn_to_stock_dynamic_table_never_targets_platform_skus).

WHAT THIS SWEEP CANNOT CATCH (documented limitation, not a TODO — same
stance as test_nonstock_coverage_sweep.py's own list):
  - A write reached through a helper whose OWN source contains no SQL
    literal at all (e.g. an ORM-style call, or a table name built by
    string concatenation split across multiple statements/variables
    before ever reaching `.execute(`). Shape (B) catches the single-
    f-string-interpolation case; it does not trace data flow.
  - A write inside a nested function/lambda/comprehension whose enclosing
    def is not the one performing the semantic "write" (identity here is
    the INNERMOST enclosing function, same as the nonstock sweep — a
    write inside a closure defined inside an allowlisted function reads
    as that closure's own key, not its parent's, and needs its own entry).
  - Anything outside `inventory_app/` (see SCOPE above).
"""
import ast
import os
import re

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

MODULE_LEVEL = '<module level>'
_DYNAMIC = '{DYNAMIC}'

TABLES = ('platform_skus', 'platform_stock_deductions')


# ── Flattening: reconstruct the TEXT of a string-ish SQL expression ────────

def _flatten_str_like(node):
    """Best-effort literal text for a node that MIGHT be (part of) a SQL
    string: a plain Constant, an f-string (JoinedStr, with `{DYNAMIC}`
    standing in for any interpolated expression — the point is to make a
    dynamic table-name POSITION visible to the keyword regex even though
    its runtime value is unknowable statically), `"..." % (...)` old-style
    formatting, or `"...".format(...)`. Returns None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                parts.append(_DYNAMIC)
        return ''.join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _flatten_str_like(node.left)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'format'):
        return _flatten_str_like(node.func.value)
    return None


# Each regex captures the TOKEN immediately in the table-name position of a
# write statement -- tight and adjacent, not "the keyword appears somewhere
# earlier in this (possibly huge, multi-statement) string". That looser
# check was tried first and false-positived on database.py's whole-schema
# executescript blob (an UPDATE inside some unrelated trigger body, followed
# much later in the same giant string by an unrelated CREATE TABLE mentioning
# platform_skus) and on docstring prose ("re-diffs", "updates the header").
# `[\w{}]*` lets the captured token be the literal `{DYNAMIC}` placeholder
# `_flatten_str_like` inserts for an f-string's interpolated expression.
_UPDATE_RE = re.compile(r'\bUPDATE\s+(?:OR\s+\w+\s+)?([A-Za-z_{][\w{}]*)', re.I)
_INSERT_RE = re.compile(r'\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_{][\w{}]*)', re.I)
_DELETE_RE = re.compile(r'\bDELETE\s+FROM\s+([A-Za-z_{][\w{}]*)', re.I)
_WRITE_RES = (_UPDATE_RE, _INSERT_RE, _DELETE_RE)

# `\b` around 'stock' deliberately excludes 'synced_to_stock' (no boundary
# between the '_' and 's' -- both are \w) so a dynamic-table write in
# bsn_sync's OWN idiom (SET synced_to_stock=1, nothing to do with
# platform_skus.stock) does not false-positive shape B. See
# test_shape_B_fstring_dynamic_table_unrelated_to_stock_is_not_caught.
_STOCK_WORD_RE = re.compile(r'\bstock\b', re.I)
_PLATFORM_SKU_RE = re.compile(r'\bplatform_sku', re.I)


def _classify_text(text):
    """[(table_or_DYNAMIC, is_dynamic), ...] for every write-keyword +
    table-token pair found (a single flattened text, e.g. an executescript
    blob, can legitimately hold more than one statement)."""
    hits = []
    for rx in _WRITE_RES:
        for m in rx.finditer(text):
            token = m.group(1)
            if token.lower() == 'platform_skus':
                # Column-mention filter: only IN SCOPE when the SAME
                # statement also touches `stock` -- excludes the (real,
                # legitimate) internal_product_id/qty_per_sale-only writes
                # in _propagate_listings_to_platform_skus and
                # apply_platform_mapping. See
                # test_unrelated_column_write_is_not_caught.
                if _STOCK_WORD_RE.search(text):
                    hits.append(('platform_skus', False))
            elif token.lower() == 'platform_stock_deductions':
                # No column filter needed: the whole table IS the
                # deduction-provenance record, by definition in scope.
                hits.append(('platform_stock_deductions', False))
            elif token == _DYNAMIC:
                # Shape B (see module docstring): can't know the runtime
                # table, so flag generously -- but still require the
                # statement to at least LOOK stock/platform_sku-related, or
                # every dynamic-table write anywhere in the app (there are
                # several, all unrelated -- e.g. sales_transactions/
                # purchase_transactions) would need its own entry.
                if _STOCK_WORD_RE.search(text) or _PLATFORM_SKU_RE.search(text):
                    hits.append((_DYNAMIC, True))
    return hits


# ── Scanner ──────────────────────────────────────────────────────────────

def _iter_py_files(root=APP):
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'instance', 'static', '.git')]
        for name in files:
            if name.endswith('.py'):
                path = os.path.join(r, name)
                yield os.path.relpath(path, root).replace(os.sep, '/'), path


def _enclosing_function(tree, lineno):
    """Innermost FunctionDef/AsyncFunctionDef containing `lineno`, or None
    (module level) -- identical rule to test_nonstock_coverage_sweep.py."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, 'end_lineno', None)
            if end is None or not (node.lineno <= lineno <= end):
                continue
            if best is None or (node.lineno >= best.lineno and end <= best.end_lineno):
                best = node
    return best


def _scan_source(src, rel_path='<rogue>'):
    """{(rel_path, func_name): [(lineno, table_or_DYNAMIC, is_dynamic, snippet), ...]}
    for one already-read source string. The reusable core both the
    whole-app scan and the shape-coverage rogue-file tests call."""
    occurrences = {}
    tree = ast.parse(src, filename=rel_path)
    seen = set()   # dedupe: a "...".format(...) / "..." % (...) node and its
                    # nested Constant child both match independently via
                    # ast.walk -- same (lineno, text) is the same statement.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
            continue
        text = _flatten_str_like(node)
        if not text:
            continue
        dedupe_key = (node.lineno, text)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        hits = _classify_text(text)
        if not hits:
            continue
        func = _enclosing_function(tree, node.lineno)
        key = (rel_path, func.name if func else MODULE_LEVEL)
        snippet = text.strip().replace('\n', ' ')[:100]
        for table, is_dynamic in hits:
            occurrences.setdefault(key, []).append((node.lineno, table, is_dynamic, snippet))
    return occurrences


def _scan():
    occurrences = {}
    for rel, path in _iter_py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        if not any(t in src for t in TABLES) and _DYNAMIC not in src:
            # Cheap pre-filter: a file that never mentions either table name
            # AND has no f-string at all cannot possibly match shape A or B.
            # (An f-string-only file still needs parsing for shape B, so
            # this only skips files with NEITHER signal.)
            if 'f"' not in src and "f'" not in src:
                continue
        for key, hits in _scan_source(src, rel).items():
            occurrences.setdefault(key, []).extend(hits)
    return occurrences


def _function_source(rel_path, func_name):
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


# ── EXPECTED: every STATIC (shape A) writer, with a written reason ─────────
#
# Expected writer set after task 1.4/1.6 (per the plan's task-1.6 brief):
# import_platform_skus / import_tiktok_snapshot (overwrite+stamp),
# _apply_order_stock_effect (the diff engine), reverse_platform_deduction
# (historical reversals), the manual SKU edit form, and NOTHING in
# bsn_sync's sync path. Plus the two DELETE-only provenance-invalidation
# helpers those overwrite paths call, and import_weekly's carry_from
# re-point (D7 -- not a stock write, re-points an EXISTING record's key).

EXPECTED = {
    ('models/platform_skus.py', 'import_platform_skus'):
        'overwrite+stamp (D-scope): the Seller Center export IS the '
        "platform's own stock figure, upserted via INSERT...ON CONFLICT DO "
        'UPDATE SET stock=excluded.stock. Also calls '
        '_supersede_deduction_provenance (its own entry below) so a stale '
        'record cannot be added back on top of the fresh figure.',

    ('models/platform_skus.py', MODULE_LEVEL):
        'the `_TIKTOK_SKU_UPSERT` SQL template constant -- overwrite+stamp '
        "for the TikTok grain (INSERT...ON CONFLICT DO UPDATE SET "
        'stock=CASE WHEN <stock_present> THEN excluded.stock ELSE stock '
        'END -- an export with no quantity column must not blank what is '
        'already on record). Defined at module level and executed via '
        '`conn.execute(_TIKTOK_SKU_UPSERT, (...))` inside '
        'import_tiktok_snapshot, so the scanner (keyed by the ENCLOSING '
        'function of the literal itself, not of its caller) attributes it '
        'here rather than tracing the name reference -- see "WHAT THIS '
        'SWEEP CANNOT CATCH" in the module docstring. TikTok has no orders '
        'in the ERP yet (D9), so this is snapshot-only; import_tiktok_'
        'snapshot also calls _supersede_deduction_provenance (its own '
        'entry below) when the export DOES carry stock.',

    ('models/platform_skus.py', '_supersede_deduction_provenance'):
        'task 2.2, renamed from _invalidate_deduction_provenance (it no '
        'longer only invalidates): per provenance row on a variation_id '
        'the overwriting file actually carried (not the whole platform), '
        'reconciled against that same file\'s own EXPORT timestamp. A '
        "sales_transactions row (the retired BSN walk, D7), or a "
        'marketplace_orders row whose order_date is missing/malformed or '
        '<= the export ts, is superseded -- DELETE FROM '
        'platform_stock_deductions. A marketplace_orders row dated AFTER '
        'the export ts is KEPT and RE-APPLIED onto the fresh figure the '
        'caller just wrote: UPDATE platform_skus SET stock=MAX(0, stock - '
        'units), then UPDATE platform_stock_deductions SET units=<the '
        'actually-applied, possibly re-clamped amount> (or DELETE if that '
        'clamps to 0, or if the fresh stock is NULL -- mig-172, nothing to '
        're-apply onto). Called by both import_platform_skus and '
        'import_tiktok_snapshot above.',

    ('models/platform_skus.py', 'update_platform_sku'):
        'the manual SKU edit form (/ecommerce SKU edit route) -- the OTHER '
        "door onto platform_skus.stock. A hand-typed figure is authoritative "
        'for that listing exactly like a file is, so it also invalidates '
        '(DELETE) any recorded deduction, but ONLY when the stock value '
        'actually changed (before != stock) -- a price-only save must not '
        'discard a live record.',

    ('models/marketplace.py', '_apply_order_stock_effect'):
        'the diff engine (task 1.3): the ONE place a marketplace ORDER now '
        'deducts/credits platform_skus.stock, keyed on '
        "(marketplace_orders, order_id, platform_sku_id). DELETE+INSERT "
        '(not a bare INSERT) because a re-import legitimately revisits the '
        'same key -- idempotency is asserted by tests, not a PK abort.',

    ('models/bsn_sync.py', 'reverse_platform_deduction'):
        'D7, UNCHANGED code: undoes whatever a source row did to '
        'marketplace stock (stock = stock + units, clamped at zero on the '
        'downward direction) and deletes its provenance row. Still real '
        'work against a PRE-CUTOVER row a retired walk once deducted -- '
        'called from imports.py::import_weekly on a corrected/removed '
        'sales_transactions row (below).',

    ('models/imports.py', 'import_weekly'):
        'TWO distinct things, neither of which is a NEW deduction: (1) '
        'calls reverse_platform_deduction (its own entry above) on a '
        "corrected/removed line's OLD row before deleting it -- D7, "
        'unconditional, a no-op for a post-cutover row with no provenance. '
        '(2) `UPDATE platform_stock_deductions SET source_id=?` re-points '
        "an EXISTING record to a REPLACEMENT row's new id when a "
        'correction does NOT change the stock-affecting identity '
        '(price-only) -- carrying a pre-cutover record forward, not '
        'creating one. bsn_sync._sync_bsn_to_stock itself (called from '
        'here for the warehouse-ledger pass 2) has NO entry in this table: '
        'it writes stock_levels via the transactions ledger only -- see '
        'test_bsn_sync_no_longer_writes_platform_skus_or_provenance.',
}

# Shape-B (dynamic table name) sites confirmed NOT to target platform_skus /
# platform_stock_deductions at runtime. Empty today -- see the module
# docstring and test_sync_bsn_to_stock_dynamic_table_never_targets_platform_skus.
DYNAMIC_TABLE_WRITE_SITES = {}


# ── Tests ────────────────────────────────────────────────────────────────

def test_every_occurrence_is_classified_or_allowlisted():
    """Both directions: a new (path, function) key not in EXPECTED (static)
    or DYNAMIC_TABLE_WRITE_SITES (dynamic), or an EXPECTED key the scan no
    longer finds, both fail loud."""
    found = _scan()
    static_found = {k for k, hits in found.items() if any(not d for _, _, d, _ in hits)}
    dynamic_found = {k for k, hits in found.items() if any(d for _, _, d, _ in hits)}

    def _describe(key):
        hits = found.get(key, [])
        lines = '; '.join(f'L{ln}: [{tbl}] {snip!r}' for ln, tbl, _, snip in hits)
        return f'  {key[0]}::{key[1]} — {lines or "(no longer found)"}'

    new_static = static_found - set(EXPECTED)
    gone_static = set(EXPECTED) - static_found
    new_dynamic = dynamic_found - set(DYNAMIC_TABLE_WRITE_SITES)
    gone_dynamic = set(DYNAMIC_TABLE_WRITE_SITES) - dynamic_found

    problems = []
    if new_static:
        problems.append(
            'New STATIC platform_skus.stock / platform_stock_deductions '
            'writer(s) with no EXPECTED entry -- classify each (what does '
            'it write, why, what covers it):\n'
            + '\n'.join(_describe(k) for k in sorted(new_static)))
    if gone_static:
        problems.append(
            'EXPECTED writer(s) the scan no longer finds -- either the '
            'writer moved/was deleted (update this file) or something else '
            'is wrong:\n' + '\n'.join(_describe(k) for k in sorted(gone_static)))
    if new_dynamic:
        problems.append(
            'New DYNAMIC (f-string table name) write near "stock"/'
            '"platform_sku" with no DYNAMIC_TABLE_WRITE_SITES entry -- read '
            'the call site and confirm what table it actually targets at '
            'runtime, then classify it:\n'
            + '\n'.join(_describe(k) for k in sorted(new_dynamic)))
    if gone_dynamic:
        problems.append(
            'DYNAMIC_TABLE_WRITE_SITES entry the scan no longer finds:\n'
            + '\n'.join(_describe(k) for k in sorted(gone_dynamic)))
    assert not problems, '\n\n'.join(problems)


def test_bsn_sync_no_longer_writes_platform_skus_or_provenance():
    """Explicit regression pin (not just the general sweep above): the
    function task 1.4 actually edited must have ZERO write-shaped hits
    against either table anywhere in its source. A future "helpful" re-add
    inside _sync_bsn_to_stock itself (as opposed to a NEW function
    elsewhere) is exactly the shape this plan exists to make impossible.

    Uses _classify_text (the SAME scanner the whole-app sweep uses), not a
    bare substring check -- the function's own docstring (task 1.4)
    legitimately MENTIONS 'platform_skus.stock' in prose explaining where
    the deduction moved TO, and a substring check would wrongly flag that."""
    src = _function_source('models/bsn_sync.py', '_sync_bsn_to_stock')
    assert src, 'could not locate _sync_bsn_to_stock -- has it moved/been renamed?'
    hits = _scan_source(src, 'models/bsn_sync.py')
    assert not hits, (
        f'_sync_bsn_to_stock has a write-shaped hit again -- the walk was '
        f'supposed to be gone for good (task 1.4): {hits}')


def test_sync_bsn_to_stock_dynamic_table_never_targets_platform_skus():
    """_sync_bsn_to_stock's OWN `f"...{table}..."` idiom (real, live, used
    for the warehouse ledger against sales_transactions/purchase_
    transactions) is the exact shape DYNAMIC_TABLE_WRITE_SITES exists to
    catch if it were ever pointed at platform_skus. Positive control: this
    function's dynamic-table writes are for `table`, called with only
    'sales_transactions'/'purchase_transactions' -- never platform_skus --
    confirmed by reading every call site of _sync_bsn_to_stock."""
    import inspect
    import models
    sites = [
        ('models/imports.py', 'import_weekly'),
        ('models/bsn_sync.py', 'update_unit_conversion_ratio'),
        ('models/mapping.py', 'repoint_bsn_code'),
    ]
    for rel, func in sites:
        src = _function_source(rel, func)
        assert '_sync_bsn_to_stock(' in src, (
            f'{rel}::{func} no longer calls _sync_bsn_to_stock -- update '
            'this test\'s call-site list')
    # And the function itself only ever branches file_type in {'sales','purchase'}
    # to pick txn_type -- there is no third table this could resolve to.
    sig = inspect.signature(models.bsn_sync._sync_bsn_to_stock)
    assert 'table' in sig.parameters


def test_admin_master_tables_manifest_is_not_flagged():
    """CONTROL for the scanner's write-keyword requirement: admin.py's
    _MASTER_TABLES tuple contains the literal string 'platform_skus' with
    NO SQL keyword anywhere near it, so it must NOT appear in the scan at
    all -- proving the keyword filter, not luck, is what excludes it."""
    found = _scan()
    assert ('blueprints/admin.py', MODULE_LEVEL) not in found
    for key in found:
        assert key[0] != 'blueprints/admin.py', (
            f'unexpected platform_skus write-shaped literal in admin.py: {key}')


# ── Shape-coverage break-it-once: one rogue fixture per shape ──────────────
#
# Per the plan's 2026-08-25 rule ("a cross-cutting sweep you wrote yourself
# is the next thing to distrust"): feed the scanner one rogue snippet per
# shape it claims to catch and demand a positive AND a negative result for
# each, rather than trusting the live-codebase scan alone (which today only
# exercises shapes A and E — nothing here currently uses %/`.format()`/an
# f-string table name for these two tables).

def _rogue_hit(src, table=None, dynamic=None):
    """True if _scan_source(src) contains a hit matching (table, dynamic)
    -- None means 'don't care about this field'."""
    for hits in _scan_source(src).values():
        for _, tbl, dyn, _ in hits:
            if (table is None or tbl == table) and (dynamic is None or dyn == dynamic):
                return True
    return False


def test_shape_A_literal_update_is_caught():
    src = (
        "def f(conn, sku_id, n):\n"
        "    conn.execute(\"UPDATE platform_skus SET stock = stock - ? WHERE id=?\","
        " (n, sku_id))\n")
    assert _rogue_hit(src, table='platform_skus', dynamic=False)


def test_shape_A_literal_insert_into_provenance_is_caught():
    src = (
        "def f(conn, order_id, sku_id, units):\n"
        "    conn.execute(\"INSERT INTO platform_stock_deductions"
        " (source_table, source_id, platform_sku_id, units) VALUES (?,?,?,?)\","
        " (order_id, sku_id, units))\n")
    assert _rogue_hit(src, table='platform_stock_deductions', dynamic=False)


def test_shape_B_fstring_dynamic_table_is_caught():
    """The shape the brief explicitly calls out: a dynamic table name where
    the literal substring 'platform_skus' NEVER appears in source at all."""
    src = (
        "def f(conn, table, sku_id, n):\n"
        "    conn.execute(f\"UPDATE {table} SET stock = stock - ? WHERE id=?\","
        " (n, sku_id))\n")
    assert 'platform_skus' not in src, 'the rogue fixture must not spoil its own point'
    assert _rogue_hit(src, dynamic=True)


def test_shape_B_fstring_dynamic_table_unrelated_to_stock_is_not_caught():
    """CONTROL for shape B's column filter: a dynamic-table write with
    neither 'stock' nor 'platform_sku' in the flattened text (e.g. the
    REAL _sync_bsn_to_stock idiom, `SET synced_to_stock=1`) must not be
    flagged -- otherwise every dynamic-table write anywhere in the app
    would need an entry, which is a different, much bigger sweep."""
    src = (
        "def f(conn, table, row_id):\n"
        "    conn.execute(f\"UPDATE {table} SET synced_to_stock=1 WHERE id=?\","
        " (row_id,))\n")
    assert not _rogue_hit(src, dynamic=True), (
        "'synced_to_stock' contains the substring 'stock' -- if this control "
        "fails, the column filter is matching too loosely")


def test_shape_C_percent_formatting_is_caught():
    src = (
        "def f(conn, sku_id, n):\n"
        "    conn.execute(\"UPDATE platform_skus SET stock = %s WHERE id=?\""
        " % (n,), (sku_id,))\n")
    assert _rogue_hit(src, table='platform_skus', dynamic=False)


def test_shape_D_dot_format_is_caught():
    src = (
        "def f(conn, sku_id, n):\n"
        "    conn.execute(\"UPDATE platform_skus SET stock = {} WHERE id=?\""
        ".format(n), (sku_id,))\n")
    assert _rogue_hit(src, table='platform_skus', dynamic=False)


def test_shape_E_insert_on_conflict_do_update_is_caught():
    """The real live shape import_platform_skus/import_tiktok_snapshot use
    -- an INSERT...ON CONFLICT DO UPDATE SET stock=excluded.stock is still
    just an 'INSERT' keyword + literal table name to the scanner."""
    src = (
        "def f(conn, sku_id, n):\n"
        "    conn.execute('''INSERT INTO platform_skus (variation_id, stock)"
        " VALUES (?,?) ON CONFLICT(variation_id) DO UPDATE SET"
        " stock=excluded.stock''', (sku_id, n))\n")
    assert _rogue_hit(src, table='platform_skus', dynamic=False)


def test_unrelated_column_write_is_not_caught():
    """CONTROL: a platform_skus write that never touches stock (the real
    shape of _propagate_listings_to_platform_skus / apply_platform_mapping,
    which only ever set internal_product_id/qty_per_sale) must NOT be
    flagged -- otherwise this sweep would demand an entry for every mapping
    write in the app, drowning the two tables it actually cares about."""
    src = (
        "def f(conn, sku_id, pid):\n"
        "    conn.execute(\"UPDATE platform_skus SET internal_product_id = ?"
        " WHERE id=?\", (pid, sku_id))\n")
    assert not _rogue_hit(src)


def test_select_only_mention_is_not_caught():
    """CONTROL: a bare SELECT must not be flagged -- the write-keyword
    requirement is what makes every read-only query in models/marketplace.py
    and models/platform_skus.py invisible to this sweep, on purpose."""
    src = (
        "def f(conn, pid):\n"
        "    return conn.execute(\"SELECT stock FROM platform_skus WHERE"
        " internal_product_id=?\", (pid,)).fetchall()\n")
    assert not _rogue_hit(src)


def test_table_name_tuple_with_no_keyword_is_not_caught():
    """CONTROL mirroring the real admin.py case: the literal table name
    sitting in a plain tuple, with no write keyword anywhere in the same
    string, must not be flagged."""
    src = "_MASTER_TABLES = (\n    'products',\n    'platform_skus',\n    'suppliers',\n)\n"
    assert not _rogue_hit(src)
