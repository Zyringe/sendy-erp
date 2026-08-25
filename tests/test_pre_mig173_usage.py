"""Only the recorded tests may reconstruct a pre-mig-173 world.

An escape hatch nobody counts stops being an escape hatch and becomes the
default. This is the same shape as SCRIPT_EXEMPTIONS in
test_source_doc_writer_coverage.py: every entry carries a reason, and an entry
for a file that no longer uses it is a failure too, so the list cannot rot.
"""
import os
import re

HERE = os.path.dirname(__file__)

ALLOWED = {
    'test_mig133_marketplace_sr_customer_code.py':
        'applies migration 133 to a reconstructed pre-state; an applied '
        'migration cannot be edited to declare, and 133 < 173 so it can never '
        'run after the guard in any real environment',
    'test_apply_decision_remaps.py':
        'drives scripts/apply_decision_remaps.py, a dated one-off already '
        'recorded as accepted-to-abort in SCRIPT_EXEMPTIONS',
    'test_apply_stock_and_mapping.py':
        'drives scripts/apply_stock_and_mapping_csv.py, same recorded decision',
    'test_normalize_bsn_units.py':
        'drives scripts/normalize_bsn_units.py, whose own docstring says '
        'DEPRECATED, do not re-run',
}

_CALL = re.compile(r'\bemulate_pre_mig173\s*\(')


def _users():
    out = set()
    for name in os.listdir(HERE):
        # `test_*.py` only: the helper module's own `def` line matches the
        # call regex, and counting the definition as a use makes the list
        # permanently stale.
        if not name.startswith('test_') or not name.endswith('.py') \
                or name == os.path.basename(__file__):
            continue
        with open(os.path.join(HERE, name), encoding='utf-8') as f:
            if _CALL.search(f.read()):
                out.add(name)
    return out


def test_only_recorded_tests_reconstruct_the_pre_guard_world():
    users = _users()
    # CONTROL: if the scan finds nothing at all it is broken, not clean — the
    # four files below are known to call it.
    assert users, 'the scan found no callers; it cannot be distinguishing anything'
    assert users - set(ALLOWED) == set(), (
        'these tests drop the mig 173 guard without a recorded reason: '
        f'{sorted(users - set(ALLOWED))}. A runtime path must DECLARE instead — '
        'that is what scripts/merge_product.py does.')


def test_the_list_does_not_rot():
    stale = set(ALLOWED) - _users()
    assert not stale, f'recorded but no longer using the helper: {sorted(stale)}'


def test_every_entry_carries_a_reason():
    assert all(len(v.strip()) > 30 for v in ALLOWED.values())
