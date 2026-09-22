"""Only the recorded tests may run a dated script in a pre-590 world.

Same shape as test_pre_mig173_usage.py: every entry carries a reason, and an
entry for a file that no longer uses the helper is a failure too, so the list
cannot rot into a standing exemption.
"""
import os
import re

HERE = os.path.dirname(__file__)

ALLOWED = {
    'test_hammer_bundle_datafix.py':
        'drives scripts/hammer_bundle_datafix.py, a dated one-off (Phase 1 of the '
        '2026-08-14 hammer bundle plan) that opens raw connections; accepted to abort if re-run',
    'test_fix_pack_ratios_592.py':
        'drives scripts/2026_09_19_fix_pack_ratios_592.py, applied on prod 2026-09-19; '
        'accepted to abort if re-run',
    'test_rebase_689_767.py':
        'drives scripts/2026_09_19_rebase_689_767.py, applied on prod 2026-09-19; '
        'accepted to abort if re-run',
}

_CALL = re.compile(r'\bsign_raw_connections\s*\(')


def _users():
    out = set()
    for name in os.listdir(HERE):
        if not name.startswith('test_') or not name.endswith('.py') \
                or name == os.path.basename(__file__):
            continue
        with open(os.path.join(HERE, name), encoding='utf-8') as f:
            if _CALL.search(f.read()):
                out.add(name)
    return out


def test_only_recorded_tests_emulate_the_pre_590_world():
    users = _users()
    assert users, 'no test uses the helper: the sweep itself is broken'
    assert users - set(ALLOWED) == set(), \
        'a new test runs in a pre-590 world; fix the code it drives, or record why here'


def test_every_recorded_entry_is_still_used():
    assert set(ALLOWED) - _users() == set()
