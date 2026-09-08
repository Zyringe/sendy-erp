"""Every call site of cross_unit_hazard() is accounted for against the
'configuration_error' hazard kind it can now return (never raise past its own
boundary — see models/bsn_sync.py's docstring, and
tests/test_cross_unit_hazard_configuration_error.py for the behavioural
coverage this sweep is checking wasn't missed anywhere).

Why this exists (Codex review finding 3, PR #388): cross_unit_hazard used to
propagate ConversionRoleError straight out of a malformed [แพ็ค] formula, and
NOT ONE of its (then) 6 call sites had a try/except around it — two of them
are list builders, so one bad formula 500'd a whole page. Patching callers
one at a time, from memory, is exactly the failure mode that produced that
gap in the first place. This test does the sweep mechanically, modeled on
tests/test_revenue_filter_coverage.py: a new call site with no registered
handling fails CI instead of silently reproducing the same bug.
"""
import os
import re

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

# Every call site of cross_unit_hazard( in application code (excluding its own
# definition and bare import statements), keyed by (relative file path,
# enclosing top-level function name) — line numbers shift across edits,
# function names don't. Each entry records WHY that call site is safe
# against a 'configuration_error' hazard: either it's a WRITE that blocks
# unconditionally, or a READ/LIST that renders it as a warning row rather
# than branching on 'kind' in a way that could explode.
CALL_SITES = {
    ('models/mapping.py', 'get_pending_split_mappings'):
        "Read/list builder — appends the hazard dict verbatim (any kind, "
        "opaque) to its output row for mapping.html's pending_splits table. "
        "The template only special-cases kind=='pair' (to pre-select an "
        "option) and kind=='configuration_error' (its own warning message); "
        "every other kind, including ones added in future, falls through to "
        "a plain destination-picker row rather than raising.",
    ('models/bsn_sync.py', 'get_pending_unit_conversions'):
        "Read/list builder — assigns the hazard dict verbatim to d['hazard'] "
        "for unit_conversions.html, which has an explicit "
        "kind=='configuration_error' branch (a warning row + a link to the "
        "conversions page, no editable ratio input) alongside 'pair' and "
        "'pack_piece'.",
    ('models/bsn_sync.py', 'save_unit_conversions'):
        "Write — blocks unconditionally when "
        "hazard['kind'] in _UNCONDITIONAL_BLOCK_KINDS (pair, "
        "configuration_error), regardless of the submitted ratio.",
    ('models/bsn_sync.py', 'update_unit_conversion_ratio'):
        "Write — same _UNCONDITIONAL_BLOCK_KINDS check as "
        "save_unit_conversions.",
    ('models/bsn_sync.py', 'upsert_unit_conversion'):
        "Write — same _UNCONDITIONAL_BLOCK_KINDS check.",
    ('models/suggestions.py', 'approve_pending_suggestion'):
        "Write — an ALLOWLIST shape, not a blocklist: "
        "`hz is None or (hz['kind']=='pack_piece' and ratio==1)` is the only "
        "condition that inserts the unit_conversions row. Any OTHER kind — "
        "'pair', 'configuration_error', or a kind added in future — already "
        "falls through to 'do not insert' without needing to name it, so "
        "this call site needed no code change for this finding.",
}

# Second axis, added for issue #389: does this call site already hold SQLite's
# WRITE LOCK when it calls cross_unit_hazard? If it does, the alert's default
# fresh connection cannot get in — it waits out the busy timeout and the
# best-effort except swallows it, so the site must pass
# alert_on_caller_conn=True and file the alert inside its own transaction.
#
# This axis gets its own registry because it is invisible at the call site and
# the ORIGINAL issue got it wrong in both directions: it named two callers that
# check the hazard BEFORE their first write (no lock held) and missed the one
# that writes three times first. The rule is mechanical — a caller is exposed
# iff it writes on `conn` before the call AND can reach the
# configuration_error branch — but nothing enforced it until this sweep.
ALERT_CONN = {
    ('models/mapping.py', 'get_pending_split_mappings'): (
        False,
        "Read/list builder — never writes on its connection, and calls this "
        "mid-SELECT-loop, so a write here would hold the lock for the whole "
        "request. Must keep the fresh connection."),
    ('models/bsn_sync.py', 'get_pending_unit_conversions'): (
        False,
        "Read/list builder — same as get_pending_split_mappings."),
    ('models/bsn_sync.py', 'save_unit_conversions'): (
        True,
        "EXPOSED. The loop's first INSERT INTO unit_conversions takes the write "
        "lock and holds it until the single commit after the loop, so a "
        "malformed formula met on iteration 2+ could not be alerted from a "
        "fresh connection. It has no rollback path, so filing on its own "
        "connection is genuinely durable."),
    ('models/bsn_sync.py', 'update_unit_conversion_ratio'): (
        False,
        "The hazard check is the first statement to touch the DB after "
        "get_connection(), and it close()s and returns on a block — under "
        "deferred isolation no write lock exists yet."),
    ('models/bsn_sync.py', 'upsert_unit_conversion'): (
        False,
        "Same shape as update_unit_conversion_ratio — hazard check precedes "
        "every write."),
    ('models/suggestions.py', 'approve_pending_suggestion'): (
        False,
        "Holds the lock (three writes precede the call) but hands "
        "cross_unit_hazard a product id create_structured_product just "
        "INSERTed, and no conversion_formulas row can reference a brand-new "
        "id — the configuration_error branch is unreachable, so no alert can "
        "be lost. Pinned by test_conversion_role_alert_durability.py::"
        "test_approve_pending_suggestion_cannot_reach_the_alert_path; if that "
        "goes red this entry becomes True."),
}


def _function_body(rel_path, fn):
    src = open(os.path.join(APP, rel_path), encoding='utf-8').read()
    m = re.search(rf'\ndef {fn}\(.*?(?=\ndef |\Z)', src, re.S)
    assert m, f'{fn} not found in {rel_path}'
    return m.group(0)


def test_every_call_site_declares_whether_it_holds_the_write_lock():
    missing = set(CALL_SITES) - set(ALERT_CONN)
    assert not missing, (
        "New cross_unit_hazard call site(s) with no ALERT_CONN entry. Decide: "
        "does this caller WRITE on its connection before the call, and can it "
        "reach the configuration_error branch? If both, pass "
        "alert_on_caller_conn=True or its durable alert is silently lost "
        "(#389):\n  " + "\n  ".join(f'{f}::{fn}' for f, fn in sorted(missing)))
    stale = set(ALERT_CONN) - set(CALL_SITES)
    assert not stale, "Remove these stale ALERT_CONN entries:\n  " + "\n  ".join(
        f'{f}::{fn}' for f, fn in sorted(stale))


@pytest.mark.parametrize('site', sorted(ALERT_CONN))
def test_alert_conn_declaration_matches_the_code(site):
    """The registry is a claim about the source; check the source agrees.

    Each of these functions contains exactly ONE cross_unit_hazard call, so
    looking for the keyword anywhere in the function body is unambiguous."""
    expected, reason = ALERT_CONN[site]
    assert len(reason) > 40, f'{site}: explain WHY, in a sentence'
    body = _function_body(*site)
    passes_flag = 'alert_on_caller_conn=True' in body
    assert passes_flag == expected, (
        f'{site[0]}::{site[1]} — ALERT_CONN says '
        f'{"it must pass" if expected else "it must NOT pass"} '
        f'alert_on_caller_conn=True, but the code '
        f'{"does not" if expected else "does"}. Registry reason: {reason}')


def test_the_flag_defaults_to_off():
    """The safe default is the read/list one: a new call site that forgets to
    think about this gets the fresh connection, which is at worst a missed
    alert — never a write transaction held open inside someone's SELECT loop."""
    src = open(os.path.join(APP, 'models/bsn_sync.py'), encoding='utf-8').read()
    assert 'def cross_unit_hazard(conn, product_id, bsn_unit, *, alert_on_caller_conn=False):' in src


_CALL_RE = re.compile(r'cross_unit_hazard\(')
_DEF_RE = re.compile(r'^def (\w+)\(')
_IMPORT_HINTS = ('import', 'from ')


def _call_sites_in_app():
    """(rel_path, enclosing_top_level_def_name) for every real CALL of
    cross_unit_hazard — i.e. every regex match except the def line itself
    and an import statement naming it."""
    found = set()
    for root, _dirs, names in os.walk(APP):
        if any(part in root for part in ('__pycache__', 'instance', 'static')):
            continue
        for n in names:
            if not n.endswith('.py'):
                continue
            path = os.path.join(root, n)
            rel = os.path.relpath(path, APP).replace(os.sep, '/')
            current_def = None
            for line in open(path, encoding='utf-8'):
                m = _DEF_RE.match(line)
                if m:
                    current_def = m.group(1)
                if 'def cross_unit_hazard(' in line:
                    continue                                   # the definition
                stripped = line.lstrip()
                if any(stripped.startswith(h) for h in _IMPORT_HINTS) and 'cross_unit_hazard' in line:
                    continue                                   # a bare import line
                if _CALL_RE.search(line):
                    found.add((rel, current_def))
    return found


def test_every_call_site_is_registered_with_a_reason():
    sites = _call_sites_in_app()
    unregistered = sites - set(CALL_SITES)
    assert not unregistered, (
        "New cross_unit_hazard call site(s) with no registered handling — "
        "confirm they treat kind=='configuration_error' correctly (write: "
        "unconditional block, same as 'pair'; read/list: no branch that can "
        "explode on an unrecognised kind), then add an entry to CALL_SITES "
        "here saying how:\n  " +
        "\n  ".join(f'{f}::{fn}' for f, fn in sorted(unregistered)))


def test_registered_call_sites_still_exist():
    """A stale registry entry hides the next real miss — same rationale as
    test_revenue_filter_coverage.py's equivalent check."""
    sites = _call_sites_in_app()
    stale = set(CALL_SITES) - sites
    assert not stale, "Remove these stale entries:\n  " + "\n  ".join(
        f'{f}::{fn}' for f, fn in sorted(stale))


@pytest.mark.parametrize('site', sorted(CALL_SITES))
def test_every_entry_carries_a_reason(site):
    assert len(CALL_SITES[site]) > 40, f'{site}: explain WHY it is safe, in a sentence'


def test_write_callers_actually_check_the_new_kind():
    """Positive control for the 3 write call sites in bsn_sync.py — the sweep
    above only proves the registry NAMES them, not that the code still does
    what the registry claims. Their function source must reference
    'configuration_error' (directly, or via the shared
    _UNCONDITIONAL_BLOCK_KINDS constant both name)."""
    src = open(os.path.join(APP, 'models/bsn_sync.py'), encoding='utf-8').read()
    for fn in ('save_unit_conversions', 'update_unit_conversion_ratio', 'upsert_unit_conversion'):
        m = re.search(rf'\ndef {fn}\(.*?(?=\ndef |\Z)', src, re.S)
        assert m, f'{fn} not found in models/bsn_sync.py'
        body = m.group(0)
        assert '_UNCONDITIONAL_BLOCK_KINDS' in body or 'configuration_error' in body, \
            f'{fn} no longer checks the configuration_error hazard kind'


def test_cross_unit_hazard_itself_never_lets_the_role_error_escape():
    """Positive control on the ROOT fix: cross_unit_hazard's own body must
    catch ConversionRoleError, not merely happen to not raise it today.

    Counts occurrences (expect 2), not just membership — component_product_id
    is called from TWO separate loops in this function (product as the
    OUTPUT of a pack half, and product as an INPUT of one), and a bare
    `in body` check cannot tell 'both loops guarded' from 'one loop guarded,
    the OTHER one bare' since the substring still appears once either way.
    Caught exactly this gap via break-it-once while building this test: with
    only the first loop's try/except removed, the membership version stayed
    green because the second loop's try/except was still present."""
    src = open(os.path.join(APP, 'models/bsn_sync.py'), encoding='utf-8').read()
    m = re.search(r'\ndef cross_unit_hazard\(.*?(?=\ndef |\Z)', src, re.S)
    assert m, 'cross_unit_hazard not found in models/bsn_sync.py'
    body = m.group(0)
    assert body.count('except ConversionRoleError') == 2
    # 3, not 2: the docstring itself names the return shape once, plus the
    # two code sites (one per loop).
    assert body.count("'kind': 'configuration_error'") == 3
