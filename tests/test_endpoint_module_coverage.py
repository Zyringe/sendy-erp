"""Every GET-able endpoint must declare its nav module — or declare that it has none.

Why this exists (2026-07-16): `active_module = _ENDPOINT_MODULE.get(endpoint, 'overview')`
is a SILENT default. A page missing from the map never errors; it just paints the
ภาพรวม sidebar instead of its own. Eight real pages had been sitting in that hole
unnoticed — /products/<id>/pricing, /products/<id>/trade, /products/categorize,
/photos/review, /products/<id>/promotions/new, /regions, /customers/bulk-reassign
and /marketplace/returns all showed Dashboard/แจ้งเตือน/ตรวจบิล as their nav.

It is the same hazard `.claude/rules/erp-engineering-discipline.md` already records
from the /hr/advances incident, in its mirror form: a missing entry there HID the
module sidebar; here it SHOWS the wrong one. Both are invisible without a test —
the app renders happily either way.

So: every GET-able endpoint must appear in `_ENDPOINT_MODULE` (it has a module) or
in `_NAV_EXEMPT_ENDPOINTS` (it renders no sidebar — API/file/redirect/pre-auth).
Being in NEITHER is the bug. Adding a page? Add its endpoint to one of the two.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from access_control import (_ENDPOINT_MODULE, _MODULE_DEFS,
                            _NAV_EXEMPT_ENDPOINTS)

# Module keys a sidebar section can be scoped to. 'mobile' is a deliberate
# sentinel: no _MODULE_DEFS entry and no base.html section, so mobile-only PWA
# pages render no desktop module nav (see _ENDPOINT_MODULE's docstring).
_SENTINEL_MODULES = {'mobile'}
_VALID_MODULES = {m['key'] for m in _MODULE_DEFS} | _SENTINEL_MODULES


@pytest.fixture(scope='module')
def get_endpoints():
    from app import app
    return sorted(
        r.endpoint for r in app.url_map.iter_rules()
        if 'GET' in r.methods and r.endpoint != 'static'
    )


def test_every_get_endpoint_declares_a_module_or_is_exempt(get_endpoints):
    undeclared = [ep for ep in get_endpoints
                  if ep not in _ENDPOINT_MODULE and ep not in _NAV_EXEMPT_ENDPOINTS]
    assert not undeclared, (
        "these GET endpoints are in neither _ENDPOINT_MODULE nor _NAV_EXEMPT_ENDPOINTS, "
        "so active_module silently falls back to 'overview' and they render the ภาพรวม "
        f"sidebar instead of their own: {undeclared}"
    )


def test_module_keys_are_real(get_endpoints):
    bad = {ep: mod for ep, mod in _ENDPOINT_MODULE.items() if mod not in _VALID_MODULES}
    assert not bad, f"endpoints mapped to a module key that does not exist: {bad}"


def test_exempt_and_mapped_are_disjoint():
    both = sorted(set(_ENDPOINT_MODULE) & _NAV_EXEMPT_ENDPOINTS)
    assert not both, f"endpoints both mapped AND exempt — pick one: {both}"


def test_no_stale_entries(get_endpoints):
    """A renamed/deleted route leaves a dead entry that silently does nothing."""
    live = set(get_endpoints)
    from app import app
    all_eps = {r.endpoint for r in app.url_map.iter_rules()}
    stale_exempt = sorted(_NAV_EXEMPT_ENDPOINTS - live)
    assert not stale_exempt, f"_NAV_EXEMPT_ENDPOINTS lists non-existent GET endpoints: {stale_exempt}"
    stale_mapped = sorted(set(_ENDPOINT_MODULE) - all_eps)
    assert not stale_mapped, f"_ENDPOINT_MODULE lists endpoints that no longer exist: {stale_mapped}"
