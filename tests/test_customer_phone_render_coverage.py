"""Every template that renders a customer phone goes through `phone_entries`.

Why this exists: `customers.phone` holds a LIST (ADR 0011). #461 fixed the call
card, where handing the raw comma-joined column to `href="tel:"` dialled nothing
usable for 62% of the customer book. Six other surfaces kept the broken shape,
and two of them (`m/customer.html`, `m/sales_trip.html`) hid it behind their own
inline `.split(',')` — which is exactly why nobody noticed the rest were wrong:
the one screen a rep looked at daily *looked* fine.

Reading templates one at a time is what let that happen. This sweep does it
mechanically, so the next person who adds a surface (or forgets one) is told at
CI time rather than by a rep dialling a comma.

⚠ What this sweep CANNOT see, stated so nobody reads a green run as more than it
is:
  - `.py` files. A route that builds HTML in Python, or an API returning a raw
    phone to a caller that renders it, is outside the scan. The one server-side
    consumer that exists today (`partners.customer_map`) is pinned by name in
    `test_the_surfaces_that_must_be_guarded_are`.
  - Anything reached by a `{% include %}`/`{% import %}` is scanned as its own
    file, not as part of its includer — fine here, but it means "surface" in
    this file means "template file", not "page".
  - JavaScript that assembles a phone from parts, or reads one from `fetch`
    JSON, without the substring `phone` appearing on the line.
"""
import os
import re

import pytest

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'inventory_app', 'templates')

# Templates allowed to render a phone WITHOUT `phone_entries`. Each entry must
# say why — a reason is what makes this a decision rather than an oversight.
ALLOWED = {
    'hr/employee_detail.html':
        'employees.phone, not customers.phone — a single number in its own '
        'column, with no comma-joined list to split. It renders through '
        '`thai_phone` (the single-number formatter added by #463), and is '
        'pinned by test_employee_identity_surfaces.py. Wiring it to '
        '`phone_entries` instead would split a column that never holds a list '
        '— the second convention this sweep exists to prevent.',
    'marketplace/_order_detail_modal.html':
        'marketplace_orders.buyer_phone — one number supplied by Shopee/Lazada '
        'per order, never edited here and never comma-joined. Not a customer '
        'record and not reachable from the customer book.',
    'ar_followup_detail.html':
        "not a number at all: `'phone'` is a value of ar_followup_log.channel "
        "(phone / line / sms / email / visit), and the `{% set channel_map %}` "
        'this matches renders it as the Thai word โทร. The sweep matches a bare '
        'substring on purpose, so a word meaning "by telephone" is the price of '
        'never missing a real one.',
}

# Over-inclusive on purpose, in the same stance as
# test_revenue_filter_coverage.py: a false hit costs one allowlist decision, a
# false miss costs a rep a failed call. Near-bare substring, so it catches names
# a tighter identifier pattern would miss — `phone_raw`, `primary_phone`,
# `orig_phone`, `c.phone`, `orig.get('phone')`.
#
# The one thing filtered out is a LETTER immediately before: `bi-telephone` is
# a Bootstrap icon class, and it appears inside real interpolations
# (`{{ 'printer' if e.is_fax else 'telephone' }}`), so matching it would put a
# permanent false hit on the surfaces this ticket just fixed — the fastest way
# to get a sweep deleted for noise. Cost of the filter: a name like `custphone`
# with no separator is invisible. Every phone identifier in this app is
# snake_case or dotted.
_PHONE = re.compile(r'(?i)(?<![A-Za-z])phone')
_GUARD = 'phone_entries'

_JINJA_OUT = re.compile(r'\{\{.*?\}\}', re.DOTALL)
# A `{% set %}` that READS a phone is treated as a render even though it prints
# nothing, because it is how a phone escapes a name-based sweep entirely:
# `{% set nums = customer.phone|... %}` then `{{ nums }}` — the output has no
# "phone" in it, so nothing downstream fires. That is one rename away from the
# exact shape #462 deleted (`{% set primary_phone = customer.phone.split(',')[0] %}`),
# and the old name is the only reason the sweep saw it. Found by the standards
# review, 2026-09-09.
_JINJA_SET = re.compile(r'\{%-?\s*set\b.*?%\}', re.DOTALL)
_SCRIPT = re.compile(r'(?is)<script\b[^>]*>(.*?)</script>')

# Shared by the sweep and by its own break-it-once, so the proof cannot pass
# against a stale COPY of the pattern while the real one rots.
_TEL_TARGET = re.compile(r'tel:\s*(\{\{.*?\}\}|\$\{[^}]*\})', re.DOTALL)

# `.split(',')` and `.rsplit(',')` alike — the sweep above catches either when
# the phone name is visible, but paired with a `{% set %}` rename this detector
# is the only thing left looking.
_HAND_SPLIT = re.compile(r"\.r?split\(\s*['\"],")


_TEXTAREA = re.compile(r'(?is)<textarea\b[^>]*>.*?</textarea>')


def _is_write_path(html, pos):
    """True when `pos` sits inside a form control.

    A form control holds the value on its way BACK to storage, so it must carry
    the stored spelling exactly — splitting it for display there would feed the
    split back into the column. ADR 0011 keeps the column comma-joined; the box
    Put types into is the one place that shape is the point.

    Two shapes, because they nest differently: an <input> carries its value in
    an ATTRIBUTE (so `pos` is inside the tag), a <textarea> carries it as
    element CONTENT (so `pos` is between the tags, and the attribute test above
    reads it as ordinary markup).
    """
    open_at = html.rfind('<', 0, pos)
    if open_at != -1:
        tag = html[open_at:pos]
        # '>' before `pos` means the tag closed already — `pos` is not in it
        if ('>' not in tag
                and re.match(r'(?i)<(input|textarea)\b', tag)
                # ...and specifically inside value=, not just anywhere in the
                # tag. `<input readonly title="{{ c.phone }}">` is a DISPLAY
                # (a tooltip a reader sees); exempting the whole tag let it
                # through silently. Found by the standards review, 2026-09-09.
                and re.search(r'(?i)\bvalue\s*=\s*["\']?[^"\']*$', tag)):
            return True
    return any(m.start() < pos < m.end() for m in _TEXTAREA.finditer(html))


def _sites(paths):
    """Every place a template RENDERS something whose name mentions a phone.

    Three regions count, and nothing else does:
      - a Jinja output expression `{{ ... }}`
      - a `{% set %}` — it prints nothing, but it RENAMES, and the rename is
        what makes everything downstream invisible to a name-based sweep
      - a line inside a <script> block (covers `${c.phone}` and plain DOM writes)

    `{% if customer.phone %}` is deliberately NOT a site: a guard decides
    whether to render, it does not render. Flagging it would push authors to
    delete the guard to quiet the sweep.

    Returns [(relpath, lineno, text, guarded, write_path)].
    """
    out = []
    for rel, path in sorted(paths):
        html = open(path, encoding='utf-8').read()
        seen = set()
        for m in list(_JINJA_OUT.finditer(html)) + list(_JINJA_SET.finditer(html)):
            if not _PHONE.search(m.group(0)):
                continue
            line = html.count('\n', 0, m.start()) + 1
            seen.add((line, m.group(0)))
            out.append((rel, line, m.group(0).strip(),
                        _GUARD in m.group(0), _is_write_path(html, m.start())))
        for sm in _SCRIPT.finditer(html):
            body_start = sm.start(1)
            for offset, raw in enumerate(sm.group(1).split('\n')):
                if not _PHONE.search(raw):
                    continue
                line = html.count('\n', 0, body_start) + 1 + offset
                # A {{ }} inside a <script> is already recorded above; do not
                # count it twice under a different text.
                if any(line == ln and _PHONE.search(txt) for ln, txt in seen):
                    continue
                out.append((rel, line, raw.strip(), _GUARD in raw, False))
    return out


def _template_files(root=TEMPLATES):
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.endswith('.html'):
                path = os.path.join(dirpath, n)
                yield os.path.relpath(path, root).replace(os.sep, '/'), path


def _unguarded(paths=None):
    return [s for s in _sites(paths if paths is not None else _template_files())
            if not s[3] and not s[4]]


# ── the sweep ────────────────────────────────────────────────────────────────

def test_no_surface_renders_a_raw_customer_phone():
    offenders = [(rel, line, text) for rel, line, text, _, _ in _unguarded()
                 if rel not in ALLOWED]
    assert not offenders, (
        "These render a phone without `| phone_entries`. `customers.phone` "
        "holds a LIST (ADR 0011) — pipe it through the shared filter, or add an "
        "ALLOWED entry saying why this one is exempt:\n  " +
        "\n  ".join(f'{rel}:{line}  {text}' for rel, line, text in offenders))


def test_no_dial_target_is_built_from_anything_but_a_dial_value():
    """The defect that started #460, enforced structurally rather than per page.

    `phone_entries` is the only thing that produces `dial` (bare digits, comma
    impossible by construction). Any other expression behind `tel:` is the old
    bug wearing a new name.
    """
    bad, seen = [], 0
    for rel, path in sorted(_template_files()):
        html = open(path, encoding='utf-8').read()
        for m in _TEL_TARGET.finditer(html):
            seen += 1
            if 'dial' not in m.group(1):
                bad.append(f'{rel}:{html.count(chr(10), 0, m.start()) + 1}  {m.group(0)}')
    # CONTROL: `not bad` is also what an app with no dial targets at all
    # returns. Assert the app still HAS them before assering they are sound.
    assert seen >= 3, f'only {seen} dial targets in the whole tree — check the check'
    assert not bad, "dial targets not built from phone_entries' `dial`:\n  " + "\n  ".join(bad)


def test_the_surfaces_that_must_be_guarded_are():
    """Positive control. The sweep above passes just as happily when a phone
    block is DELETED as when it is fixed, so name the surfaces that must still
    be rendering one.

    Requires the PIPE, not the bare token: `{# phone_entries #}` in a comment
    would satisfy a substring test while the surface renders nothing — the same
    prose-counts-as-a-guard hole `test_revenue_filter_coverage.py::
    test_a_guard_token_only_in_prose_does_not_count_as_a_guard` exists for.
    """
    must = ('call/card.html', 'call/list.html', 'customer_summary.html',
            'customer_review/detail.html', 'customer_review/list.html',
            'm/customer.html', 'm/sales_trip.html')
    for rel in must:
        src = open(os.path.join(TEMPLATES, rel), encoding='utf-8').read()
        assert re.search(r'\|\s*' + _GUARD, src), \
            f'{rel} no longer pipes anything through {_GUARD}'


def test_the_map_popup_is_still_fed_by_the_route():
    """The map is the one surface whose entries come from Python, because its
    popup is built in JS — so neither half is covered by the template sweep.

    Assert the WIRING, not a substring: `_with_phone_entries` is named in
    `customer_map`'s own body, and the name `phone_entries` inside partners.py
    really is the filter. A file-level `'phone_entries' in source` passes on
    this module's docstrings alone — measured, while the call was removed.
    """
    import inspect

    import filters
    from blueprints import partners

    assert partners.phone_entries is filters.phone_entries, \
        'partners.phone_entries is not the shared filter'
    body = inspect.getsource(partners.customer_map)
    assert '_with_phone_entries' in body, \
        'customer_map stopped feeding the popup its entries'
    # CONTROL: the popup template must actually read what the route sends.
    popup = open(os.path.join(TEMPLATES, 'customer_map.html'), encoding='utf-8').read()
    assert 'c.phone_entries' in popup


def test_no_template_splits_a_phone_on_commas_by_hand():
    """The specific workaround #462 exists to delete. `m/customer.html` split
    the field inline and picked `[0]` as "the" number — ADR 0011 rejects that
    outright, because nothing in the data says which number is primary and a
    wrong guess sends a rep to the wrong one.
    """
    bad = []
    for rel, path in sorted(_template_files()):
        for i, raw in enumerate(open(path, encoding='utf-8'), 1):
            if _PHONE.search(raw) and _HAND_SPLIT.search(raw):
                bad.append(f'{rel}:{i}  {raw.strip()}')
    assert not bad, "hand-rolled phone splits (use phone_entries):\n  " + "\n  ".join(bad)


# ── allowlist hygiene ────────────────────────────────────────────────────────

def test_allowlist_entries_still_apply():
    """A stale entry is worse than none: it hides a surface that has since been
    fixed or deleted, so the next real miss reads as intentional."""
    unguarded = {rel for rel, _, _, _, _ in _unguarded()}
    stale = []
    for rel in ALLOWED:
        if not os.path.exists(os.path.join(TEMPLATES, rel)):
            stale.append(f'{rel} (template no longer exists)')
        elif rel not in unguarded:
            stale.append(f'{rel} (no longer renders a raw phone)')
    assert not stale, "Remove these stale allowlist entries:\n  " + "\n  ".join(stale)


@pytest.mark.parametrize('rel', sorted(ALLOWED))
def test_every_exemption_carries_a_reason(rel):
    assert len(ALLOWED[rel]) > 40, f'{rel}: explain WHY it is exempt, in a sentence'


# ── the sweep's own coverage ─────────────────────────────────────────────────
#
# Nothing about reading a sweep reveals the shape it cannot see. Each SHAPE
# below exists (or plausibly could) in this app and must be flagged; each
# NOT_FLAGGED must stay invisible, or the sweep flags the whole tree and gets
# deleted for noise.

SHAPES = {
    'dotted':            '<div>{{ customer.phone }}</div>',
    'dotted_default':    "<td>{{ r.phone or '—' }}</td>",
    'subscript':         "<td>{{ row['phone'] }}</td>",
    'dict_get':          "<div>{{ orig.get('phone') }}</div>",
    'derived_name':      '<a href="tel:{{ primary_phone }}">x</a>',
    'prefixed_name':     '<div>{{ r.orig_phone }}</div>',
    'suffixed_name':     '<div>{{ c.phone_raw }}</div>',
    'js_interpolation':  '<script>el.innerHTML = `${c.phone}`;</script>',
    'js_dom_write':      '<script>el.textContent = o.buyer_phone;</script>',
    'across_lines':      '<div>{{ ci.phone\n   or "" }}</div>',
    'uppercase':         '<div>{{ c.Phone }}</div>',
    # A `{% set %}` launders the name: whatever it binds to has no "phone" in
    # it, so every downstream `{{ }}` is invisible. The site is the SET.
    'set_launders_name': "{% set nums = customer.phone|trim %}<div>{{ nums }}</div>",
    # A display rendered into an attribute other than value= — a tooltip is
    # something a reader sees, so it is a render, not a write path.
    'input_title_attr':  '<input readonly title="{{ c.phone }}">',
    'span_title_attr':   '<span title="{{ c.phone }}">…</span>',
}

NOT_FLAGGED = {
    'icon_class':        "<div>{{ 'bi-printer' if e.is_fax else 'bi-telephone' }}</div>",
    'js_icon':           '<script>h = `<i class="bi bi-telephone"></i>`;</script>',
    'guarded_pipe':      '{% for e in m.phone | phone_entries %}{{ e.text }}{% endfor %}',
    'guarded_js':        '<script>c.phone_entries.forEach(e => x(e.text));</script>',
    'entry_fields':      '<a href="tel:{{ e.dial }}">{{ e.text }}</a>',
    'if_guard':          '{% if customer.phone %}<div>x</div>{% endif %}',
    'plain_html':        '<option value="phone">โทร</option>',
    'form_input':        '<input name="phone" value="{{ ci.phone or \'\' }}">',
    'textarea':          '<textarea name="phone">{{ ci.phone }}</textarea>',
    'unrelated':         '<div>{{ customer.address }}</div>',
}


def _scan_snippet(tmp_path, body, name='rogue.html'):
    p = tmp_path / name
    p.write_text(body, encoding='utf-8')
    return _unguarded([(name, str(p))])


@pytest.mark.parametrize('shape', sorted(SHAPES))
def test_the_sweep_sees_every_rendering_shape(shape, tmp_path):
    """Feed the sweep one rogue surface per shape and demand it fire. A shape
    the sweep is blind to reads as coverage, which is worse than none."""
    assert _scan_snippet(tmp_path, SHAPES[shape]), \
        f'{shape}: the sweep is blind to this shape, so a surface using it is unswept'


@pytest.mark.parametrize('shape', sorted(NOT_FLAGGED))
def test_the_sweep_stays_quiet_on_what_is_not_a_raw_render(shape, tmp_path):
    assert not _scan_snippet(tmp_path, NOT_FLAGGED[shape]), f'{shape}: false positive'


def test_the_hand_split_detector_can_fail(tmp_path):
    """Break-it-once for the split detector, both spellings. `assert not bad`
    over an empty list pins nothing, and `.rsplit(',')` evaded the first
    version of the pattern entirely."""
    for expr in (".split(',')", ".rsplit(',')", '.split(",")'):
        assert _HAND_SPLIT.search('{{ c.phone%s[0] }}' % expr), expr
    # CONTROL: a split on something that is not a comma is not a phone split,
    # or the detector would flag every template that slices a date.
    assert not _HAND_SPLIT.search("{{ s.doc_no.rsplit('-', 1)[0] }}")


def test_a_rogue_surface_added_to_the_real_tree_would_be_caught(tmp_path):
    """The end-to-end proof: the sweep's own assertion, not just its scanner,
    goes red when a rogue surface joins the real template set."""
    rogue = tmp_path / 'rogue_customer_card.html'
    rogue.write_text('<div class="phone">{{ customer.phone }}</div>', encoding='utf-8')
    paths = list(_template_files()) + [('rogue_customer_card.html', str(rogue))]
    offenders = [(rel, line) for rel, line, _, _, _ in _unguarded(paths)
                 if rel not in ALLOWED]
    assert offenders == [('rogue_customer_card.html', 1)], offenders


def test_the_tel_check_can_fail(tmp_path):
    """Break-it-once for the dial guard, in the form the guard would regress
    into: the raw field back behind `tel:`."""
    raw = '<a href="tel:{{ m.phone }}">call</a>'
    ok = '<a href="tel:{{ e.dial }}">call</a>'
    pat = re.compile(r'tel:\s*(\{\{.*?\}\}|\$\{[^}]*\})', re.DOTALL)
    assert 'dial' not in pat.search(raw).group(1)   # would be flagged
    assert 'dial' in pat.search(ok).group(1)        # CONTROL: the fixed form passes
