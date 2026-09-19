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
#599 and #600 change the map's SEED DATA and run a one-time historical
relabel migration respectively — neither touches a call site, so this
census has no entries for them; nothing here should change when they ship.

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
     vs `pending` vs `exempt` — several sites (`save_unit_conversions`,
     `upsert_unit_conversion`) have byte-identical SQL and differ only in
     whether their CALLER pre-translates, which no sweep can see; that
     distinction is recorded as prose in the reason field, and the one
     transitive claim is pinned by its own positive-control test below.

What this census CANNOT see (say so up front, per #598's own AC):
  - a SQL string built from variables the sweep does not track (e.g. a
    column value assembled far from the query text);
  - a dynamic `SET {clause}`/`INSERT INTO {table}` where the actual columns
    or table are computed at import time from something other than a
    literal in the SAME rendered string — the sweep only proves "this MIGHT
    touch a unit column"; whether it actually can is decided by reading the
    whitelist the dynamic clause draws from (documented per exempt entry);
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
_WRITE_VERB_RE = re.compile(
    r'\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?)\b', re.I)
# A write verb immediately followed by `{` — the table name is an f-string
# hole (`express_registers.py::replace`'s `INSERT INTO {table.name}`,
# `learn_acronyms_normalize`'s `UPDATE {t} SET unit=...`).
_DYNAMIC_TABLE_RE = re.compile(
    r'\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?)\s*\{', re.I)
# `UPDATE <literal table> SET {clause}` — the SET clause (not the table) is
# the hole. Captured so the caller can check the table is one we care about;
# an unrelated dynamic-SET table (ar_followup_log, customers, leave_requests,
# label_company_block, customer_contact_review, a CRM upsert) is real code
# but out of scope and must not appear here.
_DYNAMIC_SET_RE = re.compile(r'\bUPDATE\s+(\w+)\s+SET\s*\{', re.I)
_BSN_UNITS_CALL_RE = re.compile(
    r'\bbsn_units\.(?:normalize_unit|translate|learn|add_acronym|is_known'
    r'|load_unit_map)\b')


def _code_only(src):
    """`src` with comments and docstrings removed — a guard call named only
    in a comment (e.g. `pass  # bsn_units.add_acronym(...) removed`) is not
    a guard. Tokenize-based, like test_revenue_filter_coverage.py's
    `_code_only` — but joined with NO separator, not `\\n`: that file's
    GUARD_TOKENS are single identifiers, so `\\n`.join (one token per line)
    still leaves each one findable as a substring. This file's checks are
    DOTTED calls (`bsn_units.add_acronym`, `acr_full.get(`) spanning THREE
    tokens (NAME, OP '.', NAME) — `\\n`.join was proven to break adjacency
    between them (every through_map self-check went red against unmutated
    code the first time this ran), so plain concatenation is what actually
    reconstructs a dotted name. Proven correct, not assumed, by
    test_code_only_strips_comments_but_keeps_real_calls below."""
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
    return ''.join(out)


def _render(node):
    """Source text of a string expression. f-string holes are kept as
    `{expr}` (so a dynamic table/column name shows up as a literal brace for
    the DYNAMIC detectors, never as if it were resolved); `.format()` is
    treated as a passthrough of its template (the call's own substitution
    args are not more query text). Mirrors
    test_last_purchase_population_coverage.py's `_render`, plus `.format()`.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                try:
                    parts.append('{' + ast.unparse(v.value) + '}')
                except Exception:
                    parts.append('{?}')
        return ''.join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is not None and right is not None:
            return left + right
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
    (the table name itself is a hole), or `DYNAMIC-SET:<table>` (a literal,
    in-scope table with a dynamically-built SET clause).

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
        'DYNAMIC-TABLE': ('through_map',
            'Calls bsn_units.normalize_unit twice (matching an incoming '
            'bsn_unit against the ledger before repointing) before its '
            'dynamic per-table UPDATE.'),
        'product_code_mapping.bsn_unit': ('exempt',
            'The bsn_unit written here is copied VERBATIM from an existing '
            'product_code_mapping row fetched earlier in the same '
            'function (line ~618-634) when repointing a code to a '
            'different product — it introduces no new spelling, only '
            'relocates one that is already stored.'),
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

    # ── through_map_transitive: caller pre-translates; see the named
    # positive-control test below ──────────────────────────────────────
    'models/bsn_sync.py::save_unit_conversions': {
        'unit_conversions.bsn_unit': ('through_map_transitive',
            'This function does not call bsn_units itself, but its ONLY '
            'caller — blueprints/bsn.py::unit_conversions_save — runs '
            'Pass 1 (models.learn_acronyms_normalize, which teaches the '
            'map any acronym Put just typed) BEFORE Pass 2 builds the '
            '`items` list this function receives, and Pass 2 substitutes '
            '`acr_full.get((pid, acronym), acronym)` so every bsn_unit in '
            '`items` is already a canonical word by the time this runs. '
            'Pinned by '
            'test_unit_conversions_save_caller_pretranslates_before_calling.'),
    },

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
            'with NO Pass-1/Pass-2 pre-translation step (unlike its '
            'sibling unit_conversions_save above). Safe today only '
            'because the client value already originated from an '
            'already-normalised sales_transactions.unit via '
            'bsn_suggest.py — an implicit, not enforced, invariant.'),
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
        'unit_conversions.bsn_unit': ('exempt', 'One-off, dated + ticketed #592, already run.'),
    },
    'scripts/2026_09_19_gross_to_piece.py::rebase': {
        'products.unit_type': ('exempt', 'One-off, dated 2026-09-19 (the 1050/1320 gross-to-piece rebase), already run against prod.'),
        'unit_conversions.bsn_unit': ('exempt', 'Same one-off gross-to-piece rebase script, dated 2026-09-19, already run.'),
    },
    'scripts/2026_09_19_rebase_689_767.py::apply_tiers': {
        'product_price_tiers.qty_label': ('exempt', 'One-off, dated 2026-09-19 (pid 689/767 rebase), already run; writes hardcoded literals only.'),
    },
    'scripts/2026_09_19_split_belco_582.py::split': {
        'unit_conversions.bsn_unit': ('exempt', 'One-off, dated + ticketed #582, already run.'),
    },
    'scripts/apply_decision_ratios.py::main': {
        'unit_conversions.bsn_unit': ('exempt', 'One-off decision-application script (no date in name, but its own docstring scopes it to one specific ratio-decision batch), already run.'),
    },
    'scripts/apply_decision_remaps.py::main': {
        'DYNAMIC-TABLE': ('exempt', 'One-off "Bucket C+E" remap script (own docstring names the specific decision batch), already run; the dynamic part only reassigns product_id.'),
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


def test_unit_conversions_save_caller_pretranslates_before_calling():
    """Positive control for the ONE through_map_transitive claim
    (models/bsn_sync.py::save_unit_conversions). The guarantee lives in the
    CALLER, blueprints/bsn.py::unit_conversions_save — assert its two-pass
    shape (learn first, substitute before building `items`) is still there,
    so the transitive claim cannot silently rot when nobody is looking at
    save_unit_conversions itself. Checked against `_code_only`, not the raw
    source — the first draft of this test named the call in its own prose
    ("assert 'models.learn_acronyms_normalize(learned)' in caller") and
    stayed GREEN when the real call was replaced by `pass  # ... removed`,
    because the commented-out line still contained the literal text."""
    caller = _function_source(os.path.join(APP, 'blueprints', 'bsn.py'),
                              'unit_conversions_save')
    assert caller is not None, 'blueprints/bsn.py::unit_conversions_save not found'
    code = _code_only(caller)
    assert 'models.learn_acronyms_normalize(learned)' in code, (
        'unit_conversions_save no longer teaches the map before saving — '
        'the through_map_transitive claim on save_unit_conversions no '
        'longer holds')
    assert 'acr_full.get(' in code, (
        'unit_conversions_save no longer substitutes the learned full word '
        'before building the items list passed to save_unit_conversions')
    # ORDER matters: the substitution must read from a dict populated by
    # the SAME learn step, not a stale one — a crude but real proxy is that
    # the learn call's line number precedes the substitution's.
    learn_at = code.index('models.learn_acronyms_normalize(learned)')
    sub_at = code.index('acr_full.get(')
    assert learn_at < sub_at, (
        'unit_conversions_save now substitutes BEFORE learning — an item '
        'could reach save_unit_conversions holding the raw acronym instead '
        'of the word Pass 1 just taught the map')


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


def test_transactions_unit_mode_is_a_documented_exclusion_not_an_oversight():
    """transactions.unit_mode is deliberately NOT in TARGET_COLUMNS (it is a
    CHECK-constrained unit/box/carton SCALE selector, never an Express
    code). Control: the column still exists and is still that same
    3-value enum, so the exclusion is describing real, current schema —
    not a stale claim about a column that has since changed shape."""
    import sqlite3
    db = os.path.join(APP, 'instance', 'inventory.db')
    if not os.path.exists(db):
        pytest.skip('no local dev DB to introspect')
    conn = sqlite3.connect(db)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "CHECK(unit_mode IN ('unit','box','carton'))" in sql


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
