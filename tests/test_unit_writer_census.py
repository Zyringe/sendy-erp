"""Census: every writer of a stored unit column is declared through the map,
pending a named ticket, or exempt with a reason.

Why this exists (#595 · #598): the ADR-0018 spec says a unit's meaning comes
from the Express book it came from and its spelling is Sendy's choice, kept
in ONE database table (`unit_map`, read via `inventory_app/bsn_units.py`).
That rule holds only if EVERY place that writes a unit column honours it.
#596 built the map; #597 cleaned up the codes the importer already knows.
This ticket is the checklist for the writers that still don't: #599 (กร/ถง/บล
Express meaning), #601 (VAT-book book-awareness), #602 (product form /
promotions / suggestions / price lookup), #610 (the DBF sales-order-lines
writer, the credit-note importers, the supplier catalogue importer).
#600 runs a one-time historical relabel migration touching no call site, so
this census has no entries under its name. #599 is NOT call-site-free
(review N3 corrected this) — its own body says "Callers pass their book" —
but the sites it would touch (import_weekly, repoint_bsn_code,
seed_products_from_stmas) are already `through_map` below, because they
already call bsn_units against the current default book; #599's job there
is to make that book argument explicit, which lands as a reason-text
update on shipping, not a status flip out of `pending`.

Prior art: tests/test_revenue_filter_coverage.py (file-level ALLOWED +
guard-token-in-file) and tests/test_last_purchase_population_coverage.py
(function-level ALLOWED + AST query extraction). This test borrows the
query-extraction machinery from the second and the allowlist-with-reasons
shape from the first, then extends both: a SITE here is a (file, qualname)
pair, and each site can carry MORE THAN ONE declared column — because two
real sites in this app write two different stored columns from the SAME
function with two different provenances (`approve_pending_suggestion` writes
`product_code_mapping.bsn_unit` as a hardcoded placeholder, always exempt,
while its `unit_conversions.bsn_unit` write in the very same function is a
real raw value, pending #602).

The census was NOT built by grepping for "unit" and reading hits by eye —
that is exactly the failure mode `erp-engineering-discipline.md` warns
about ("Reading code file-by-file is what missed them"). It was built by:
  1. Enumerating every TEXT column whose name suggests a unit spelling,
     straight off the live schema (`pragma_table_info`), not off memory.
  2. Writing the mechanical sweep below FIRST, running it against the real
     tree, and then reading every hit — including the false positives,
     which is how `models/products.py::get_product` (a bare SELECT) and
     `naming_cascade.py::save_product` (a dynamic SET that structurally
     cannot reach `unit_type`) were caught and explained rather than
     silently mis-declared.
  3. Tracing every real hit to its actual caller to decide `through_map`
     vs `pending` vs `exempt` — two sites (`save_unit_conversions`,
     `upsert_unit_conversion`) have byte-identical SQL and neither calls
     bsn_units itself; both are `pending:#602` because neither caller
     provably pre-translates on EVERY path (review S1 found the ONE claim
     of a proven caller guarantee here was itself wrong on a re-read of the
     template — see `through_map_transitive`'s own section below for what
     it would actually take to earn that status).

What this census CANNOT see (say so up front, per #598's own AC):
  - a SQL string built from variables the sweep does not track (e.g. a
    column value assembled far from the query text);
  - a dynamic `SET {clause}`/`INSERT INTO {table}`/column list where the
    actual columns or table are computed at import time from something
    other than a literal in the SAME rendered string — the sweep only
    proves "this MIGHT touch a unit column"; whether it actually can is
    decided by reading the whitelist the dynamic clause draws from
    (documented per exempt entry, each pinned by its own test reading that
    whitelist directly rather than trusting the sweep's silence);
  - **that a `through_map` site's bsn_units call result is the value
    actually WRITTEN, for most sites** (review S4): the self-check below
    only proves the call is PRESENT somewhere in the function, which is
    necessary but not sufficient — a mutation that keeps the call but
    writes a DIFFERENT, untranslated variable stays green unless the
    translated value flows through a simple, traceable local assignment.
    Two sites (`scripts/import_express.py::_import_sales`,
    `vat_book_builder.py::seed_products_from_stmas`) have that simple
    shape and are additionally checked by
    `test_through_map_translated_value_reaches_a_write` — pinning that the
    variable assigned FROM the bsn_units call also appears inside the
    params of a `conn.execute`-shaped call in the same function. The other
    three (`import_weekly`'s in-place `e['unit'] = ...` dict mutation,
    `repoint_bsn_code`'s value renamed through a list comprehension and a
    tuple-unpack before reaching the write, `learn_acronyms_normalize`'s
    "teach the map AND write the same literal" shape with no intermediate
    variable at all) were checked BY READING, not mechanically — a naive
    identifier-reuse check would either miss the mutation (learn_acronyms_
    normalize) or FALSELY fail correct code that renames the value on the
    way to the write (repoint_bsn_code), which is worse than not checking;
  - a huge multi-statement string mixing unrelated CREATE TABLE / CREATE
    TRIGGER bodies (`database.py`'s legacy `SCHEMA` bootstrap constant) can
    report a false co-occurrence between a write verb in one statement and
    a column name in a much later, unrelated one — verified by reading, not
    guessed away;
  - anything outside `inventory_app/` and `scripts/`, and anything not
    `.py` (a `.sql` migration's one-time snapshot tables, e.g.
    `migration_186_uc_deleted`, are invisible on purpose: frozen historical
    artifacts written once by SQL, not by a Python code path);
  - a SQL constant defined at module level and referenced by name at its
    call site is attributed to the DEFINING scope, not the calling function
    (no such shape exists among today's real hits, but nothing here rules
    it out);
  - a matching/comparison key that is normalised in memory but never
    persisted to a stored column (`price_lookup.py`'s hand-coded unit alias
    list, `scripts/xp5_mapping_pipeline.py`'s line-signature normalisation)
    — #598 is a WRITER census; a read-time comparison is #602's/#601's
    concern, not this file's.

Columns considered and explicitly EXCLUDED as look-alikes (same word,
different meaning — checked, not assumed):
  - `unit_price`, `unit_cost`, `commission_overrides.fixed_per_unit` — money
    columns, never a หน่วย spelling.
  - `units`, `units_per_box`, `units_per_carton`, `qty_per_sale` — pack-size
    COUNTS, not unit spellings.
  - `transactions.unit_mode` — CHECK-constrained to the literal English
    words `unit`/`box`/`carton`, selecting the SCALE of a manual stock
    movement. It is never an Express code and ADR 0018 does not cover it.

It still passes once every `pending` entry above clears (see
test_the_census_survives_its_own_success) — the framework does not secretly
require an open pending entry to exist.
"""
import ast
import io
import os
import re
import sqlite3
import tokenize

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(_ROOT, 'inventory_app')
SCRIPTS = os.path.join(_ROOT, 'scripts')

# ── every stored TEXT column that can hold a unit spelling, read off the
# live schema (`pragma_table_info`, 2026-09-19) — not off memory, and not
# off #598's own dispatch list (which said explicitly: enumerate from the
# schema). Table -> [columns].
TARGET_COLUMNS = {
    'sales_transactions': ['unit'],
    'purchase_transactions': ['unit'],
    'unit_conversions': ['bsn_unit'],
    'products': ['unit_type'],
    'product_price_tiers': ['qty_label'],
    'promotions': ['bundle_unit'],
    'product_code_mapping': ['bsn_unit'],
    'pending_product_suggestions': ['bsn_unit', 'suggested_unit_type'],
    'express_sales': ['unit'],
    'express_sales_order_lines': ['unit'],
    'express_credit_note_lines': ['unit'],
    'credit_note_imports': ['unit'],
    'supplier_catalogue_items': ['unit'],
    'supplier_catalogue_price_history': ['unit'],
    'supplier_product_mapping': ['supplier_unit', 'erp_unit'],
}

# `\b` on BOTH sides of UPDATE/INSERT matters: without it "UPDATE" matches
# inside "updated_at" (a real false positive hit during development, on
# models/products.py::get_product's `p.updated_at, ... p.unit_type` SELECT).
# `REPLACE INTO` (bare, no leading INSERT) is SQLite shorthand for
# `INSERT OR REPLACE INTO` — added after review found the sweep blind to it.
_WRITE_VERB_RE = re.compile(
    r'\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?)\b', re.I)
# A write verb immediately followed by a HOLE — the table name is dynamic.
# Three hole shapes: an f-string `{expr}` (`express_registers.py::replace`'s
# `INSERT INTO {table.name}`, `learn_acronyms_normalize`'s `UPDATE {t} SET
# unit=...`), or a %-format `%s`/`%(name)s` (`"UPDATE %s SET unit=?" % table`
# renders as literal `%s` — see `_render`'s Mod handling).
_DYNAMIC_TABLE_RE = re.compile(
    r'\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?)'
    r'\s*(?:\{|%\(|%[a-z])', re.I)
# `INSERT INTO <literal, in-scope table> (<hole>` — the TABLE is known but
# the COLUMN LIST is dynamic (`f"INSERT INTO product_price_tiers ({cols})"`).
# Different from _DYNAMIC_TABLE_RE, which fires when the TABLE ITSELF is the
# hole; this one needs the table name resolved first, so it is applied by
# the caller (like _DYNAMIC_SET_RE) rather than matched standalone here.
_DYNAMIC_COLUMNS_RE = re.compile(
    r'\bINSERT(?:\s+OR\s+\w+)?\s+INTO\s+(\w+)\s*\(\s*(?:\{|%\(|%[a-z])', re.I)
# `UPDATE <literal table>[<alias>] SET <hole>` — the SET clause (not the
# table) is the hole. `.*?` (DOTALL) tolerates an optional alias
# (`UPDATE products AS p SET {..}` / `UPDATE products p SET {..}`) between
# the table name and SET — the review found the original anchored form
# (`\s+SET`, no alias) blind to this. Captured so the caller can check the
# table is one we care about; an unrelated dynamic-SET table
# (ar_followup_log, customers, leave_requests, label_company_block,
# customer_contact_review, a CRM upsert) is real code but out of scope and
# must not appear here.
_DYNAMIC_SET_RE = re.compile(r'\bUPDATE\s+(\w+)\b.*?\bSET\s*(?:\{|%\(|%[a-z])', re.I | re.S)
# Only the calls that actually TRANSLATE or WRITE a spelling count — is_known
# (a membership check) and load_unit_map (a bulk snapshot for local lookups)
# never return or persist a translated value, so a function calling only
# those has NOT gone through the map (review finding N4).
_BSN_UNITS_CALL_RE = re.compile(
    r'\bbsn_units\.(?:normalize_unit|translate|learn|add_acronym)\b')


def _code_only(src):
    """`src` with comments and docstrings removed — a guard call named only
    in a comment (e.g. `pass  # bsn_units.add_acronym(...) removed`) is not
    a guard. Tokenize-based, like test_revenue_filter_coverage.py's
    `_code_only` — but joined WITHOUT a blanket `\\n` separator: that file's
    GUARD_TOKENS are single identifiers, so `\\n`.join (one token per line)
    still leaves each one findable as a substring. This file's checks are
    DOTTED calls (`bsn_units.add_acronym`, `acr_full.get(`) spanning THREE
    tokens (NAME, OP '.', NAME) — `\\n`.join was proven to break adjacency
    between them (every through_map self-check went red against unmutated
    code the first time this ran).

    Plain `''.join` alone has its OWN gap, found while checking N4 by hand:
    two adjacent NAME-shaped tokens glue into one run with no boundary
    between them wherever real source had a space Python's grammar
    requires but the token strings themselves don't carry — `return
    bsn_units.normalize_unit(...)` collapses to `returnbsn_units...`, and
    `\\bbsn_units\\b` cannot fire between two word characters. None of
    today's 5 real through_map calls happen to sit right after a bare
    keyword (all are preceded by `=`, `(`, or start-of-line, which stay
    correctly separated even under plain concatenation), so this was
    invisible to every real assertion in this file — until a synthetic
    `return bsn_units.normalize_unit(...)` check surfaced it. Fixed by
    inserting a single space wherever two emitted pieces would otherwise
    join two word-characters into one; a `.`-adjacent dotted call stays
    glued (`.` is not a word character), so the fix that JOINS this gap
    does not REOPEN the one `\\n`.join left. Proven correct, not assumed,
    by test_code_only_strips_comments_but_keeps_real_calls and
    test_code_only_separates_adjacent_word_tokens below."""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.line.lstrip().startswith(('"""', "'''")):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src          # unparseable: fall back to the raw text, never skip
    pieces = []
    for s in out:
        if (pieces and pieces[-1] and s
                and (pieces[-1][-1].isalnum() or pieces[-1][-1] == '_')
                and (s[0].isalnum() or s[0] == '_')):
            pieces.append(' ')
        pieces.append(s)
    return ''.join(pieces)


def _placeholder(node):
    """`{source text of node}` — the same brace-hole spelling `_render` uses
    for an f-string substitution, reused for every OTHER kind of hole
    (a `+`-concatenated variable, a `%`-format value) so all three read as
    one dynamic-table/dynamic-column shape to the DYNAMIC regexes instead of
    three different blind spots."""
    try:
        return '{' + ast.unparse(node) + '}'
    except Exception:
        return '{?}'


def _render(node):
    """Source text of a string expression. f-string holes are kept as
    `{expr}` (so a dynamic table/column name shows up as a literal brace for
    the DYNAMIC detectors, never as if it were resolved); `.format()` is
    treated as a passthrough of its template (the call's own substitution
    args are not more query text). Mirrors
    test_last_purchase_population_coverage.py's `_render`, plus `.format()`,
    `%`-format, and a `+`-concatenation with a NON-string operand.

    The last two were review findings (S5): `"UPDATE " + table + " SET
    unit=?"` used to render as None THE MOMENT ANY operand failed to
    render — `_render(BinOp)` required BOTH sides to already be strings, so
    a bare variable anywhere in a `+`-chain made the WHOLE expression
    invisible, not just that one hole. Now a `+` with at least one string
    side placeholders the other; a `+`-chain of NON-string pieces (`5 + 3`)
    still correctly renders as None (returning early keeps a `_hits_in_src`
    that filters `_WRITE_VERB_RE` from mistaking ordinary arithmetic for a
    dynamic query — proven by the 'plain arithmetic is not a string' shape
    test below).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            parts.append(str(v.value) if isinstance(v, ast.Constant) else _placeholder(v.value))
        return ''.join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else _placeholder(node.left)) + \
               (right if right is not None else _placeholder(node.right))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        # `"UPDATE %s SET unit=?" % table` — keep the literal template
        # (its `%s`/`%(name)s` holes included) and drop the substitution
        # values; a bare `x % y` with no string template renders as None.
        return _render(node.left)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'format'):
        base = _render(node.func.value)
        if base is not None:
            return base
    return None


def _queries(src):
    """(dotted qualname, rendered text) for every string VALUE in `src`,
    scoped to its innermost enclosing function/class. A bare docstring (an
    `Expr` statement whose value renders) is prose, not a query, and is
    skipped — identical logic to the prior-art reader this is copied from."""
    out = []
    tree = ast.parse(src)

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child.name])
            elif isinstance(child, ast.Expr) and _render(child.value) is not None:
                continue
            elif _render(child) is not None:
                out.append(('.'.join(scope) or '<module>', _render(child)))
            else:
                visit(child, scope)

    visit(tree, [])
    return out


def _hits_in_src(src):
    """{qualname: frozenset(labels)} for every write-shaped SQL string.

    A label is `<table>.<column>` (a literal table AND column both present
    in the same rendered string, alongside a write verb), `DYNAMIC-TABLE`
    (the table name itself is a hole), `DYNAMIC-COLUMNS:<table>` (a literal,
    in-scope table with a dynamically-built INSERT column list), or
    `DYNAMIC-SET:<table>` (a literal, in-scope table with a dynamically-
    built SET clause).

    Deliberately over-inclusive, same stance as
    test_revenue_filter_coverage.py's `_SUM_NET`: a column can appear in a
    WHERE clause rather than being SET (`update_unit_conversion_ratio`'s
    `bsn_unit=?` selects which row to touch; it is not written), and a
    dynamic-SET table can have a whitelist that never reaches the column in
    question (`naming_cascade.py::save_product`). Both cost one exempt
    entry, verified by reading the actual whitelist — cheaper than a missed
    real writer.
    """
    out = {}
    for func, sql in _queries(src):
        if not _WRITE_VERB_RE.search(sql):
            continue
        labels = set()
        for table, cols in TARGET_COLUMNS.items():
            if not re.search(rf'\b{re.escape(table)}\b', sql):
                continue
            for col in cols:
                if re.search(rf'(?:\w+\.)?\b{re.escape(col)}\b', sql):
                    labels.add(f'{table}.{col}')
        if _DYNAMIC_TABLE_RE.search(sql):
            labels.add('DYNAMIC-TABLE')
        m = _DYNAMIC_COLUMNS_RE.search(sql)
        if m and m.group(1).lower() in TARGET_COLUMNS:
            labels.add(f'DYNAMIC-COLUMNS:{m.group(1)}')
        m = _DYNAMIC_SET_RE.search(sql)
        if m and m.group(1).lower() in TARGET_COLUMNS:
            labels.add(f'DYNAMIC-SET:{m.group(1)}')
        if labels:
            out.setdefault(func, set()).update(labels)
    return {k: frozenset(v) for k, v in out.items()}


def _py_files(app_dir=APP, scripts_dir=SCRIPTS):
    """(site-prefix, path) for every .py file under `app_dir` (bare
    relpath) and `scripts_dir` ('scripts/'-prefixed), tests excluded.
    Parametrised so the scripts/-file break-it-once test can point it at a
    throwaway tree instead of the real repo."""
    for base, prefix in ((app_dir, ''), (scripts_dir, 'scripts/')):
        if base is None or not os.path.isdir(base):
            continue
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ('__pycache__', 'instance', 'static')]
            if os.sep + 'tests' + os.sep in root + os.sep:
                continue
            for n in names:
                if n.endswith('.py'):
                    path = os.path.join(root, n)
                    yield prefix + os.path.relpath(path, base).replace(os.sep, '/'), path


def _census(app_dir=APP, scripts_dir=SCRIPTS):
    """{'<relpath>::<qualname>': frozenset(labels)} for the whole tree."""
    out = {}
    for rel, path in _py_files(app_dir, scripts_dir):
        with open(path, encoding='utf-8') as f:
            src = f.read()
        try:
            hits = _hits_in_src(src)
        except SyntaxError:
            continue
        for func, labels in hits.items():
            out[f'{rel}::{func}'] = labels
    return out


def _function_source(path, qualname):
    """Exact source text of the function named by dotted `qualname` in
    `path`, or None if not found. Used only by the through_map self-check —
    scoped to the ONE function a status is claimed for, not the whole file,
    so a bsn_units call in a sibling function cannot launder an unrelated
    site's claim."""
    with open(path, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)
    parts = qualname.split('.')

    def find(node, remaining):
        if not remaining:
            return node
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and child.name == remaining[0]):
                return find(child, remaining[1:])
        return None

    node = find(tree, parts)
    if node is None:
        return None
    return ast.get_source_segment(src, node)


# ── The census itself ────────────────────────────────────────────────────
#
# site -> {label: (status, reason)}. status is 'through_map',
# 'through_map_transitive' (caller pre-translates; see the dedicated
# positive-control test), 'pending:#NNN', or 'exempt'.

ALLOWED = {
    # ── through_map: the function's OWN source calls bsn_units ───────────
    'models/imports.py::import_weekly': {
        'DYNAMIC-TABLE': ('through_map',
            'Calls bsn_units.normalize_unit(e.get("unit"), conn=conn) on '
            'every row before the dynamic INSERT/UPDATE into '
            'sales_transactions/purchase_transactions — the shared choke '
            'point for the weekly CSV, the DBF-sourced sales/purchase '
            'entries (express_dbf_source.py::build_sales_entries / '
            'build_purchase_entries feed it the same shape), and the '
            'VAT-book build (vat_book_builder.py reuses this function '
            'verbatim). CAVEAT: it never receives a `book=` argument, so '
            'when reused for the xp5 VAT-book build it silently applies '
            'BSN5657 meanings — #601 is the ticket that makes this call '
            'book-aware; it is still a real bsn_units call today, so '
            'through_map, not pending.'),
    },
    'models/mapping.py::repoint_bsn_code': {
        # Review S2: the two labels below were SWAPPED in the previous
        # version of this entry. Re-read line by line: the dynamic-table
        # write (~line 674, `UPDATE {table} SET synced_to_stock=0`) never
        # names a unit column at all — that one is exempt. The
        # product_code_mapping write (~line 621-633) is the real
        # through_map site: when the caller passes a bsn_unit, `norm_unit =
        # bsn_units.normalize_unit(bsn_unit, conn=conn)` (line 515) feeds
        # `target_unit_rows = [(norm_unit,)]`, which is what gets written.
        # Only in the OTHER branch (no bsn_unit passed — repoint the WHOLE
        # code) does it fall back to copying each EXISTING row's own
        # already-stored bsn_unit verbatim — never a NEW raw spelling
        # either way.
        'product_code_mapping.bsn_unit': ('through_map',
            'Calls bsn_units.normalize_unit(bsn_unit, conn=conn) (line '
            '515) and writes ITS result (via target_unit_rows -> '
            'unit_value) when the caller passes a bsn_unit; when no '
            'bsn_unit is passed, it instead copies each row\'s own '
            'EXISTING bsn_unit verbatim (never introduces a new spelling '
            'in that branch either).'),
        'DYNAMIC-TABLE': ('exempt',
            'The dynamic per-table UPDATE (~line 674, "UPDATE {table} SET '
            'synced_to_stock=0 WHERE product_id IN (...)") only ever sets '
            'synced_to_stock — it never names a unit column. This is a '
            'DIFFERENT statement than the product_code_mapping write '
            'above; both live in this function but do not interact.'),
    },
    'models/bsn_sync.py::learn_acronyms_normalize': {
        'DYNAMIC-TABLE': ('through_map',
            'This IS the naming call: for every acronym Put types on '
            '/unit-conversions it calls bsn_units.add_acronym(acr, full, '
            'conn=conn) — the map\'s own learn() — and then rewrites '
            'sales_transactions/purchase_transactions.unit to the full '
            'word via a dynamic per-table UPDATE, so the ledger never '
            'shows the acronym again.'),
    },
    'vat_book_builder.py::seed_products_from_stmas': {
        'products.unit_type': ('through_map',
            'Calls bsn_units.normalize_unit(str(r.get("QUCOD")...), '
            'conn=conn) before seeding products.unit_type from the '
            'Express STMAS stock master, in the SEPARATE vat_book.db '
            'build. Same book-unaware caveat as import_weekly above — '
            '#601\'s job, not a raw write.'),
        'product_code_mapping.bsn_unit': ('exempt',
            'The mapping row this function inserts always writes bsn_unit '
            'as the literal empty string (the non-split catch-all row) — '
            'never a real code.'),
    },
    'scripts/import_express.py::_import_sales': {
        'express_sales.unit': ('through_map',
            'Calls bsn_units.normalize_unit(r.unit, conn=conn) — comment '
            'on the line above says why: "both the mapping resolver and '
            'the canonical express_sales.unit value must be on the '
            'canonical alias" — then inserts the normalised word, never '
            'the raw Express code.'),
    },

    # through_map_transitive (a caller PROVABLY pre-translates on every
    # path) currently has NO entries — see review S1: save_unit_conversions
    # was declared through_map_transitive here on the strength of the
    # ACRONYM path alone, but blueprints/bsn.py::unit_conversions_save's
    # Pass-2 substitution (`acr_full.get((pid_s, bsn_unit), bsn_unit)`,
    # bsn.py:137) only swaps a value when Put ALSO typed a name into that
    # row's `fullunit_<pid>_<acronym>` box (unit_conversions.html:76-77) —
    # a SEPARATE, optional input from the `ratio_<pid>_<bsn_unit>` field
    # every row (acronym or not) always renders (unit_conversions.html:73,
    # 100). Put can fill in a ratio while leaving the full-name box blank,
    # and that row's raw acronym reaches save_unit_conversions untranslated.
    # Moved to pending:#602 below, next to its structurally-identical
    # sibling upsert_unit_conversion. The status stays a valid, testable
    # value (test_every_through_map_transitive_entry_names_an_existing_
    # control proves ANY future entry here must name a real control test,
    # so a later ticket cannot "clear" a pending entry into this status
    # without evidence) — it is simply unoccupied today.

    # ── pending:#602 (product form / promotions / suggestions / price
    # lookup / the /unit-conversions naming flow) ─────────────────────────
    'models/products.py::create_product': {
        'products.unit_type': ('pending:#602',
            'Writes unit_type verbatim from the caller\'s dict with no '
            'normalisation at all (not even the local strip-only '
            'normalize_unit_type). No live caller today (create_structured '
            'product is the canonical path both /products/new and '
            'Smart-Suggest approve use) — kept honest as pending rather '
            'than exempt in case it is ever revived.'),
    },
    'models/products.py::create_structured_product': {
        'products.unit_type': ('pending:#602',
            'Calls normalize_unit_type(d.get("unit_type")) — strip, then '
            'fall back to \'ตัว\' — never bsn_units. Both real create '
            'entry points route through here (/products/new\'s hand form '
            'and Smart-Suggest approval), so this is THE choke point #602 '
            'should fix.'),
    },
    'models/products.py::update_product': {
        'DYNAMIC-SET:products': ('pending:#602',
            '_UPDATABLE_PRODUCT_COLUMNS includes unit_type (verified: '
            'models/products.py, the tuple literally names it), and this '
            'is the LIVE write path behind /products/<id>/edit '
            '(blueprints/products.py::product_edit inlines its own raw '
            '`f.get("unit_type", "ตัว").strip() or "ตัว"` and hands it '
            'straight to this dynamic SET). No bsn_units call anywhere on '
            'this path.'),
    },
    'models/promotions.py::create_promotion': {
        'promotions.bundle_unit': ('pending:#602',
            'Writes data.get("bundle_unit") verbatim. Exported via '
            'models/__init__.py but has NO live caller today (grepped: '
            'only its own definition and the re-export) — kept pending, '
            'not exempt, for the same reason as create_product above.'),
    },
    'models/promotions.py::replace_promotion': {
        'promotions.bundle_unit': ('pending:#602',
            'Writes data.get("bundle_unit") verbatim. This IS the live '
            'path: blueprints/products.py\'s promotion-save route builds '
            '`bundle_unit: _opt_str(f.get("bundle_unit"))` raw from the '
            'form and calls this.'),
    },
    'models/bsn_sync.py::upsert_unit_conversion': {
        'unit_conversions.bsn_unit': ('pending:#602',
            'No bsn_units call in this function. Its one caller — '
            'blueprints/bsn.py::mapping_save\'s \'map\' action — strips '
            'the client-posted bsn_unit and passes it straight through '
            'with no pre-translation step at all (its ONLY guarantee, if '
            'any, is that the client value already originated from an '
            'already-normalised sales_transactions.unit via '
            'bsn_suggest.py — implicit, not enforced).'),
    },
    'models/bsn_sync.py::save_unit_conversions': {
        'unit_conversions.bsn_unit': ('pending:#602',
            'No bsn_units call in this function. Its one caller — '
            'blueprints/bsn.py::unit_conversions_save — DOES pre-translate '
            'the acronym path (Pass 1 learns any full name Put typed, '
            'Pass 2 substitutes it in), but review S1 found that guarantee '
            'is PARTIAL: Pass 2 only swaps a value when Put ALSO filled '
            'in that row\'s optional "หน่วยเต็ม" box '
            '(unit_conversions.html:76) — the ratio box next to it '
            '(:73/:100) can be submitted alone, on ANY row including an '
            'unknown acronym, and Pass 2\'s `acr_full.get((pid, unit), '
            'unit)` then falls back to the raw, un-substituted value. So '
            'a raw acronym CAN reach this function with a filled ratio '
            'and a blank full-name box — same structural gap as its '
            'sibling upsert_unit_conversion above, not a proven '
            'transitive guarantee.'),
    },
    'models/suggestions.py::approve_pending_suggestion': {
        'unit_conversions.bsn_unit': ('pending:#602',
            'Writes d.get("bsn_unit") (the suggestion\'s stored/edited '
            'value) straight into a new unit_conversions row with no '
            'bsn_units call on this path.'),
        'product_code_mapping.bsn_unit': ('exempt',
            'Both the UPDATE (WHERE bsn_unit=\'\') and the fallback INSERT '
            '(bsn_unit column literal \'\') in this function always use '
            'the empty-string non-split catch-all — never a real code, '
            'same shape as vat_book_builder\'s mapping insert above.'),
    },
    'models/suggestions.py::save_pending_suggestion': {
        'pending_product_suggestions.bsn_unit': ('pending:#602',
            'Writes data.get("bsn_unit") straight through with no '
            'normalisation in this function (confirmed: it only '
            'defaults missing extras to None, never translates). The '
            'value is client-payload-sourced (blueprints/bsn.py::'
            '_build_suggestion_payload reads item.get("bsn_unit") from '
            'the mapping page\'s JS, which the operator can edit before '
            'staging) — unlike the transitive-safe sites above, there is '
            'no explicit re-translation step guaranteeing this is always '
            'already-normalised, so it is pending, not through_map.'),
        'pending_product_suggestions.suggested_unit_type': ('pending:#602',
            'Same function, same client-payload provenance, same absence '
            'of a translate step, as bsn_unit above.'),
    },
    'scripts/import_catalog_pricing.py::_execute_ops': {
        'product_price_tiers.qty_label': ('pending:#602',
            'Writes tier qty_label verbatim from a hand-curated catalog '
            'CSV (normalize_base_price.py\'s sendy_unit_type column — a '
            'person-typed spelling, not an Express code). NOT literally '
            'named in #602\'s body (which lists product form / promotions '
            '/ suggestions / price lookup / the /unit-conversions pending '
            'list) — flagged in the PR body as a scope gap; #602 is the '
            'closest fit because the fix is the same shape ("a variant '
            'typed at data-entry becomes its word on save").'),
        'promotions.bundle_unit': ('pending:#602',
            'The SAME function also opens/reopens promotions from the '
            'catalog CSV and writes row.get("bundle_unit", "").strip() '
            'raw — same reasoning and same scope-gap flag as the tier '
            'write above.'),
    },

    # ── pending:#610 (the DBF sales-order-lines writer, the credit-note
    # importers, the supplier catalogue importer) ────────────────────────
    'express_registers.py::replace': {
        'DYNAMIC-TABLE': ('pending:#610',
            'The fully generic register-replacement writer (table name AND '
            'column list both computed at runtime from a RegisterTable '
            'dataclass, so no static text ever names "unit"). It executes '
            'whatever express_dbf_source.py::build_sales_order_records '
            'hands it, and that builder sets \'unit\': _plain(row, '
            '\'TQUCOD\') — the raw Express code, never normalised. This is '
            '#610\'s "DBF sales-order-lines writer" bullet.'),
    },
    'scripts/import_express.py::_import_credit_notes_records': {
        'express_credit_note_lines.unit': ('pending:#610',
            'Writes ln[\'unit\'] verbatim for every credit-note line. Fed '
            'by BOTH the text-report parser (p_cn.parse_credit_notes) and '
            'the DBF path (express_dbf_source.py::build_credit_notes_ap_'
            'records, which also sets \'unit\': l.get(\'TQUCOD\') raw) — '
            '#610\'s "credit-note importers" bullet, both paths in one '
            'function.'),
    },
    'import_credit_notes.py::_process_entry': {
        'credit_note_imports.unit': ('pending:#610',
            'Writes entry["unit"] verbatim into the credit_note_imports '
            'side table with no bsn_units call anywhere in this file '
            '(confirmed: no `import bsn_units`). #610\'s "credit-note '
            'importers" bullet — the SECOND of the two credit-note writers '
            'it names, alongside the scripts/import_express.py one above.'),
    },
    'scripts/import_supplier_catalogue.py::upsert_item': {
        'supplier_catalogue_items.unit': ('pending:#610',
            'Writes row["unit"] verbatim (a supplier\'s own Excel '
            'spelling, e.g. the ขด/ขีด collision #610\'s own body names). '
            '#610\'s explicit "supplier catalogue importer" bullet.'),
        'supplier_catalogue_price_history.unit': ('pending:#610',
            'Same INSERT OR REPLACE statement, same row["unit"], same '
            'reasoning as the items-table write above.'),
    },

    # ── exempt: no ticket needed — verified NOT a raw-code risk ──────────
    'database.py::<module>': {
        lbl: ('exempt',
            'This is the `SCHEMA` bootstrap constant (a multi-thousand-'
            'line string of CREATE TABLE / CREATE TRIGGER DDL, executed '
            'via conn.executescript on every boot of an EXISTING db — a '
            'fresh db instead builds from data/schema.sql). Pure DDL, no '
            'runtime data write. Every one of these 6 labels is the '
            'sweep\'s own documented blind spot: a write verb from one '
            'CREATE TRIGGER body (audit_log inserts) co-occurring, purely '
            'by string proximity across a multi-thousand-line string, with '
            'an unrelated table/column DECLARATION (a plain `unit_type '
            'TEXT ...` in a CREATE TABLE) far below it. Verified by '
            'reading: this file contains zero runtime INSERT/UPDATE that '
            'sets unit_type, bundle_unit or bsn_unit to anything.')
        for lbl in ('product_code_mapping.bsn_unit', 'products.unit_type',
                    'promotions.bundle_unit', 'purchase_transactions.unit',
                    'sales_transactions.unit', 'unit_conversions.bsn_unit')
    },
    'models/_shared.py::declared_update': {
        'DYNAMIC-TABLE': ('exempt',
            'The generic single-row migration-173 declared-change writer '
            '(table + column dict supplied by the caller). Every current '
            'caller checked (import_credit_notes.py, models/_shared.py '
            'itself, models/mapping.py x2, scripts/2026_09_19_split_'
            'belco_582.py, scripts/merge_product.py) passes only '
            'product_id, synced_to_stock, or ref_invoice — never a unit '
            'column. A future caller that DOES pass one would need its '
            'own site declared here; nothing in this function translates '
            'for it.'),
    },
    'models/bsn_sync.py::_sync_bsn_to_stock': {
        'DYNAMIC-TABLE': ('exempt',
            'The dynamic UPDATE here only ever sets synced_to_stock=1 '
            '(verified: both occurrences in this function). It never '
            'touches the unit column.'),
    },
    'models/bsn_sync.py::dismiss_pending_unit_conversion': {
        'DYNAMIC-TABLE': ('exempt',
            'bsn_unit/unit appear only in WHERE-clause row selection (the '
            'protected-row COUNT check and the DELETE predicate); the '
            'dynamic SET clause writes change_source/change_actor/'
            'change_reason metadata before the delete, never a unit '
            'value.'),
    },
    'models/bsn_sync.py::update_unit_conversion_ratio': {
        'DYNAMIC-TABLE': ('exempt',
            'Its dynamic UPDATE (part of the ledger-rebuild-after-ratio-'
            'change flow) only sets synced_to_stock=0.'),
        'unit_conversions.bsn_unit': ('exempt',
            'bsn_unit appears only in the literal '
            '"UPDATE unit_conversions SET ratio=? WHERE product_id=? AND '
            'bsn_unit=?" — a WHERE-clause key selecting which conversion '
            'row\'s ratio to change. Only ratio is SET.'),
    },
    'models/mapping.py::upsert_mapping': {
        'product_code_mapping.bsn_unit': ('exempt',
            'The parameter exists (for a future split-mapping caller) but '
            'every current caller — both call sites in '
            'blueprints/bsn.py::mapping_save — omits it, taking the '
            'default \'\' (the non-split catch-all row). If a future '
            'caller ever passes a real bsn_unit here, nothing in this '
            'function translates it first; that caller would need its own '
            'declared site.'),
    },
    'scripts/dump_schema.py::_data_sql': {
        'DYNAMIC-TABLE': ('exempt',
            'DATA_TABLES = {"unit_map": (...)} only (verified by reading '
            'the module constant) — this dumps the unit_map TABLE ITSELF '
            'into data/schema.sql\'s seed data for a fresh DB build. It is '
            'the map, not a consumer of it; out of this census\'s scope '
            'by definition.'),
    },
    'scripts/merge_product.py::main': {
        'DYNAMIC-TABLE': ('exempt',
            'The dynamic per-table UPDATE in the product-merge sweep only '
            'ever sets product_id=? (reassigning FK ownership from the '
            'losing product to the surviving one); its OWN unit_conversions '
            'handling a few lines earlier is two literal, non-dynamic '
            'statements (a SELECT and an UPDATE ... SET product_id=?) that '
            'never touch the bsn_unit VALUE, only read/match it — no hit '
            'on those two, verified by reading.'),
    },
    'naming_cascade.py::save_product': {
        'DYNAMIC-SET:products': ('exempt',
            '_EDITABLE_TEXT — the whitelist this dynamic SET clause draws '
            'from — is (series, model, size, color_code, packaging_th, '
            'condition, pack_variant, sub_category). unit_type is not in '
            'it (verified by reading the tuple), so this write can never '
            'reach unit_type regardless of what `fields` contains. Pinned '
            'by test_naming_cascade_whitelist_still_excludes_unit_type.'),
    },
    'scripts/apply_normalize_round1.py::main': {
        'DYNAMIC-SET:products': ('exempt',
            'APPLY_FIELDS (the whitelist feeding this dynamic SET) lists '
            'series/model/size/color_code/packaging_th/packaging_short/'
            'condition/pack_variant only — no unit_type. Pinned by '
            'test_apply_normalize_round1_whitelist_still_excludes_unit_type.'),
    },
    'scripts/apply_product_naming.py::apply_ops': {
        'DYNAMIC-SET:products': ('exempt',
            '_FIELD_WHITELIST (checked at the top of this function before '
            'any write, per its own "field whitelisted above" comment) is '
            '(color_code, brand_id, model, size, packaging_th, '
            'packaging_short, series, condition, pack_variant, '
            'sub_category_short_code) — no unit_type. Pinned by '
            'test_apply_product_naming_whitelist_still_excludes_unit_type.'),
    },

    # ── exempt: one-off historical scripts (already run against prod;
    # frozen; not a recurring writer). Grouped by shared reasoning, still
    # one entry per mechanically-found site so a status FLIP is still
    # caught. Every file below is independently self-documenting as one-off
    # (a date in the filename, an explicit "DEPRECATED"/"One-off"/"One-shot"
    # docstring, or a specific ticket number in the name) — verified by
    # reading each one, not assumed from the naming pattern alone.
    'scripts/2026_08_17_bolt_dozen_to_piece.py::convert': {
        'product_price_tiers.qty_label': ('exempt', 'One-off, dated 2026-08-17 (the โหล-to-piece dozen conversion), already run against prod.'),
        'products.unit_type': ('exempt', 'Same one-off script, dated 2026-08-17, already run against prod.'),
    },
    'scripts/2026_09_19_fix_pack_ratios_592.py::fix': {
        'unit_conversions.bsn_unit': ('exempt',
            'One-off, dated + ticketed #592, already run. Structurally safe '
            'too: the write is "UPDATE unit_conversions SET ratio=? WHERE '
            'product_id=? AND bsn_unit=?" — bsn_unit is a WHERE key, only '
            'ratio is SET (review N2).'),
        'DYNAMIC-TABLE': ('exempt',
            'Same one-off script (#592). The %-format dynamic write '
            '("UPDATE %s SET synced_to_stock=0 WHERE product_id=?" % table) '
            'only ever sets synced_to_stock, never a unit column.'),
    },
    'scripts/2026_09_19_gross_to_piece.py::rebase': {
        'products.unit_type': ('exempt', 'One-off, dated 2026-09-19 (the 1050/1320 gross-to-piece rebase), already run against prod.'),
        'unit_conversions.bsn_unit': ('exempt', 'Same one-off gross-to-piece rebase script, dated 2026-09-19, already run.'),
        'DYNAMIC-TABLE': ('exempt',
            'Same one-off script. The %-format dynamic write '
            '("UPDATE %s SET synced_to_stock=0 WHERE product_id=?" % table) '
            'only ever sets synced_to_stock, never a unit column.'),
    },
    'scripts/2026_09_19_rebase_689_767.py::apply_tiers': {
        'product_price_tiers.qty_label': ('exempt', 'One-off, dated 2026-09-19 (pid 689/767 rebase), already run; writes hardcoded literals only.'),
    },
    'scripts/2026_09_19_split_belco_582.py::split': {
        'unit_conversions.bsn_unit': ('exempt', 'One-off, dated + ticketed #582, already run.'),
        'DYNAMIC-TABLE': ('exempt',
            'Same one-off script (#582). The %-format dynamic write '
            '("UPDATE %s SET synced_to_stock=0 WHERE product_id IN (?,?)" '
            '% table) only ever sets synced_to_stock, never a unit column.'),
    },
    'scripts/apply_decision_ratios.py::main': {
        'unit_conversions.bsn_unit': ('exempt', 'One-off decision-application script (no date in name, but its own docstring scopes it to one specific ratio-decision batch), already run.'),
    },
    'scripts/apply_decision_remaps.py::main': {
        'DYNAMIC-TABLE': ('exempt', 'One-off "Bucket C+E" remap script (own docstring names the specific decision batch), already run; the dynamic part only reassigns product_id.'),
        'DYNAMIC-COLUMNS:products': ('exempt',
            'Same one-off Bucket C+E remap script — its clone-a-sibling '
            'branch ("INSERT INTO products ({\',\'.join(cols)}) VALUES '
            '...") copies a whole row\'s columns including unit_type '
            'verbatim from an EXISTING product, already applied against '
            'prod; not a new-code-path risk.'),
        'products.unit_type': ('exempt', 'Same one-off script; a "guessed" unit_type for a minimal new row, already applied against prod.'),
        'unit_conversions.bsn_unit': ('exempt', 'Same one-off Bucket C+E remap script, already applied against prod.'),
    },
    'scripts/apply_worksheets_20260530.py::main': {
        'products.unit_type': ('exempt', 'One-off, dated 2026-05-30 (worksheet application), already run against prod.'),
        'unit_conversions.bsn_unit': ('exempt', 'Same one-off worksheet-application script, dated 2026-05-30, already run.'),
    },
    'scripts/apply_worksheets_20260530.py::plan_insert_unit_conversion': {
        'unit_conversions.bsn_unit': ('exempt', 'Same one-off script as above; a planning helper for the same already-run batch.'),
    },
    'scripts/backfill_express_unit_normalize.py::normalize_units': {
        'express_sales.unit': ('exempt', 'Own docstring: "DEPRECATED: one-off from 2026-05-20 ... Do not re-run."'),
    },
    'scripts/backfill_nonstock_2026_08.py::main': {
        'sales_transactions.unit': ('exempt', 'Own docstring: "One-off: backfill the 3 888ค8888 lines ... run once, verify, then archive as .py.txt."'),
    },
    'scripts/cleanup_split_mapping_stubs.py::cleanup_pair': {
        'DYNAMIC-TABLE': ('exempt', 'Own docstring: "DEPRECATED: one-off from 2026-05-20. Kept for audit trail. Do not re-run."'),
        'unit_conversions.bsn_unit': ('exempt', 'Same file, same "DEPRECATED: one-off from 2026-05-20 ... do not re-run" docstring.'),
    },
    'scripts/fix_k11_1155_unit_conversion.py::fix_live_db': {
        'unit_conversions.bsn_unit': ('exempt', 'One-off, named for one specific SKU pair (K11/1155), already run.'),
    },
    'scripts/import_listing_mapping_csv.py::create_stub_product': {
        'products.unit_type': ('exempt', 'Own docstring: "One-shot import for listing_mapping_cleaned_20260427.csv."'),
    },
    'scripts/2026_09_20_rebase_gross_603.py::_add_gross_row': {
        'unit_conversions.bsn_unit': ('exempt', 'The #603 gross-to-piece rebase (PR #624), dated in its own filename and already run on prod for 1047/1048/1049/1052. The word it inserts is the module constant GROSS = กุรุส, which is the unit map\'s own word for BSN5657 กร; the script asserts that before touching 1187/1188 (scripts/2026_09_20_rebase_gross_603.py:182 calls bsn_units.translate). It refuses a product it has already rebased.'),
    },
    'scripts/2026_09_20_rebase_gross_603.py::_restore_kept_ratios': {
        'unit_conversions.bsn_unit': ('exempt', 'Same #603 one-off. It writes unit_conversions.ratio only (a real โหล is 12 pieces, not the gross RATIO the engine sets); bsn_unit appears in the WHERE clause, never in a SET.'),
    },
    'scripts/2026_09_20_rebase_gross_603.py::main': {
        'products.unit_type': ('exempt', 'Same #603 one-off. It names the product\'s CURRENT base by its true word (GROSS = กุรุส) inside the rebase transaction, so the engine re-denominates กุรุส to the piece unit Put ruled; the piece word itself comes from PLAN, which is Put\'s per-product ruling recorded on issue #603.'),
    },
    'scripts/apply_platform_overview_mapping.py::create_stub_product': {
        'products.unit_type': ('exempt', 'Own docstring: "DEPRECATED: one-off from 2026-05-17. Kept for audit trail. Do not re-run."'),
    },
    'scripts/load_purchase_history_20250529.py::load': {
        'purchase_transactions.unit': ('exempt', 'The original full-history purchase load, dated in its own filename; the ongoing importer is models/imports.py::import_weekly, already covered above.'),
    },
    'scripts/p2p3_split_hinges.py::run': {
        'DYNAMIC-TABLE': ('exempt', 'The historical hinge-family split job named in erp-engineering-discipline.md as the origin of the "Reconciliation Procedure" — already run; dynamic part only reassigns product_id/synced_to_stock.'),
    },
    'scripts/p2p3_split_hinges.py::upsert_mapping_row': {
        'product_code_mapping.bsn_unit': ('exempt', 'Same one-off hinge-family split job as scripts/p2p3_split_hinges.py::run above, already run.'),
    },
    'scripts/phase_c_dedup_replay_20260530.py::main': {
        'purchase_transactions.unit': ('exempt', 'One-off, dated 2026-05-30 (dedup-replay phase C), already run against prod.'),
        'unit_conversions.bsn_unit': ('exempt', 'Same one-off phase-C dedup-replay script, dated 2026-05-30, already run.'),
    },
    'scripts/phase_c_replay_apply_20260530.py::main': {
        'unit_conversions.bsn_unit': ('exempt', 'One-off, dated 2026-05-30 (the "apply" half of the same phase C replay), already run.'),
    },
    'scripts/reimport_2026_04_28/import_credit_notes.py::upsert_credit_notes': {
        'sales_transactions.unit': ('exempt', 'Lives under the dated one-off reimport folder scripts/reimport_2026_04_28/, already run.'),
    },
    'scripts/reimport_2026_04_28/run.py::reset_ledger': {
        'DYNAMIC-TABLE': ('exempt', 'Same dated one-off reimport folder; dynamic part only sets synced_to_stock.'),
    },
    'scripts/reimport_2026_04_28/run.py::sync_table_post_baseline': {
        'DYNAMIC-TABLE': ('exempt', 'Same dated one-off reimport folder; dynamic part only sets synced_to_stock.'),
    },
    'scripts/reimport_2026_04_28/run.py::upsert_table': {
        'DYNAMIC-TABLE': ('exempt', 'Same dated one-off reimport folder; dynamic part only sets synced_to_stock.'),
    },
}

_VALID_STATUS_RE = re.compile(r'^(through_map|through_map_transitive|pending:#\d+|exempt)$')


def _flatten(allowed):
    """{site: frozenset(labels)} view of ALLOWED, for the found-vs-declared
    comparison."""
    return {site: frozenset(labels) for site, labels in allowed.items()}


# ── The coverage tests ───────────────────────────────────────────────────

def test_every_writer_is_declared_with_the_right_labels():
    found = _census()
    declared = _flatten(ALLOWED)
    undeclared = {s: sorted(n) for s, n in found.items() if declared.get(s) != n}
    assert not undeclared, (
        'These sites write a target unit column with a label set not '
        'matching (or missing from) ALLOWED — declare each label as '
        'through_map / through_map_transitive / pending:#NNN / exempt:\n  '
        + '\n  '.join(f'{s}: {v}' for s, v in sorted(undeclared.items())))


def test_no_stale_declarations():
    """The mirror of the test above: an entry ALLOWED still claims but the
    sweep no longer finds (the code changed, or the sweep's own regex
    changed) is worse than a missing one — it hides a surface that may have
    gone raw again, or celebrates a fix that never actually shipped."""
    found = _census()
    declared = _flatten(ALLOWED)
    stale = {s: sorted(n) for s, n in declared.items() if found.get(s) != n}
    assert not stale, (
        'These declared sites no longer match what the sweep finds — '
        'update or remove them:\n  '
        + '\n  '.join(f'{s}: declared {v}, found {sorted(found.get(s, ()))}'
                       for s, v in sorted(stale.items())))


@pytest.mark.parametrize('site', sorted(ALLOWED))
def test_every_label_has_a_valid_status(site):
    for label, (status, _reason) in ALLOWED[site].items():
        assert _VALID_STATUS_RE.match(status), f'{site} [{label}]: bad status {status!r}'


@pytest.mark.parametrize('site', sorted(ALLOWED))
def test_every_label_carries_a_reason(site):
    for label, (_status, reason) in ALLOWED[site].items():
        assert len(reason) > 40, f'{site} [{label}]: explain WHY, in a sentence'


def _site_path(site):
    rel = site.split('::', 1)[0]
    if rel.startswith('scripts/'):
        return os.path.join(SCRIPTS, rel[len('scripts/'):])
    return os.path.join(APP, rel)


@pytest.mark.parametrize('site,label', [
    (s, lbl) for s, labels in ALLOWED.items()
    for lbl, (status, _r) in labels.items() if status == 'through_map'
])
def test_through_map_direct_sites_actually_call_bsn_units(site, label):
    """Self-check for the 'through_map' claim: the function's OWN source —
    not the file, not a sibling function — must call one of bsn_units'
    exported translate/learn functions. Catches a status left stale after a
    refactor moved or deleted the call."""
    path = _site_path(site)
    qualname = site.split('::', 1)[1]
    src = _function_source(path, qualname)
    assert src is not None, f'{site}: function not found (renamed/moved?)'
    assert _BSN_UNITS_CALL_RE.search(_code_only(src)), (
        f'{site} [{label}] is declared through_map but no longer calls bsn_units')


# Review S4: "the status check proves the call is PRESENT, not that its
# result is WRITTEN" — a mutation that keeps calling bsn_units.normalize_unit
# but writes a DIFFERENT (untranslated) value stays green against the check
# above. Tightened for the two sites with a simple, safely-traceable shape
# (`x = bsn_units.normalize_unit(...)` followed directly by `x` reappearing
# inside a conn.execute-shaped call's params, in the SAME function) — see
# the module docstring's "cannot see" section for why the other three
# through_map sites are checked by reading instead: their translated value
# is renamed through an intermediate structure (a dict key, a list
# comprehension + tuple-unpack, or never assigned to a name at all), and a
# naive "does this identifier reappear" check would either miss a real
# mutation or wrongly fail correct code.
_TRANSLATE_ASSIGN_RE = re.compile(
    r'\b(\w+)\s*=\s*bsn_units\.(?:normalize_unit|translate)\s*\(')

_DIRECT_ASSIGN_THROUGH_MAP_SITES = (
    'scripts/import_express.py::_import_sales',
    'vat_book_builder.py::seed_products_from_stmas',
)


def _execute_call_param_sources(src):
    """Source text of the params argument (2nd positional arg) for every
    conn.execute-shaped call anywhere in `src`, ignoring scope — deliberately
    simple: this only needs to answer "does this identifier appear inside
    ANY write call's arguments", not which specific write."""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('execute', 'executemany') and len(node.args) > 1):
            seg = ast.get_source_segment(src, node.args[1])
            if seg:
                out.append(seg)
    return out


@pytest.mark.parametrize('site', _DIRECT_ASSIGN_THROUGH_MAP_SITES)
def test_through_map_translated_value_reaches_a_write(site):
    """The tightened half of the through_map self-check (review S4):
    proves the variable assigned FROM the bsn_units call is not just
    computed but actually passed to a write, not silently swapped for the
    original raw value on the way there — the exact shape S4 demonstrated
    by editing scripts/import_express.py::_import_sales to insert `r.unit`
    where `norm_unit` belonged."""
    assert site in ALLOWED, f'{site}: not declared in ALLOWED at all'
    src = _function_source(_site_path(site), site.split('::', 1)[1])
    assert src is not None, f'{site}: function not found (renamed/moved?)'
    code = _code_only(src)
    translated = set(_TRANSLATE_ASSIGN_RE.findall(code))
    assert translated, f'{site}: no `x = bsn_units.normalize_unit(...)`-style assignment found'
    params = _execute_call_param_sources(src)
    assert any(re.search(rf'\b{re.escape(v)}\b', p) for v in translated for p in params), (
        f'{site}: the value assigned from bsn_units never reaches a write — '
        f'translated identifiers {sorted(translated)} not found in any '
        f'execute() call\'s params')


# ── through_map_transitive: a status, not a home for hope ────────────────
#
# Review S1 found the one entry that used to claim this status
# (save_unit_conversions) was wrong — its caller's pre-translation only
# covers the acronym sub-path, not every path. S6 then asked for the status
# itself to be un-gameable: a later ticket that wants to "clear" a pending
# entry into through_map_transitive must ALSO register a real control test
# here, in the SAME edit, or the census fails outright.

# (site, label) -> the control test function's name proving the caller
# pre-translates on EVERY path. Empty today (zero valid claims) — kept as
# its own dict, not folded into ALLOWED's tuple, so a future entry cannot
# claim the status without registering here too.
THROUGH_MAP_TRANSITIVE_CONTROLS = {}


def _missing_transitive_controls(allowed, control_names, existing):
    """[(site, label, problem)] for every through_map_transitive entry in
    `allowed` that has no entry in `control_names`, or whose named entry in
    `existing` (a name -> object mapping, e.g. `globals()`) is not an actual
    TEST FUNCTION — name starts with `test_` AND is callable. A pure
    function of its three arguments (not tied to the real ALLOWED or this
    module's globals) so the checker can be proven against a synthetic case
    below — the real ALLOWED currently has ZERO through_map_transitive
    entries, which would make a test written directly against it vacuously
    green (verification-discipline.md's empty-collection trap: "an
    assertion over a collection that may be EMPTY pins nothing").

    Review S7: the first version only checked `name in existing_names` (a
    bare set of names) — registering a real, unrelated global (the
    reviewer's example: `'sqlite3'`, this module's own import) satisfied
    it, because a module object IS "in" that set. `startswith('test_')`
    +`callable` narrows this to "names a real pytest test", but even that
    is not "names a test that actually proves THIS claim" — pointing at
    some OTHER unrelated real test (e.g. a whitelist test elsewhere in
    this file) still passes this gate. That residual gap is accepted and
    left to code review of the registering PR, same as any other reason
    field in ALLOWED; this check only rules out the cheapest way to fake
    it, not every way."""
    problems = []
    for site, labels in allowed.items():
        for label, (status, _reason) in labels.items():
            if status != 'through_map_transitive':
                continue
            name = control_names.get((site, label))
            if not name:
                problems.append((site, label, 'no control test named'))
                continue
            obj = existing.get(name)
            if obj is None or not name.startswith('test_') or not callable(obj):
                problems.append(
                    (site, label, f'named control {name!r} is not a real test function'))
    return problems


def test_every_through_map_transitive_entry_names_an_existing_control():
    problems = _missing_transitive_controls(
        ALLOWED, THROUGH_MAP_TRANSITIVE_CONTROLS, globals())
    assert not problems, (
        'through_map_transitive is only valid with a real, existing control '
        'test — a later ticket cannot "clear" a pending entry into this '
        'status without evidence:\n  '
        + '\n  '.join(f'{s} [{l}]: {p}' for s, l, p in problems))


def test_missing_transitive_controls_catches_both_gaps():
    """Break-it-once for `_missing_transitive_controls` itself, against a
    SYNTHETIC allowed/control-map pair (see the empty-collection note
    above for why the real ALLOWED cannot exercise this). Proves the
    checker catches an unregistered entry, a registration naming a
    function that does not exist, AND (review S7) a registration naming a
    REAL global that is not a test function — the exact shape the
    reviewer used to prove the first version was fakeable (`'sqlite3'`,
    imported at the top of this file). Stays silent on a non-transitive
    entry (control), and clears once a real `test_`-named callable is
    given."""
    def a_real_name():
        pass

    fake_allowed = {
        'site_a': {'col': ('through_map_transitive', 'x' * 50)},
        'site_b': {'col': ('through_map_transitive', 'x' * 50)},
        'site_c': {'col': ('through_map_transitive', 'x' * 50)},
        'site_d': {'col': ('through_map', 'x' * 50)},   # control: never flagged
    }
    fake_controls = {
        ('site_a', 'col'): 'this_function_does_not_exist_anywhere',
        # site_b: deliberately left unregistered
        ('site_c', 'col'): 'sqlite3',   # review S7's exact reproduction
    }
    fake_existing = {'test_a_real_name': a_real_name, 'sqlite3': sqlite3}
    problems = _missing_transitive_controls(fake_allowed, fake_controls, fake_existing)
    found = {(s, l) for s, l, _p in problems}
    assert found == {('site_a', 'col'), ('site_b', 'col'), ('site_c', 'col')}, problems

    # CONTROL: naming a REAL test_-prefixed callable clears all three.
    fake_controls_ok = {(s, 'col'): 'test_a_real_name' for s in ('site_a', 'site_b', 'site_c')}
    assert not _missing_transitive_controls(fake_allowed, fake_controls_ok, fake_existing)


def test_code_only_strips_comments_but_keeps_real_calls():
    """Control for the two `_code_only`-gated tests above. A stripper that
    ate everything (or that left a commented-out call looking real) would
    make both of those tests pass for the wrong reason — proven here by
    running `_code_only` on a real bsn_sync.py mutation shape directly,
    without touching the file on disk."""
    real = (
        'def f(pairs, conn):\n'
        '    bsn_units.add_acronym("acr", "full", conn=conn)\n')
    commented_out = (
        'def f(pairs, conn):\n'
        '    pass  # bsn_units.add_acronym("acr", "full", conn=conn) removed\n')
    assert _BSN_UNITS_CALL_RE.search(_code_only(real))
    assert not _BSN_UNITS_CALL_RE.search(_code_only(commented_out)), (
        'a call named only in a comment must not count as calling bsn_units')
    # the strip must not have eaten the CODE beside the comment either
    assert 'pass' in _code_only(commented_out)


def test_code_only_separates_adjacent_word_tokens():
    """Found by hand while checking review N4 (`_BSN_UNITS_CALL_RE` must NOT
    match `is_known`/`load_unit_map`): a synthetic `return
    bsn_units.normalize_unit(...)` came back UNMATCHED even though the call
    is real, because plain `''.join` glued "return" and "bsn_units" into
    one word with no boundary between them. None of today's 5 real
    through_map sites happen to write the call this way (see `_code_only`'s
    own docstring for which shapes they use instead), so this never bit a
    real assertion — but the NEXT through_map site easily could look like
    this. Fixed by inserting a space between two word-adjacent pieces;
    proven here directly, and the second half is the control that the fix
    did not reopen the ORIGINAL `\\n`.join gap (a dotted call staying
    glued through a non-word `.`)."""
    after_keyword = 'def f(conn, u):\n    return bsn_units.normalize_unit(u, conn=conn)\n'
    assert _BSN_UNITS_CALL_RE.search(_code_only(after_keyword)), (
        'a bsn_units call immediately after a bare keyword must still be seen')
    # CONTROL: the dotted call itself must still be intact (not re-split by
    # the space-insertion fix) — this is exactly what plain '' .join was
    # added to fix in the first place.
    assert 'bsn_units.normalize_unit' in _code_only(after_keyword)


def test_naming_cascade_whitelist_still_excludes_unit_type():
    from naming_cascade import _EDITABLE_TEXT
    assert 'unit_type' not in _EDITABLE_TEXT


def test_apply_normalize_round1_whitelist_still_excludes_unit_type():
    src = open(os.path.join(SCRIPTS, 'apply_normalize_round1.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    fields = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'APPLY_FIELDS' for t in node.targets):
            fields = ast.literal_eval(node.value)
    assert fields is not None, 'APPLY_FIELDS not found — did it get renamed?'
    db_cols = {pair[1] for pair in fields}
    assert 'unit_type' not in db_cols


def test_apply_product_naming_whitelist_still_excludes_unit_type():
    src = open(os.path.join(SCRIPTS, 'apply_product_naming.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    whitelist = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == '_FIELD_WHITELIST' for t in node.targets):
            whitelist = ast.literal_eval(node.value)
    assert whitelist is not None, '_FIELD_WHITELIST not found — did it get renamed?'
    assert 'unit_type' not in whitelist


def test_supplier_product_mapping_unit_columns_still_have_no_writer():
    """Documentation-only exempt entry (supplier_product_mapping.{supplier_
    unit,erp_unit} has no ALLOWED entry above because nothing writes it).
    Positive control: prove the sweep still finds ZERO hits for that table,
    so if a writer is ever added it surfaces via the main coverage test
    above instead of silently matching a "no writer" claim that stopped
    being true."""
    found = _census()
    hits = {s: v for s, v in found.items()
            if any(lbl.startswith('supplier_product_mapping.') for lbl in v)}
    assert not hits, f'supplier_product_mapping now has a writer: {hits}'


def _schema_sql_conn():
    """A fresh in-memory sqlite3 connection built from data/schema.sql —
    the tracked file a fresh `git clone` + first boot actually builds from
    (database.py::init_db), always present, unlike the live dev DB (absent
    in a fresh worktree or CI). Caller owns closing it."""
    schema_path = os.path.join(_ROOT, 'data', 'schema.sql')
    conn = sqlite3.connect(':memory:')
    with open(schema_path, encoding='utf-8') as f:
        conn.executescript(f.read())
    return conn


def test_transactions_unit_mode_is_a_documented_exclusion_not_an_oversight():
    """transactions.unit_mode is deliberately NOT in TARGET_COLUMNS (it is a
    CHECK-constrained unit/box/carton SCALE selector, never an Express
    code). Control: the column still exists and is still that same
    3-value enum, so the exclusion is describing real, current schema —
    not a stale claim about a column that has since changed shape.

    Review N7: reads data/schema.sql (via `_schema_sql_conn`, the same
    source `_schema_sql_columns` below uses for the N1 guard) instead of
    the live dev DB — the previous version skipped in a fresh worktree or
    CI where that file does not exist."""
    conn = _schema_sql_conn()
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "CHECK(unit_mode IN ('unit','box','carton'))" in sql


def _schema_sql_columns():
    """{table: [column, ...]} for the WHOLE schema, built from
    data/schema.sql (review N1) — see `_schema_sql_conn`."""
    conn = _schema_sql_conn()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
                for t in tables}
    finally:
        conn.close()


# Every (table, column) confirmed by READING (never guessed from the name)
# to hold something other than a หน่วย spelling — the exact set behind the
# module docstring's "look-alikes" bullets, kept here as data so
# test_target_columns_covers_every_unit_ish_column_in_schema_sql can check
# it mechanically instead of the docstring being the only place this is
# asserted (review N1).
_EXCLUDED_LOOKALIKE_COLUMNS = {
    ('commission_overrides', 'fixed_per_unit'),   # money: a commission rate
    ('conversion_cost_log', 'unit_cost'),         # money
    ('credit_note_imports', 'unit_price'),        # money
    ('express_credit_note_lines', 'unit_price'),  # money
    ('express_sales', 'unit_price'),              # money
    ('express_sales_order_lines', 'unit_price'),  # money
    ('marketplace_order_items', 'unit_price'),    # money
    ('pending_product_suggestions', 'unit_conversion_ratio'),  # a ratio number
    ('pending_product_suggestions', 'units_per_box'),     # pack-size count
    ('pending_product_suggestions', 'units_per_carton'),  # pack-size count
    ('platform_stock_deductions', 'units'),       # pack-size count
    ('product_cost_ledger', 'unit_cost'),         # money
    ('products', 'units_per_box'),                # pack-size count
    ('products', 'units_per_carton'),             # pack-size count
    ('purchase_order_lines', 'unit_price'),       # money
    ('purchase_transactions', 'unit_price'),      # money
    ('sales_transactions', 'unit_price'),         # money
    ('transactions', 'unit_mode'),   # CHECK-enum unit/box/carton, not an Express code
}


def test_target_columns_covers_every_unit_ish_column_in_schema_sql():
    """N1: guard TARGET_COLUMNS (+ the exclusion set above) against
    data/schema.sql itself, so a NEW column with "unit" in its name added
    to any table later — one this census has never seen — fails the suite
    instead of silently going unswept forever. `migration_*` snapshot
    tables are skipped: they are frozen one-time artifacts written by a
    .sql migration, not a live table schema.sql would ever carry (already
    documented as out of scope; verified empty today by the assert below)."""
    cols = _schema_sql_columns()
    migration_snapshot_tables = {t for t in cols if t.startswith('migration_')}
    assert not migration_snapshot_tables, (
        'schema.sql now carries a migration snapshot table — re-check '
        'whether it needs its own exemption: ' + str(migration_snapshot_tables))
    declared = {(t, c) for t, cs in TARGET_COLUMNS.items() for c in cs}
    unaccounted = []
    for table, colnames in cols.items():
        for c in colnames:
            if 'unit' not in c.lower() and c.lower() != 'qty_label':
                continue
            if (table, c) in declared or (table, c) in _EXCLUDED_LOOKALIKE_COLUMNS:
                continue
            unaccounted.append(f'{table}.{c}')
    assert not unaccounted, (
        'schema.sql has unit-ish column(s) neither in TARGET_COLUMNS nor '
        'documented in _EXCLUDED_LOOKALIKE_COLUMNS:\n  ' + '\n  '.join(unaccounted))


def test_excluded_lookalike_columns_still_exist_in_schema_sql():
    """Mirror of the test above: a stale exclusion (the column was renamed
    or dropped) is invisible to the check above, since it only complains
    about UNACCOUNTED columns — it would happily let a typo'd or removed
    entry sit here forever looking like coverage."""
    cols = _schema_sql_columns()
    stale = [f'{t}.{c}' for t, c in _EXCLUDED_LOOKALIKE_COLUMNS if c not in cols.get(t, ())]
    assert not stale, f'stale entries in _EXCLUDED_LOOKALIKE_COLUMNS: {stale}'


def test_the_census_survives_its_own_success():
    """AC: 'It still passes once no pending entries remain.' Simulate the
    post-#599/#601/#602/#610 world by flipping every pending:#NNN status to
    through_map_transitive (a status this file already treats as valid and
    reason-bearing, without requiring a matching bsn_units call — a real
    flip to through_map would additionally need its own self-check to pass,
    which is a per-site code fact this synthetic copy cannot manufacture).
    The structural validators — status is one of the four values, every
    entry still carries its reason — must keep passing on that copy,
    proving they do not secretly depend on a pending entry existing."""
    simulated = {
        site: {label: (('through_map_transitive' if status.startswith('pending:')
                        else status), reason)
               for label, (status, reason) in labels.items()}
        for site, labels in ALLOWED.items()
    }
    assert not any(status.startswith('pending:')
                   for labels in simulated.values()
                   for status, _r in labels.values()), 'simulation left a pending status behind'
    for site, labels in simulated.items():
        for label, (status, reason) in labels.items():
            assert _VALID_STATUS_RE.match(status), f'{site} [{label}]: {status!r}'
            assert len(reason) > 40, f'{site} [{label}]: reason too short'


# ── Break-it-once: one rogue snippet per required SQL shape ─────────────
#
# Each shape must make `_hits_in_src` see a write it would otherwise miss.
# Synthetic in-memory snippets, matching the shape-coverage style already
# established by test_revenue_filter_coverage.py's AGGREGATE_SHAPES and
# test_last_purchase_population_coverage.py's AGGREGATE_SHAPES/HELPER_SHAPES
# — those tests prove sweep COVERAGE the same way, against snippets, not by
# mutating real files and reverting. The scripts/-file shape below is the
# one exception: it uses a REAL file in a throwaway temp tree, because
# "a file under scripts/" is a discovery-mechanism claim (does the WALKER
# see a new file), not a regex-shape claim.

SHAPES = {
    'table_alias':
        # UPDATE via an alias-qualified column reference.
        'def f(conn, pid, u):\n'
        '    conn.execute("UPDATE unit_conversions uc SET uc.bsn_unit = ? '
        'WHERE uc.product_id = ?", (u, pid))\n',
    'plus_concat':
        'def f(conn, pid, u, r):\n'
        '    conn.execute("INSERT INTO unit_conversions "\n'
        '                 "(product_id, bsn_unit, ratio) " +\n'
        '                 "VALUES (?, ?, ?)", (pid, u, r))\n',
    'dot_format':
        'def f(conn, pid, u):\n'
        '    conn.execute("UPDATE products SET unit_type = {0} WHERE id = {1}"'
        '.format("?", "?"), (u, pid))\n',
    'dynamic_table_fstring':
        'def f(conn, table, pid, u, r):\n'
        '    conn.execute(f"INSERT INTO {table} (product_id, bsn_unit, ratio) "\n'
        '                 f"VALUES (?, ?, ?)", (pid, u, r))\n',
    'update_or_replace':
        'def f(conn, item_id, u):\n'
        '    conn.execute("UPDATE OR REPLACE supplier_catalogue_items "\n'
        '                 "SET unit = ? WHERE id = ?", (u, item_id))\n',
    'insert_select':
        'def f(conn, pid, u):\n'
        '    conn.execute("INSERT INTO product_price_tiers "\n'
        '                 "(product_id, qty_label, price) "\n'
        '                 "SELECT ?, ?, price FROM product_price_tiers '
        'WHERE product_id = ?", (pid, u, pid))\n',
    'executemany':
        'def f(conn, rows):\n'
        '    conn.executemany("INSERT INTO express_sales_order_lines "\n'
        '                     "(so_no, unit) VALUES (?, ?)", rows)\n',
    # ── review S5: 5 more shapes the sweep was blind to ──────────────────
    'dynamic_column_list':
        # literal table, but the COLUMN LIST is a hole — different from
        # dynamic_table_fstring above, where the TABLE ITSELF is the hole.
        'def f(conn, cols, pid, u, price):\n'
        '    conn.execute(f"INSERT INTO product_price_tiers ({cols}) "\n'
        '                 f"VALUES (?, ?, ?)", (pid, u, price))\n',
    'percent_format_table':
        'def f(conn, table, item_id, u):\n'
        '    conn.execute("UPDATE %s SET unit = ? WHERE id = ?" % table, (u, item_id))\n',
    'plus_concat_variable_table':
        # unlike plus_concat above (two STRING literals), one side here is
        # a bare variable — _render used to return None for the WHOLE
        # expression the moment any `+` operand failed to render as a
        # string, hiding this from the sweep entirely.
        'def f(conn, table, item_id, u):\n'
        '    conn.execute("UPDATE " + table + " SET unit = ? WHERE id = ?", (u, item_id))\n',
    'bare_replace_into':
        # SQLite shorthand for INSERT OR REPLACE INTO — no leading INSERT.
        'def f(conn, item_id, u):\n'
        '    conn.execute("REPLACE INTO supplier_catalogue_items "\n'
        '                 "(id, unit) VALUES (?, ?)", (item_id, u))\n',
    'aliased_dynamic_set':
        'def f(conn, set_clause, pid, u):\n'
        '    conn.execute(f"UPDATE products AS p SET {set_clause} WHERE p.id = ?",\n'
        '                 (u, pid))\n',
}

NOT_SHAPES = {
    'a bare select':
        'def f(conn, pid):\n'
        '    return conn.execute("SELECT unit_type FROM products WHERE id=?", (pid,))\n',
    'updated_at is not UPDATE':
        'def f(conn, pid):\n'
        '    return conn.execute("SELECT updated_at, unit_type FROM products '
        'WHERE id=?", (pid,))\n',
    'unrelated table, unrelated column':
        'def f(conn, pid, v):\n'
        '    conn.execute("UPDATE customers SET note = ? WHERE code = ?", (v, pid))\n',
    'prose mentioning a target column':
        'def f():\n'
        '    """This function does NOT write unit_type or bsn_unit."""\n'
        '    return 1\n',
    'plain arithmetic is not a string':
        # review S4/S5's `_render` fix (placeholder-substitute a `+`
        # operand that fails to render) must not start treating ordinary
        # non-string arithmetic as a candidate query string — proven by
        # the ABSENCE of a crash/false-positive here, not by a value.
        'def f(a, b):\n'
        '    total = a + b\n'
        '    return total\n',
    'percent_format_no_string':
        # a bare `%` with no string template on the left must not render.
        'def f(a, b):\n'
        '    return a % b\n',
}


@pytest.mark.parametrize('shape', sorted(SHAPES))
def test_the_sweep_sees_every_required_shape(shape):
    hits = _hits_in_src(SHAPES[shape])
    assert hits.get('f'), f'{shape}: the sweep is blind to this shape'


@pytest.mark.parametrize('shape', sorted(NOT_SHAPES))
def test_the_sweep_ignores_what_is_not_a_target_write(shape):
    hits = _hits_in_src(NOT_SHAPES[shape])
    assert not hits.get('f'), f'{shape}: false positive'


def test_a_new_file_under_scripts_is_discovered_and_must_be_declared(tmp_path):
    """The 8th required shape: 'a file under scripts/'. Uses a REAL file on
    a throwaway tree (not a synthetic snippet) because this is a claim
    about the WALKER (_py_files/_census), not about the regex — a rogue
    script dropped into the real scripts/ directory must be picked up by
    the same enumeration the coverage test above runs, with nothing
    special-cased about scripts/ vs inventory_app/.

    Break-it-once, for real: (1) an empty scripts/ tree finds nothing: RED
    would mean the walker is broken even absent a rogue file. (2) adding
    the rogue file makes it appear: RED here would mean the walker cannot
    see a new file at all — re-read the created file to confirm the
    mutation landed before trusting either result. (3) removing it again
    makes it disappear — proves the census does not cache anything."""
    scripts_dir = tmp_path / 'scripts'
    scripts_dir.mkdir()
    app_dir = tmp_path / 'inventory_app'
    app_dir.mkdir()

    baseline = _census(app_dir=str(app_dir), scripts_dir=str(scripts_dir))
    assert baseline == {}, 'an empty throwaway tree must find nothing (sanity)'

    rogue = scripts_dir / 'rogue_new_importer.py'
    rogue.write_text(
        'def write_it(conn, pid, u):\n'
        '    conn.execute("UPDATE products SET unit_type = ? WHERE id = ?", '
        '(u, pid))\n',
        encoding='utf-8')
    landed = rogue.read_text(encoding='utf-8')
    assert 'UPDATE products SET unit_type' in landed, 'mutation did not land on disk'

    with_rogue = _census(app_dir=str(app_dir), scripts_dir=str(scripts_dir))
    assert with_rogue == {'scripts/rogue_new_importer.py::write_it':
                          frozenset({'products.unit_type'})}, (
        'the walker did not see the new scripts/ file — census would '
        'silently miss a real new writer')

    rogue.unlink()
    after_removal = _census(app_dir=str(app_dir), scripts_dir=str(scripts_dir))
    assert after_removal == {}, 'stale result after the rogue file was removed'
