"""The adopt-suggestion button's state machine (Codex, 2026-08-25).

Pressing it writes a real product name, so an enabled button showing a stale value is a
rename of the wrong thing. The browser behaviour itself cannot be tested here — pytest
and curl do not click — but the two properties that made it wrong ARE checkable in the
rendered page, and both were regressions this branch introduced:

  * `adoptSuggestedName` read `ed-preview`'s DOM text, so it could adopt the '…'
    placeholder or the previous product's suggestion;
  * the button was disabled INSIDE the 250ms debounce, leaving it live in between.

⚠ Assertions run against the script with comments STRIPPED — every claim below is about
code that executes, not a sentence in a comment that happens to contain the symbol.
"""
import re

import pytest


@pytest.fixture
def admin_client(tmp_db):
    """Authed admin test client. Session injection, NOT a real login — this machine's
    Python has no hashlib.scrypt (same pattern as tests/test_product_save.py)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _script(html):
    body = html[html.index("<script>"):html.rindex("</script>")]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"(?m)//.*$", "", body)
    assert "/*" not in body, "unterminated block comment — later assertions would be vacuous"
    return body


@pytest.fixture
def page(admin_client):
    r = admin_client.get('/naming?tab=workbench')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_the_button_ships_disabled(page):
    m = re.search(r'<button[^>]*id="ed-adopt"[^>]*>', page)
    assert m, "adopt button missing"
    assert 'disabled' in m.group(0), m.group(0)
    # CONTROL: a button that is NOT meant to start disabled must not match, or this
    # assertion would pass on any page where everything is disabled.
    save = re.search(r'<button[^>]*id="ed-save"[^>]*>', page)
    assert save and 'disabled' not in save.group(0), save.group(0)


def test_adopt_uses_validated_state_not_the_dom_text(page):
    body = _script(page)
    fn = body[body.index("function adoptSuggestedName()"):]
    fn = fn[:fn.index("\n}") + 2]
    assert "_edSuggestion" in fn, fn
    assert "ed-preview" not in fn, "adopt is reading the preview's DOM text again:\n" + fn


def test_the_suggestion_is_invalidated_before_the_debounce_not_inside_it(page):
    body = _script(page)
    fn = body[body.index("function _edRefresh()"):]
    fn = fn[:fn.index("\n}") + 2]
    before, _, after = fn.partition("setTimeout")
    assert "_setSuggestion(null)" in before, \
        "invalidation happens inside the debounce — the button stays live for 250ms"
    # CONTROL: the request itself must still be inside the debounce, or the split above
    # is not measuring what it claims.
    assert "preview-name" in after, fn
