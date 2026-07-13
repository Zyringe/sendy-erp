"""Sidebar nav-active highlight — parent link must not light up on a sibling
child that has its own link, and the archived/deleted pages are gone.

Pins two bugs fixed in the nav-cleanup PR:
  1. `/marketplace` (marketplace.dashboard) used `startswith('marketplace.')`,
     so it wrongly stayed `active` on `/marketplace/review` (which has its own
     sibling link). Same shape for `/labels/manage` vs `/labels/print`.
  2. The old `/labels` (products.labels_view barcode page), `/products/walkthrough`,
     and the whole `/supplier-catalogue/` blueprint were removed → must 404.

Renders against `tmp_db` (a copy of the live DB) as admin — admin is the one
role that sees every link involved. The desktop sidebar (`class="sidebar-link"`)
is the only surface with active logic; the mobile drawer has none, so the helper
pins to sidebar-link anchors to avoid matching drawer links with the same href.

Python 3.9 — Optional[...] not X | None.
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')


def _client(role='admin', user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _sidebar_link_class(html, href):
    """Return the class attribute of the desktop sidebar <a> with this exact
    href. Exact-quote match so href="/marketplace" does not catch
    "/marketplace/review"; sidebar-link prefix so it ignores the drawer."""
    m = re.search(
        r'<a\s+href="' + re.escape(href) + r'"[^>]*?class="(sidebar-link[^"]*)"',
        html,
    )
    assert m, f'sidebar link href={href!r} not found'
    return m.group(1)


# ── marketplace: dashboard must not steal review's highlight ──────────────────

def test_review_page_highlights_only_review(tmp_db):
    html = _client().get('/marketplace/review').get_data(as_text=True)
    assert 'active' in _sidebar_link_class(html, '/marketplace/review')
    assert 'active' not in _sidebar_link_class(html, '/marketplace')


def test_marketplace_dashboard_highlights_itself(tmp_db):
    html = _client().get('/marketplace').get_data(as_text=True)
    assert 'active' in _sidebar_link_class(html, '/marketplace')


# ── labels: manage must not steal print's highlight ───────────────────────────

def test_print_page_highlights_only_print(tmp_db):
    html = _client().get('/labels/print').get_data(as_text=True)
    assert 'active' in _sidebar_link_class(html, '/labels/print')
    assert 'active' not in _sidebar_link_class(html, '/labels/manage')


def test_labels_manage_highlights_itself(tmp_db):
    html = _client().get('/labels/manage').get_data(as_text=True)
    assert 'active' in _sidebar_link_class(html, '/labels/manage')


# ── removed pages are gone ────────────────────────────────────────────────────

def test_removed_routes_return_404(tmp_db):
    c = _client()
    assert c.get('/labels').status_code == 404
    assert c.get('/products/walkthrough').status_code == 404
    assert c.get('/supplier-catalogue/').status_code == 404
