# Sendy — Mobile Conventions (the wave contract)

> **Read this first, before touching any template in a mobile wave (W1–W7).**
> Phase P2 (`mobile-framework`) put the shared hooks in place: `base.html`,
> `static/css/app.css` (the `=== mobile framework ===` section), the role-aware
> bottom nav, and these conventions. A wave's job is to apply the checklist at
> the bottom to each page in its scope — **no new global CSS, no new JS**.

Sendy has no JS build step and we keep it that way. Everything here is **pure
Bootstrap 5.3 + the utility classes already in `app.css`**. If you find yourself
writing custom JavaScript or new global CSS for a layout problem, stop — the
recipe you need is almost certainly below.

---

## 1. Breakpoint

One breakpoint: **Bootstrap `lg` = 992px**. "Mobile" = `max-width: 991.98px`.

- Hide on mobile / show on desktop: `d-none d-lg-block` (or `d-lg-flex`, `d-lg-inline`…).
- Show on mobile / hide on desktop: `d-lg-none`.
- Project-named equivalents (same effect, clearer intent): `.hide-mobile`,
  `.hide-desktop` (defined in the framework CSS).

Do **not** introduce new breakpoints. The sidebar, bottom nav, and
`table-mobile-cards` all pivot at 992px; a second breakpoint desyncs them.

---

## 2. Page header actions (`.page-actions`)

`base.html` already wraps `{% block page_actions %}` in `<div class="page-actions">`
inside `.page-head`. **Just fill the block with buttons** — don't add your own
wrapper div.

```jinja
{% block page_actions %}
  <a href="{{ url_for('...') }}" class="btn btn-primary">
    <i class="bi bi-plus-lg me-1"></i>เพิ่ม
  </a>
{% endblock %}
```

### ≥3 buttons → collapse to a `⋯` dropdown under 992px

No custom JS. Provide **two markups**: the inline row for desktop
(`d-none d-lg-flex`) and a single Bootstrap dropdown for mobile (`d-lg-none`).

```jinja
{% block page_actions %}
  {# Desktop: full button row #}
  <div class="d-none d-lg-flex gap-2">
    <a href="{{ url_for('a') }}" class="btn btn-outline-secondary"><i class="bi bi-download me-1"></i>Export</a>
    <a href="{{ url_for('b') }}" class="btn btn-outline-secondary"><i class="bi bi-printer me-1"></i>พิมพ์</a>
    <a href="{{ url_for('c') }}" class="btn btn-primary"><i class="bi bi-plus-lg me-1"></i>เพิ่ม</a>
  </div>
  {# Mobile: one ⋯ menu #}
  <div class="dropdown d-lg-none">
    <button class="btn btn-outline-secondary btn-touch" type="button"
            data-bs-toggle="dropdown" aria-expanded="false" aria-label="ตัวเลือก">
      <i class="bi bi-three-dots-vertical"></i>
    </button>
    <ul class="dropdown-menu dropdown-menu-end">
      <li><a class="dropdown-item" href="{{ url_for('a') }}"><i class="bi bi-download me-2"></i>Export</a></li>
      <li><a class="dropdown-item" href="{{ url_for('b') }}"><i class="bi bi-printer me-2"></i>พิมพ์</a></li>
      <li><a class="dropdown-item" href="{{ url_for('c') }}"><i class="bi bi-plus-lg me-2"></i>เพิ่ม</a></li>
    </ul>
  </div>
{% endblock %}
```

- **1–2 buttons:** no dropdown needed. `.page-actions` wraps on mobile; just make
  sure the labels aren't so long they overflow (shorten or icon-only on mobile).
- The most important single action (usually the primary `btn-primary`) may stay
  visible on mobile *next to* the `⋯` if you prefer — keep the rest in the menu.

---

## 3. Tables — decision tree

**Never leave a bare table that overflows the viewport.** Pick one:

### (a) Row = one entity, ≤ ~5 meaningful columns → `table-mobile-cards`

The table flattens to a stack of cards on mobile. Put `data-label` on **every**
`<td>` (it becomes the field label). Mark the title cell `td-primary` and any
actions cell `td-actions`.

```html
<table class="table table-mobile-cards">
  <thead>
    <tr><th>สินค้า</th><th class="text-end">คงเหลือ</th><th class="text-end">ราคา</th><th></th></tr>
  </thead>
  <tbody>
    <tr>
      <td class="td-primary">{{ p.name }}</td>
      <td class="text-end" data-label="คงเหลือ">{{ p.qty }}</td>
      <td class="text-end" data-label="ราคา">{{ p.price }}</td>
      <td class="td-actions"><a class="btn btn-sm btn-outline-secondary" href="…">ดู</a></td>
    </tr>
  </tbody>
</table>
```

Cell helpers (all defined in `app.css`): `td-primary` (large title row, pinned
top), `td-actions` (right-aligned, pinned bottom), `td-hide-mobile` (dropped on
mobile — use for low-value columns). A `<td>` with **no** `data-label` and none
of those classes renders as a plain single-column value.

> `btn-sm` inside `td-actions` is acceptable (a list-row affordance, not a
> primary page action). The `btn-sm` ban in §4 is about the page's main actions.

### (b) Wide analytic / numeric table (many columns, not one-entity-per-row)

Wrap in `.table-responsive` — horizontal scroll **inside the table** is fine and
expected; it does not scroll the page body.

```html
<div class="table-responsive">
  <table class="table">…many columns…</table>
</div>
```

### (c) Choosing

| Situation | Use |
|---|---|
| Product list, customer list, doc lines — row is a "thing" | `table-mobile-cards` |
| Pivot / month-by-month / wide financial grid | `.table-responsive` |
| Anything else that would overflow | one of the above — never bare |

---

## 4. Forms

- **Single column on mobile:** `col-12 col-lg-6` (or `-4`, etc.). Never leave a
  multi-column grid row that squeezes inputs side-by-side under 992px.
- **16px input floor:** the framework CSS forces `font-size:16px` on
  `.form-control`/`.form-select` (incl. `-sm`) under 992px so iOS Safari doesn't
  auto-zoom on focus. You get this for free — don't fight it with inline styles.
- **Full-width submit on mobile:** add `.btn-block-mobile` (100% width <992px),
  or use Bootstrap's `d-grid`:

```html
<button type="submit" class="btn btn-primary btn-block-mobile">บันทึก</button>
```

---

## 5. Touch targets

- **≥44×44px** for any primary tap target on mobile.
- Buttons inside `.page-actions` are bumped to `min-height:44px` automatically on
  mobile. For a touch button elsewhere, add `.btn-touch`.
- **`.btn-sm` is banned for primary mobile actions** (submit, main CTA, page
  actions). It's fine for dense in-row affordances (`td-actions`, table links).

---

## 6. Role-gated buttons

Hide admin/manager-only controls from staff **and** keep them off the mobile
chrome when they're desktop-power-user tools:

```jinja
{% if session.role == 'admin' %}
  <a href="…" class="btn btn-outline-secondary hide-mobile">เครื่องมือผู้ดูแล</a>
{% endif %}
```

- Role check (`{% if session.role in ['admin','manager'] %}`) controls *who* sees
  it; `.hide-mobile` controls *where* (desktop only) for tools that make no sense
  on a phone. Use both when appropriate.
- The bottom nav is already role-aware (`build_mobile_nav_slots` in `app.py`):
  staff see only สินค้า/การค้า + เพิ่มเติม; never hand-add a nav slot whose
  landing page a role would 403 on.

---

## 7. Obsolete-button protocol — **waves never delete features**

On a phone some desktop controls are noise (bulk exports, multi-select tools,
power-user toggles). You may *hide* them on mobile, but **do not remove or
permanently disable** anything without Put's sign-off.

When you think a control is obsolete on mobile (or entirely):

1. Keep it working. Hide on mobile with `.hide-mobile` if it's desktop-only.
2. Mark it for review with a Jinja comment **right above it**:
   ```jinja
   {# OBSOLETE-CANDIDATE: bulk CSV export — unused on phones, confirm before removing #}
   ```
3. **List every `OBSOLETE-CANDIDATE` in the PR body** under a heading so Put can
   confirm. Removal happens in a *follow-up* only after he says yes.

This keeps waves reversible: hiding is safe, deleting is a decision.

---

## 8. Service-worker cache bump

Any change to a file under `static/` (CSS, JS, icons) **must bump the cache
version** so installed PWAs don't serve a stale asset:

- Edit `static/sw.js` → change `const CACHE = 'sendy-vN'` to the next N.
- A wave that only edits `.html` templates does **not** need a bump (templates
  aren't cached — the SW only stale-while-revalidates `/static/*`).
- A wave that touches `app.css` (e.g. adds a one-off rule) **does** need a bump.

---

## Per-page checklist (paste into every wave PR)

For **each** page touched:

- [ ] Renders at **375px** with **no horizontal body scroll**.
- [ ] Header actions use `.page-actions`; ≥3 buttons collapse to a `⋯` dropdown (§2).
- [ ] Every table follows the decision tree (§3) — `table-mobile-cards` or
      `.table-responsive`, never bare-overflowing.
- [ ] Forms are single-column on mobile; submit is full-width; no <16px inputs (§4).
- [ ] Primary actions are ≥44px touch targets; no `btn-sm` on primary actions (§5).
- [ ] Role-gated/desktop-only controls hidden appropriately (§6).
- [ ] Any removed-feeling control marked `{# OBSOLETE-CANDIDATE: … #}` and listed
      in the PR body (§7) — nothing actually deleted.
- [ ] Thai labels intact (no accidental English / lost copy).
- [ ] `curl -s -o /dev/null -w '%{http_code}\n'` on each touched route = 200/302.
- [ ] `static/` changed? → bumped `CACHE` in `sw.js` (§8).
- [ ] 375px screenshot attached to the PR.

### Wave process

1. Read this doc.
2. `find inventory_app/templates/<scope>` for the exact page list.
3. Apply the checklist per page.
4. `sendy-down && sendy-up`; `curl` every touched route.
5. `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest -x -ra` green.
6. 375px screenshots + `OBSOLETE-CANDIDATE` list in the PR body.
