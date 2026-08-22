"""Every Jinja comment delimiter in every template must be matched.

Why this is a repo-wide sweep and not one page's assertion: an unmatched `#}`
renders as literal text, and inside a `<script>` that text silently becomes
part of the JavaScript. PR4 of mapping-suggest-clone closed a JS block comment
with `#}` instead of `*/`; the comment then ran on to the next real `*/`
~120 lines later, commenting out the entire clone feature. The page still
rendered every id and every `function ...(` name, so seven substring-based
tests stayed green and the browser silently did nothing.

`{#`/`#}` cannot nest in Jinja, so a single left-to-right walk is exact.

Run: cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_template_comment_hygiene.py -q
"""
import io
import os

import pytest

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'inventory_app', 'templates',
)

# Add an entry ONLY with a written reason — a legitimate literal `#}` (e.g. a
# JS string) rather than a typo. Empty is the correct steady state.
ALLOWED = {}   # relative path -> reason


def _stray_delimiters(text):
    """Unmatched `{#` / `#}`, as (delimiter, 1-based line number)."""
    strays, i, open_at = [], 0, None
    while True:
        nxt_open, nxt_close = text.find('{#', i), text.find('#}', i)
        if open_at is None:
            if nxt_open == -1 and nxt_close == -1:
                return strays
            if nxt_close != -1 and (nxt_open == -1 or nxt_close < nxt_open):
                strays.append(('#}', text.count('\n', 0, nxt_close) + 1))
                i = nxt_close + 2
            else:
                open_at, i = nxt_open, nxt_open + 2
        elif nxt_close == -1:
            strays.append(('{#', text.count('\n', 0, open_at) + 1))
            return strays
        else:
            open_at, i = None, nxt_close + 2


def _template_files():
    for root, _dirs, files in os.walk(TEMPLATES):
        for name in files:
            if name.endswith('.html'):
                full = os.path.join(root, name)
                yield os.path.relpath(full, TEMPLATES), full


def test_the_detector_can_actually_fire():
    """CONTROL. A green sweep below is only evidence if this passes."""
    assert _stray_delimiters('{# fine #}') == []
    assert _stray_delimiters('a {# open') == [('{#', 1)]
    assert _stray_delimiters('/* js\n   comment #}\nconst x = 1;') == [('#}', 2)]
    # the real shape: a matched comment earlier in the file must not "absorb"
    # the stray one that follows it
    assert _stray_delimiters('{# real #}\n<script>\n/* c #}\n</script>') == [('#}', 3)]


def test_every_template_is_scanned():
    """CONTROL: the walk found the tree, not an empty directory."""
    found = list(_template_files())
    assert len(found) > 50, f"only {len(found)} templates found under {TEMPLATES}"
    assert any(rel == 'products/form.html' for rel, _ in found)


@pytest.mark.parametrize('rel,full', sorted(_template_files()))
def test_template_has_no_unmatched_jinja_comment_delimiter(rel, full):
    if rel in ALLOWED:
        pytest.skip(f"allowlisted: {ALLOWED[rel]}")
    strays = _stray_delimiters(io.open(full, encoding='utf-8').read())
    assert not strays, (
        f"{rel}: unmatched Jinja comment delimiter(s) "
        + ', '.join(f"{d} at line {ln}" for d, ln in strays)
        + " — inside a <script> this comments out live code"
    )
