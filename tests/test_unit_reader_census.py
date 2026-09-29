"""Census: every direct read of `unit_conversions` outside unit_conversion.py
is a writer, PR2's, or exempt with a reason.

Card A (ADR 0019) gave the "how many base units is one of this unit" question
one home, `inventory_app/unit_conversion.py`, with a named entry point per
reader family. That holds only while nothing new reads the table on its own.
This sweep finds every string in `inventory_app/` and `scripts/` whose SQL
selects or joins `unit_conversions` (a `DELETE FROM` is a write, not a read)
and demands a declaration for each function holding one. Same shape as
tests/test_unit_writer_census.py, whose SQL renderer and file walker it
reuses.

⚠ What this cannot see: a table name assembled at runtime (`FROM {table}`),
and a reader that asks a helper instead of writing SQL. A new reader of the
second kind already goes through someone's code; the first is judged by hand.
"""
import os
import re

import pytest

from tests.test_unit_writer_census import APP, SCRIPTS, _py_files, _queries

_READ_RE = re.compile(r'\b(?:FROM|JOIN)\s+unit_conversions\b', re.I)
_DELETE_RE = re.compile(r'\bDELETE\s+FROM\s+unit_conversions\b', re.I)

HOME = 'unit_conversion.py'


def _reads_in_src(src):
    """{qualname} of every function whose SQL reads unit_conversions."""
    return {func for func, sql in _queries(src) if _READ_RE.search(_DELETE_RE.sub('', sql))}


def _census(app_dir=APP, scripts_dir=SCRIPTS):
    found = set()
    for rel, path in _py_files(app_dir, scripts_dir):
        if rel == HOME:
            continue
        with open(path, encoding='utf-8') as f:
            src = f.read()
        try:
            funcs = _reads_in_src(src)
        except SyntaxError:
            continue
        found.update(f'{rel}::{func}' for func in funcs)
    return found


_WRITER = ('Reads unit_conversions to decide or build its own write to that table '
           '(declared in tests/test_unit_writer_census.py). A writer, not a '
           'reader that converts a quantity.')
_ONE_OFF = ('A dated one-off data fix, already run on prod and kept for its audit '
            'trail and its tests. This read snapshots, checks preconditions or '
            'asserts invariants around the script\'s own write; no screen or ledger '
            'consumes it, and historical scripts are not edited.')
_PREFLIGHT = ('Migration preflight on a backup copy: reproduces the migration\'s own '
              'twin-ratio joins to report what it would change. It mirrors migration '
              'SQL, it is not a runtime reader.')
_PR2 = ('The exact family on the stock side (raw spelling, no map). It moves to '
        'unit_conversion in PR2 of card A, with the stock replay as its gate.')

# site -> (status, reason). status: 'writer' | 'pending:PR2' | 'exempt'.
ALLOWED = {
    # ── inventory_app ─────────────────────────────────────────────────────
    'models/bsn_sync.py::_get_base_qty': ('pending:PR2', _PR2 + ' The stock writer itself: '
                                          'strip compare, 4-dp round, None holds the line.'),
    'models/mapping.py::missing_unit_ratios': ('pending:PR2', _PR2 + ' Predicts which units '
                                               'the stock writer would hold after a remap.'),
    'review_rules.py::_get_ratio': ('pending:PR2', _PR2 + ' R4 predicts the stock writer '
                                    '("สต๊อกจะไม่ตัด") and writes txn_review_flags.'),
    'models/bsn_sync.py::add_catalogue_unit_conversion': ('writer', _WRITER),
    'models/bsn_sync.py::get_pending_unit_conversions': (
        'exempt',
        'The /unit-conversions pending list: a NOT EXISTS predicate for held lines '
        'that still need a ratio. It answers a set, not a ratio, and must change in '
        'lockstep with conversion_unit_key and _get_base_qty (the Q11 follow-up), '
        'never on its own.'),
    'models/bsn_sync.py::get_all_unit_conversions': (
        'exempt',
        'The admin list on /unit-conversions: every stored row, shown for editing. '
        'It converts nothing.'),
    'price_lookup.py::_known_ratio_units': (
        'exempt',
        'Names the units a product can answer, for the resolver\'s strict ValueError '
        'text. Reads DISTINCT bsn_unit, never a ratio.'),

    # ── scripts: writers ──────────────────────────────────────────────────
    'scripts/2026_09_19_gross_to_piece.py::rebase': ('writer', _WRITER),
    'scripts/2026_09_29_rebase_meta_nails_657.py::main': ('writer', _WRITER),
    'scripts/apply_decision_ratios.py::main': ('writer', _WRITER),
    'scripts/apply_decision_remaps.py::main': ('writer', _WRITER),
    'scripts/apply_worksheets_20260530.py::main': ('writer', _WRITER),
    'scripts/cleanup_split_mapping_stubs.py::cleanup_pair': ('writer', _WRITER),
    'scripts/merge_product.py::main': ('writer', _WRITER),
    'scripts/p0_split_3p5in.py::run': ('writer', _WRITER),
    'scripts/p2p3_split_hinges.py::run': ('writer', _WRITER),
    'scripts/phase_c_dedup_replay_20260530.py::main': ('writer', _WRITER),
    'scripts/phase_c_replay_apply_20260530.py::main': ('writer', _WRITER),

    # ── scripts: dated one-offs ───────────────────────────────────────────
    'scripts/2026_09_19_fix_pack_ratios_592.py::assert_invariants': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_fix_pack_ratios_592.py::fingerprint_others': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_fix_pack_ratios_592.py::main': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_fix_pack_ratios_592.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_fix_pack_ratios_592.py::snapshot': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_gross_to_piece.py::assert_invariants': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_gross_to_piece.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_rebase_689_767.py::assert_invariants': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_rebase_689_767.py::fingerprint_others': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_rebase_689_767.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_19_split_belco_582.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_20_rebase_gross_603.py::_print_table': ('exempt', _ONE_OFF),
    'scripts/2026_09_20_rebase_gross_603.py::assert_invariants': ('exempt', _ONE_OFF),
    'scripts/2026_09_20_rebase_gross_603.py::fingerprint_others': ('exempt', _ONE_OFF),
    'scripts/2026_09_20_rebase_gross_603.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_21_fix_rr6700253_unit_1658.py::fingerprint_others': ('exempt', _ONE_OFF),
    'scripts/2026_09_21_fix_rr6700253_unit_1658.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_22_relabel_history_600.py::fingerprint': ('exempt', _ONE_OFF),
    'scripts/2026_09_22_sync_436_ch3.py::fingerprint': ('exempt', _ONE_OFF),
    'scripts/2026_09_22_sync_436_ch3.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_25_merge_gross_twins_641.py::duplicates': ('exempt', _ONE_OFF),
    'scripts/2026_09_25_merge_gross_twins_641.py::left_behind': ('exempt', _ONE_OFF),
    'scripts/2026_09_25_merge_gross_twins_641.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_25_merge_gross_twins_641.py::snapshot': ('exempt', _ONE_OFF),
    'scripts/2026_09_28_rebase_dozen_649.py::_print_table': ('exempt', _ONE_OFF),
    'scripts/2026_09_28_rebase_dozen_649.py::assert_invariants': ('exempt', _ONE_OFF),
    'scripts/2026_09_28_rebase_dozen_649.py::fingerprint_others': ('exempt', _ONE_OFF),
    'scripts/2026_09_28_rebase_dozen_649.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_glove_pair_word.py::_print_table': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_glove_pair_word.py::assert_invariants': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_glove_pair_word.py::fingerprint_others': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_glove_pair_word.py::main': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_glove_pair_word.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_rebase_gloves_650.py::_print_table': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_rebase_gloves_650.py::assert_invariants': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_rebase_gloves_650.py::fingerprint_others': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_rebase_gloves_650.py::preconditions': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_rebase_meta_nails_657.py::_conv': ('exempt', _ONE_OFF),
    'scripts/2026_09_29_rebase_meta_nails_657.py::other_products_fingerprint': ('exempt', _ONE_OFF),
    'scripts/cleanup_split_mapping_stubs.py::_check_existing_ratio': (
        'exempt',
        'DEPRECATED one-off (split-mapping stubs, 2026-05): checks a sibling\'s '
        'existing row before its own merge write in cleanup_pair.'),
    'scripts/ledger_dryrun_20260530.py::_get_base_qty': (
        'exempt',
        'A dated 2026-05-30 dry-run replay on a /tmp copy of the DB, with its own '
        'historical copy of the stock lookup. It runs against no live DB.'),
    'scripts/reimport_2026_04_28/run.py::get_base_qty': (
        'exempt',
        'DEPRECATED one-off (2026-04-28) kept for its audit trail, with its own '
        'historical copy of the stock lookup. Never re-run.'),

    # ── scripts: preflights, reports and review sheets ────────────────────
    'scripts/preflight_186_unit_code_cleanup.py::<module>': ('exempt', _PREFLIGHT),
    'scripts/preflight_193_unit_vocabulary.py::<module>': ('exempt', _PREFLIGHT),
    'scripts/preflight_193_unit_vocabulary.py::_run_on_copy': ('exempt', _PREFLIGHT),
    'scripts/bsn_completeness_report.py::<module>': (
        'exempt',
        'An off-app review report joining mapping, conversions, ledger and stock '
        'into one row per product. It counts rows; no ratio reaches a screen or a '
        'ledger.'),
    'scripts/clean_orphan_unit_conversions.py::<module>': (
        'exempt',
        'Selects orphan rows for its own DELETE (a product with no active mapping '
        'and a unit no ledger line uses). It converts nothing.'),
    'scripts/export_product_query.py::main': (
        'exempt',
        'An off-app xlsx export listing each (product, bsn_code) pair with its '
        'stored ratio as a column for a human. It converts nothing.'),
    'scripts/normalize_base_price.py::load_answer_key': (
        'exempt',
        'Offline catalogue-CSV normaliser: loads the ratios read-only into an answer '
        'key for a pricing file Put reviews before import_catalog_pricing runs. Its '
        'canonical fallback ratios are pricing-file policy, not a conversion rule.'),
    'scripts/stock_reconciliation_report.py::main': (
        'exempt',
        'An off-app report reconciling the opening-count CSV against the DB, with '
        'its own truncated-unit matching. Its output is read by a human, never by a '
        'screen or a ledger.'),
    'scripts/suggest_unit_conversion_ratios.py::main': (
        'exempt',
        'An off-app hint sheet listing each product\'s current ratio beside a '
        'price-implied one for Put to eyeball. It converts nothing.'),
}

_VALID_STATUS = {'writer', 'pending:PR2', 'exempt'}


def test_every_reader_is_declared():
    undeclared = sorted(_census() - set(ALLOWED))
    assert not undeclared, (
        'These functions read unit_conversions directly. Read through '
        'inventory_app/unit_conversion.py, or declare them in ALLOWED with a '
        'reason:\n  ' + '\n  '.join(undeclared))


def test_no_stale_declarations():
    stale = sorted(set(ALLOWED) - _census())
    assert not stale, 'declared but no longer found, remove them:\n  ' + '\n  '.join(stale)


@pytest.mark.parametrize('site', sorted(ALLOWED))
def test_every_declaration_has_a_status_and_a_reason(site):
    status, reason = ALLOWED[site]
    assert status in _VALID_STATUS, f'{site}: {status!r}'
    assert len(reason) > 40, f'{site}: reason too short'


def test_the_home_module_is_where_the_readers_went():
    """Control: the sweep does see reads, and the ones it skips live in the
    module this census protects."""
    with open(os.path.join(APP, HOME), encoding='utf-8') as f:
        assert _reads_in_src(f.read()) >= {'conversion_ratio', 'exact_ratio', 'exact_ratios'}


SHAPES = {
    'select': 'def f(c, p, u):\n'
              '    return c.execute("SELECT ratio FROM unit_conversions '
              'WHERE product_id=? AND bsn_unit=?", (p, u))\n',
    'join': 'def f(c):\n'
            '    return c.execute("SELECT st.qty * uc.ratio FROM sales_transactions st "\n'
            '                     "LEFT JOIN unit_conversions uc ON uc.product_id = st.product_id")\n',
    'f-string': 'def f(c, ph, ids):\n'
                '    return c.execute(f"SELECT ratio FROM unit_conversions WHERE product_id IN ({ph})", ids)\n',
    'exists': 'def f(c):\n'
              '    return c.execute("SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM unit_conversions)")\n',
}
NOT_SHAPES = {
    'delete': 'def f(c, p):\n'
              '    c.execute("DELETE FROM unit_conversions WHERE product_id=?", (p,))\n',
    'prose': 'def f():\n'
             '    """Reads the ratio FROM unit_conversions, in prose."""\n',
    'the helper': 'def f(c):\n'
                  '    return unit_conversion.exact_ratios(c, [1])\n',
}


@pytest.mark.parametrize('shape', sorted(SHAPES))
def test_the_sweep_sees_a_read(shape):
    assert _reads_in_src(SHAPES[shape]) == {'f'}


@pytest.mark.parametrize('shape', sorted(NOT_SHAPES))
def test_the_sweep_ignores_what_is_not_a_read(shape):
    assert _reads_in_src(NOT_SHAPES[shape]) == set()


def test_a_new_reader_under_scripts_is_found(tmp_path):
    app_dir, scripts_dir = tmp_path / 'inventory_app', tmp_path / 'scripts'
    app_dir.mkdir()
    scripts_dir.mkdir()
    (app_dir / HOME).write_text(SHAPES['select'], encoding='utf-8')
    assert _census(str(app_dir), str(scripts_dir)) == set(), 'the home module must be skipped'
    (scripts_dir / 'rogue.py').write_text(SHAPES['join'], encoding='utf-8')
    assert _census(str(app_dir), str(scripts_dir)) == {'scripts/rogue.py::f'}
