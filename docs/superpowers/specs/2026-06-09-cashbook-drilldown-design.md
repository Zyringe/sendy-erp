# Cashbook dashboard — click-to-detail (drill-down modal)

**Date:** 2026-06-09
**Project:** Sendy ERP (`sendy_erp/inventory_app`)
**Page:** `/cashbook/` (`blueprints/cashbook.py` + `templates/cashbook/dashboard.html`)
**Status:** Design approved by Put 2026-06-09. Precursor cleanup (remove ธุรกิจ-vs-ส่วนตัว section) already shipped to the working tree.

## Goal

On the cashbook dashboard, let Put click a summary row and see the individual
transactions behind that number, in a popup, without leaving the page. The detail
total must reconcile exactly with the dashboard figure that was clicked.

## In scope — 4 clickable dimensions

| Clicked row | Detail shows | Reconciles to |
|---|---|---|
| row in **รายรับตามหมวดหมู่** (`income_cats`) | all income txns in that category | the row's ฿total |
| row in **รายจ่ายตามหมวดหมู่** (`expense_cats`) | all expense txns in that category | the row's ฿total |
| row in **ค่าใช้จ่ายตามผู้ใช้/สถานที่** (`tag_summary`) | all expense txns with that `user_category` | the row's ฿total |
| **month** row in **สรุปรายเดือน** (`monthly`) | all txns in that month (both directions) | the row's รับ + จ่าย columns |

**Out of scope (this iteration):** charts/visualizations, headline P&L cards,
per-account table (already links to `account_ledger`), transfer-account table,
editing transactions from the modal, CSV export of a drill-down.

## Scope/filter semantics (must match the dashboard exactly)

Every drill-down reuses the dashboard's operating scope so the detail sums to the
shown figure:

- Exclude transfer accounts: `cashbook_accounts.is_transfer = 0`
- Exclude transfer categories: `COALESCE(t.category,'') NOT IN (TRANSFER_CATEGORIES)`
  via the existing `_tcat_ph()` helper. (`TRANSFER_CATEGORIES = ("เงินทุน/เงินโอน",)`.)
- Per dimension:
  - `income_category`  → `direction='income'`  AND `COALESCE(category,'(ไม่ระบุ)') = key`
  - `expense_category` → `direction='expense'` AND `COALESCE(category,'(ไม่ระบุ)') = key`
  - `user_tag`         → `direction='expense'` AND `user_category = key`
  - `month`            → `strftime('%Y-%m', txn_date) = key` (both directions)

> Note the `(ไม่ระบุ)` alias: for the income/expense_category dims the detail query
> must use the **identical** key expression as `_get_category_summary` —
> `COALESCE(t.category, '(ไม่ระบุ)') = key` — so a NULL category is reachable via
> `key='(ไม่ระบุ)'` and the grouping matches the summary by construction (do not
> hand-roll `IS NULL OR = ''`, which would diverge from the summary on empty strings).

## Backend

### Helper — `_get_detail_rows(conn, dim, key)`
Pure, unit-testable. Returns `(rows, summary)`:
- `rows`: list of dicts ordered by `txn_date DESC, id DESC`, each with
  `txn_date, account_code, account_owner_name, direction, category,
  user_category, amount, amount_display (_fmt_baht), note`.
- `summary`: dict with `count`, and totals as both raw float and `_fmt_baht`:
  - category/tag dims → `total`, `total_display`
  - month dim → `income`, `income_display`, `expense`, `expense_display`
- Raises `ValueError` on an unknown `dim` (fail-loud, no silent default).

### Route — `cashbook.detail_api`
```
GET /cashbook/api/detail?dim=<dim>&key=<value>
```
- Validates `dim` against the whitelist and that `key` is present → `abort(400)` on failure.
- Calls the helper, returns `jsonify({rows, summary, dim, key})`.
- Read-only GET: no CSRF needed; access controlled by the existing blueprint
  `before_request` (admin + manager allowed, staff redirected, anon → login).

## Frontend (`dashboard.html`)

- The 4 summary tables: each drillable `<tr>` gets `data-cb-dim` + `data-cb-key`,
  `role="button"`, `style="cursor:pointer"`, a hover highlight, and a faint
  `bi-chevron-right` affordance so it reads as clickable.
- One Bootstrap modal (markup already available via `bootstrap.bundle.min.js` in
  `base.html`): title (dimension + key), summary line in the header, a `<tbody>`
  filled by JS, a spinner while loading, and an error message on fetch failure.
- One delegated JS click handler on `[data-cb-dim]`:
  1. open modal with spinner + the clicked label,
  2. `fetch('/cashbook/api/detail?dim=..&key=..')`,
  3. inject rows; show `รวม ฿X · N รายการ` (category/tag) or
     `รับ ฿X · จ่าย ฿Y · N รายการ` (month) in the header,
  4. on error show an inline message, not a blank modal.
- Server returns pre-formatted `฿` strings; JS does no number formatting.
- Modal table columns: **วันที่ / บัญชี / รับ-จ่าย / หมวด / ป้าย / จำนวน / โน้ต**.
  รับ/จ่าย shown as a colored badge (green รับ / red จ่าย).

## Reconciliation property (the trust guarantee)

For every dimension, `SUM(detail rows.amount) == the dashboard aggregate for that
key`. This is the core correctness property and is asserted in tests, at full
float precision (no rounded-display comparison).

## Testing

`tests/test_cashbook_detail.py` (new), seeding cashbook accounts + transactions
including a transfer account and a `เงินทุน/เงินโอน` row that must be excluded:

- **Reconciliation, per dim:** helper total == the matching dashboard helper's
  figure (`_get_category_summary`, `_get_tag_summary`, `_get_monthly_summary`),
  for income_category, expense_category, user_tag, and month (income+expense).
- **Scope:** a transfer-account txn and a transfer-category txn never appear in
  any drill-down.
- **`(ไม่ระบุ)` round-trip:** a NULL-category income txn is reachable via
  `key='(ไม่ระบุ)'`.
- **Route:** 200 + JSON shape for a valid dim; `400` for an unknown dim / missing
  key; anon request redirects (not 200).

## Files touched

- `inventory_app/blueprints/cashbook.py` — add `_get_detail_rows` + `detail_api` route.
- `inventory_app/templates/cashbook/dashboard.html` — clickable rows + modal + JS.
- `tests/test_cashbook_detail.py` — new test module.

No DB migration (read-only feature over existing tables). Restart sendy after
adding the route (dev server runs `use_reloader=False`); verify
`/cashbook/api/detail?dim=expense_category&key=...` returns 200 for an authed session.
