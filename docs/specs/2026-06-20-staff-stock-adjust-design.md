# Staff stock-adjust + tick-mark reasons + count-date lock — design

- **Date:** 2026-06-20
- **Status:** approved (design), pending implementation
- **Scope:** the **ปรับ** (stock-adjust) flow in Sendy only. รับเข้า/จ่ายออก unchanged.
- **Migration:** none (reuses existing columns).

## Goal

1. Let **staff + manager + admin** (all logged-in roles) adjust a product's stock via the **ปรับ** button — currently admin-only.
2. Replace the free-text reason box with a **tick-mark (radio, pick one)** reason picker.
3. Add a **date** field to the adjustment; ticking **นับสต๊อก** locks the date to the real adjustment moment.

## Decisions (from Q&A 2026-06-20)

| # | Decision |
|---|---|
| Permissions | All logged-in roles (staff + manager + admin) can adjust. รับเข้า/จ่ายออก stay admin-only. |
| Reasons | 6 presets, pick one (radio). `อื่นๆ` reveals a free-text box. |
| Reason storage | Reuse existing `transactions.note` (TEXT). No new column. Upgrade path: add a structured `reason` column later if a "show all stock-counts" report is ever needed. |
| Date | Add `<input type="date">`. Backdatable for non-count reasons; **นับสต๊อก** → locked to today / now. |
| Backdate time-of-day | `00:00:00`, used **only** when the time is genuinely unknown (a true past date). A non-count adjustment dated *today* stamps the real time. |
| Entry UX | The detail-page **ปรับ** button opens a **modal** (consistent with the alerts page, which is already a modal) instead of navigating to a separate full-page form. |

## Reason map

`reason` form code → label stored in `note`:

| code | label stored in `note` |
|---|---|
| `count` | `นับสต๊อก` |
| `damaged` | `ชำรุด / แตกหัก` |
| `lost` | `สูญหาย` |
| `sample` | `ของแถม / เบิกใช้เอง` |
| `correction` | `แก้ยอดผิด` |
| `other` | *(staff-typed text, stored verbatim)* |

## Touchpoints (exact)

| File | Change |
|---|---|
| `inventory_app/app.py:191` (`_STAFF_POST_OK`) | add `'stock_adjust'` (manager inherits via `_MANAGER_POST_OK = _STAFF_POST_OK | {...}`). |
| `inventory_app/app.py:1223` (`def stock_adjust`) | parse `reason` + `adjust_date`; map reason→note; compute `created_at`; pass to `add_transaction`. Pass `today` to the GET render. |
| `inventory_app/models.py:422` (`add_transaction`) | add optional `created_at=None` param; include in INSERT only when not None (None ⇒ DB default `datetime('now','localtime')`). |
| `inventory_app/templates/transactions/_adjust_fields.html` | **new** shared partial: current-stock context line + new-qty + reason radios + `อื่นๆ` text box + date input + toggle JS. Used by both modals (+ fallback page). |
| `inventory_app/templates/products/detail.html:85-107` | split the footer: keep รับเข้า/จ่ายออก under `{% if is_admin %}`; render **ปรับ** to all logged-in roles as a **modal trigger** (`data-bs-toggle="modal" data-bs-target="#adjustModal"`). Add an `#adjustModal` (Bootstrap 5, already loaded app-wide) on the page whose `<form>` POSTs to `stock_adjust` with hidden `next` = detail page and includes the shared partial. |
| `inventory_app/templates/alerts.html:39, 68-112` | ungate the ปรับสต็อก button + modal (all roles); swap the free-text `note` in the modal for the shared partial. |
| `inventory_app/templates/transactions/adjust_form.html` | keep as a **fallback** full page for direct GET to `/products/<id>/adjust` (non-JS / bookmarked); switch its body to the shared partial so all three stay in sync. Primary UX is the modal. |

## Detailed behaviour

### Permissions
- `before_request` (app.py:505) blocks POST when `role` not in the whitelist. Adding `stock_adjust` to `_STAFF_POST_OK` lets staff + manager POST. GET (the form) is already allowed for any logged-in role.
- Templates currently hide the button behind `{% if is_admin %}`. Every page already requires login (before_request), so the **ปรับ** button can render unconditionally; รับเข้า/จ่ายออก remain inside the `is_admin` block.

### Modal entry (detail page)
- The **ปรับ** button is a modal trigger (`#adjustModal`), not a link. The modal `<form method="post" action="{{ url_for('stock_adjust', product_id=...) }}">` carries `csrf_token` + hidden `next` (= detail page) and includes the shared partial.
- Submit → server validates → `redirect(next)` with a flash (same non-AJAX pattern the alerts modal already uses). On a validation error the modal closes and the flash explains why; the user re-opens it. No AJAX needed.
- Bootstrap 5 JS/CSS is already loaded app-wide (the alerts page uses modals), so no new assets.

### Server logic (`stock_adjust` POST)
```
reason = form['reason']                      # one of the 6 codes
if reason not in REASON_LABELS and reason != 'other': reject

if reason == 'other':
    note = form['note_other'].strip()
    if not note: reject('กรุณาระบุเหตุผล')
else:
    note = REASON_LABELS[reason]

# date / created_at
today = date.today().isoformat()
if reason == 'count':
    created_at = None                        # DB default = now (authoritative; submitted date ignored)
else:
    adjust_date = form['adjust_date'].strip()
    parse YYYY-MM-DD; reject on bad format or adjust_date > today
    created_at = None if adjust_date == today else f"{adjust_date} 00:00:00"

# existing validation
new_qty = int(form['new_quantity']); reject if < 0
diff = new_qty - get_current_stock(pid); if diff == 0: info('ไม่มีการเปลี่ยนแปลง')

add_transaction(pid, 'ADJUST', diff, 'unit', note=note, created_at=created_at)
```
- `created_at = None` ⇒ `add_transaction` omits the column ⇒ DB default `datetime('now','localtime')`. This covers both `count` and non-count-dated-today, so both record the true moment.
- Stock math is unaffected by date: the `after_transaction_insert` trigger updates `stock_levels` from `quantity_change` only.

### Client toggle (in the partial)
- Reason radios; default none selected (force a choice) — or default `count` (most common). **Default `count`.**
- On `change`:
  - `other` → show the `note_other` text box (required); else hide.
  - `count` → set date input = today and `disabled` (visual lock) + hint "ล็อกเป็นวันนี้"; else enable. (Server ignores the date for `count` regardless — JS is convenience only.)
- Date input: `type="date"`, default today, `max` = today.

## Data / no migration

- `transactions` already has `note` (TEXT) and `created_at` (`TEXT NOT NULL DEFAULT (datetime('now','localtime'))`). No schema change.
- `add_transaction`'s new `created_at` param is appended with a default of `None`; the other two callers (IN/OUT) are unaffected.

## Backdating semantics (honest note)

`stock_levels` holds only the **current** quantity. Backdating an adjustment changes which period it appears in for **transaction history**, not the current stock number — there are no historical stock snapshots to rewrite. This is the intended effect of the date field. ADJUST rows carry no cost, so WACC (purchase-driven) is unaffected.

## Testing (TDD — write first)

`tests/test_stock_adjust.py` (CSRF off via conftest; log in by setting `session['role']`):
1. **staff allowed** — staff POST adjust → 302, one `ADJUST` row created.
2. **manager allowed** — same for manager.
3. **count ignores backdate** — `reason=count` + `adjust_date`=past → `created_at` date == today (now), not the past date; `note == 'นับสต๊อก'`.
4. **other requires text** — `reason=other`, empty `note_other` → rejected, **no** row.
5. **backdate lands on date** — `reason=damaged`, `adjust_date`=past → row `created_at` starts with that date + ` 00:00:00`; `note == 'ชำรุด / แตกหัก'`.
6. **today non-count = now** — `reason=correction`, `adjust_date`=today → `created_at` ≈ now (has real time, not `00:00:00`).
7. **future date rejected** — `adjust_date` > today → rejected, no row.
8. **invalid reason rejected** — bogus `reason` → rejected, no row.
9. **diff == 0 no-op** — new qty == current → info flash, no row.

## Rollout / verification gate (Sendy = merge auto-deploys to prod)

1. Tests green (`cd sendy_erp && pytest tests/test_stock_adjust.py`).
2. Run real local Sendy (`sendy-up`), `curl` `GET /products/<id>/adjust` (200) — restart after route/template edits.
3. Exercise the real POST: as a **staff** session, submit นับสต๊อก and a backdated `ชำรุด`; confirm rows + dates in `transactions`.
4. Put does the 30-sec click-through (radio toggle, date lock) before merge.
5. After merge: prod `/healthz` 200 + the route loads.

## Out of scope

- รับเข้า/จ่ายออก permissions (stay admin-only).
- Per-SKU "last counted date" tracking (was the alternative date interpretation; not chosen).
- A structured `reason` column / reason-based reporting.
