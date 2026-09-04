"""The one definition of BSN line identity and change detection.

These pin `models/bsn_line.py`, which `preview_import` and `import_weekly` both
call so that the confirm page and the ledger cannot disagree about what a line
is or whether it changed.

Every predicate gets its own test, so removing any one of them turns exactly
one test red rather than being absorbed by a neighbour. The party test is the
CONTROL for the claim that a stock-event change is not the negation of a field
diff: it changes something `field_diff` does not look at.

No database: the whole module is pure.
"""

import pytest

import bsn_units
from models import bsn_line


def _row(**over):
    row = {
        'doc_no': 'IV6900123-1', 'bsn_code': '030บ34', 'line_seq': 1,
        'qty': 12.0, 'unit': 'โหล', 'unit_price': 45.0, 'net': 540.0,
        'product_id': 77, 'customer': 'ร้านวรสวัสดิ์', 'supplier': 'ซัพ ก',
    }
    row.update(over)
    return row


def _entry(**over):
    e = {
        'doc_no': 'IV6900123-1', 'product_code_raw': '030บ34', 'line_seq': 1,
        'qty': 12.0, 'unit': 'โหล', 'unit_price': 45.0, 'net': 540.0,
        'party': 'ร้านวรสวัสดิ์',
    }
    e.update(over)
    return e


# ── line identity ──────────────────────────────────────────────────────────

def test_sales_key_is_doc_no_and_code_only():
    """doc_no already carries the printed '-N' suffix, so sales needs no seq."""
    assert bsn_line.entry_key(_entry(), 'sales') == ('IV6900123-1', '030บ34')
    assert bsn_line.row_key(_row(), 'sales') == ('IV6900123-1', '030บ34')


def test_purchase_key_adds_line_seq():
    """Purchase doc_nos have no suffix, so line_seq separates repeated products."""
    assert bsn_line.entry_key(_entry(), 'purchase') == ('IV6900123-1', '030บ34', 1)
    assert bsn_line.row_key(_row(), 'purchase') == ('IV6900123-1', '030บ34', 1)


def test_entry_and_row_keys_agree_across_the_field_rename():
    """The whole point: a parsed entry says product_code_raw, a stored row says
    bsn_code, and they must still produce the same key."""
    for ft in ('sales', 'purchase'):
        assert bsn_line.entry_key(_entry(), ft) == bsn_line.row_key(_row(), ft)


def test_purchase_line_seq_defaults_to_one_when_the_parser_omits_it():
    e = _entry()
    del e['line_seq']
    assert bsn_line.entry_key(e, 'purchase') == ('IV6900123-1', '030บ34', 1)


def test_two_lines_of_one_product_on_one_purchase_doc_are_distinct():
    a = bsn_line.entry_key(_entry(line_seq=1), 'purchase')
    b = bsn_line.entry_key(_entry(line_seq=2), 'purchase')
    assert a != b, 'line_seq is what keeps repeated purchase lines apart'


def test_doc_base_strips_only_the_line_suffix():
    assert bsn_line.doc_base('IV6900123-1') == 'IV6900123'
    assert bsn_line.doc_base('IV6900123') == 'IV6900123'


# ── field diff: one test per predicate ─────────────────────────────────────

def test_identical_line_has_no_field_diff():
    assert bsn_line.field_diff(_row(), _entry(), 77, 'โหล') == []


@pytest.mark.parametrize('field,row_over,entry_over,new_pid', [
    ('qty',        {'qty': 12.0},        {'qty': 13.0},        77),
    ('unit_price', {'unit_price': 45.0}, {'unit_price': 46.0}, 77),
    ('net',        {'net': 540.0},       {'net': 598.0},       77),
])
def test_each_numeric_predicate_is_detected_on_its_own(field, row_over,
                                                       entry_over, new_pid):
    diffs = bsn_line.field_diff(_row(**row_over), _entry(**entry_over),
                                new_pid, 'โหล')
    assert len(diffs) == 1, f'expected only {field} to differ, got {diffs}'
    assert diffs[0][0] == field


def test_product_id_predicate_is_detected_on_its_own():
    diffs = bsn_line.field_diff(_row(product_id=77), _entry(), 88, 'โหล')
    assert len(diffs) == 1 and diffs[0] == ('product_id', 77, 88)


def test_unit_predicate_is_detected_on_its_own():
    diffs = bsn_line.field_diff(_row(unit='โหล'), _entry(), 77, 'กล่อง')
    assert len(diffs) == 1 and diffs[0][0] == 'unit'


def test_null_product_id_on_both_sides_is_not_a_change():
    """`or 0` on both sides: an unmapped row staying unmapped is unchanged."""
    assert bsn_line.field_diff(_row(product_id=None), _entry(), None, 'โหล') == []


def test_all_five_predicates_can_fire_at_once():
    """Anti-vacuity control: the diff list is really built from five checks."""
    diffs = bsn_line.field_diff(
        _row(), _entry(qty=1.0, unit_price=1.0, net=1.0), 999, 'กล่อง')
    assert [d[0] for d in diffs] == list(bsn_line.DIFF_FIELDS)


# ── the traps the two copies encoded by hand ───────────────────────────────

def test_float_noise_below_epsilon_is_not_a_change():
    """REAL columns re-parsed from the same file can differ in the last bit."""
    assert bsn_line.field_diff(_row(net=540.0), _entry(net=540.0 + 1e-12),
                               77, 'โหล') == []


def test_a_real_satang_difference_is_a_change():
    """CONTROL for the epsilon test above — it must not swallow real money."""
    diffs = bsn_line.field_diff(_row(net=540.0), _entry(net=540.01), 77, 'โหล')
    assert len(diffs) == 1 and diffs[0][0] == 'net'


def test_a_raw_acronym_stored_unit_matches_its_normalised_form():
    """The ~95%-churn trap: legacy rows hold raw acronyms (หล/ตว/กก…) while the
    incoming unit is normalised. Comparing them raw marks nearly every purchase
    row as changed and churns the ledger for nothing."""
    raw = 'หล'
    normalised = bsn_units.normalize_unit(raw)
    assert normalised != raw, (
        'fixture no longer exercises the trap: normalize_unit is a no-op for '
        f'{raw!r}. Pick an acronym that is actually in the unit map.')
    assert bsn_line.field_diff(_row(unit=raw), _entry(), 77, normalised) == []


def test_the_unit_diff_reports_the_stored_unit_raw():
    """The operator needs to see what is actually stored, not its normal form."""
    diffs = bsn_line.field_diff(_row(unit='หล'), _entry(), 77, 'กล่อง')
    assert diffs == [('unit', 'หล', 'กล่อง')]


def test_a_missing_entry_key_raises_rather_than_comparing_against_none():
    """The entry shape is a contract with two independent parsers. A key one of
    them forgets must fail loudly, not compare quietly against None and report
    the line as changed."""
    e = _entry()
    del e['net']
    with pytest.raises(KeyError):
        bsn_line.field_diff(_row(), e, 77, 'โหล')


# ── stock event: NOT the negation of the field diff ────────────────────────

def test_a_price_only_correction_leaves_the_stock_event_alone():
    """The common case. Reversing and re-applying would be pure churn, and
    churn is lossy because the undo clamps at zero."""
    row, entry = _row(unit_price=45.0), _entry(unit_price=46.0)
    assert bsn_line.field_diff(row, entry, 77, 'โหล'), 'field diff should fire'
    assert not bsn_line.stock_event_changed(row, entry, 77, 'โหล', 'sales')


def test_a_quantity_correction_does_change_the_stock_event():
    """A 5 -> 7 correction landed at 88 instead of 93 before mig 172."""
    assert bsn_line.stock_event_changed(
        _row(qty=5.0), _entry(qty=7.0), 77, 'โหล', 'sales')


def test_a_party_change_changes_the_stock_event_but_not_the_field_diff():
    """CONTROL for 'not a subset': the party decides the platform deduction,
    and field_diff never looks at it."""
    row = _row(customer='ร้านวรสวัสดิ์')
    entry = _entry(party='หน้าร้านS')
    assert bsn_line.field_diff(row, entry, 77, 'โหล') == [], (
        'field_diff must not look at the party')
    assert bsn_line.stock_event_changed(row, entry, 77, 'โหล', 'sales')


def test_party_column_follows_the_file_type():
    assert bsn_line.party_column('sales') == 'customer'
    assert bsn_line.party_column('purchase') == 'supplier'


def test_stock_event_reads_the_supplier_column_on_the_purchase_side():
    row = _row(supplier='ซัพ ก', customer='ไม่เกี่ยว')
    assert not bsn_line.stock_event_changed(
        row, _entry(party='ซัพ ก'), 77, 'โหล', 'purchase')
    assert bsn_line.stock_event_changed(
        row, _entry(party='ซัพ ข'), 77, 'โหล', 'purchase')


def test_party_comparison_ignores_surrounding_whitespace():
    row = _row(customer='  ร้านวรสวัสดิ์  ')
    assert not bsn_line.stock_event_changed(
        row, _entry(party='ร้านวรสวัสดิ์'), 77, 'โหล', 'sales')


# ── the guard the hand-maintained copies never had ─────────────────────────

def _imports_src():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, 'inventory_app', 'models', 'imports.py')
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_preview_and_commit_both_go_through_bsn_line():
    """Both sites must call the shared module, not their own copy.

    `preview_import` and `import_weekly` each used to spell out the same five
    predicates. Nothing failed when they drifted — that is what this replaces.
    """
    src = _imports_src()
    assert src.count('bsn_line.field_diff(') == 2, (
        'expected exactly two field_diff call sites (preview + commit); '
        'a missing one means a copy came back')
    assert src.count('bsn_line.stock_event_changed(') == 1
    assert src.count('bsn_line.entry_key(') == 1
    assert src.count('bsn_line.row_key(') == 1


def test_no_inline_line_comparison_has_come_back():
    """The shapes the two copies were written in. Any of them reappearing in
    imports.py means someone re-inlined a rule that now lives in bsn_line."""
    src = _imports_src()
    banned = {
        '1e-9': 'the float tolerance belongs to bsn_line.EPSILON',
        "normalize_unit(old[": 'stored-unit normalisation belongs to bsn_line',
        'stock_event_same = (': 'the stock-event rule belongs to bsn_line',
    }
    offenders = {frag: why for frag, why in banned.items() if frag in src}
    assert not offenders, f'inline comparison is back in imports.py: {offenders}'

    # CONTROL: this check reads a file that really does contain the module's
    # name, so an empty result above means "clean", not "read the wrong file".
    assert 'bsn_line' in src, (
        'CONTROL FAILED: imports.py does not mention bsn_line at all -- the '
        'test is reading the wrong file and the check above proved nothing')
