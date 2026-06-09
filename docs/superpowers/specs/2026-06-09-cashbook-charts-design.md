# Cashbook dashboard — charts (monthly trend + expense breakdown)

**Date:** 2026-06-09
**Project:** Sendy ERP (`sendy_erp/inventory_app`)
**Page:** `/cashbook/` (`blueprints/cashbook.py` + `templates/cashbook/dashboard.html`)
**Status:** Design approved by Put 2026-06-09. Builds on the drill-down modal (branch `cashbook-drilldown` / PR #124).

## Goal

Add two Chart.js visualizations to the cashbook dashboard, above the existing tables: a monthly income/expense trend and an expense-by-category breakdown. Both reuse the drill-down modal — clicking a bar/segment opens the same transaction detail popup.

## Chart A — Monthly trend

- **Type:** mixed chart — grouped **bars** per month (income green `#2e7d3a`, expense red `#c41e2a`) + a **net line** (income − expense) overlaid on the same THB axis.
- **Data:** existing `_get_monthly_summary(conn, exclude_transfer=True)` → `[{month, income, expense}]` (already passed to the template as `monthly`). Net computed in JS per point.
- **Clickable:** clicking a month's bar opens the drill-down modal `dim=month, key=<YYYY-MM>` (same as clicking the monthly table row).
- **Guard:** only rendered when `monthly` is non-empty.

## Chart B — Expense breakdown by category

- **Type:** **donut**, Top 7 categories by total + the remainder folded into a single **"อื่นๆ"** slice.
- **Data:** new pure helper `_expense_topn(expense_cats, n=7)` over the existing `expense_cats` (from `_get_category_summary`, already sorted by total desc). Passed to the template as `expense_chart`.
- **Clickable:** clicking a category slice opens the drill-down modal `dim=expense_category, key=<category>`. The **"อื่นๆ"** slice is NOT clickable (it aggregates many categories).
- **Guard:** only rendered when `expense_chart` is non-empty.

### Helper `_expense_topn(expense_cats, n=7)`
Pure, testable. `expense_cats` is the list of `{category, total}` already sorted desc.
Returns `[{category, total}]`: the first `n` rows verbatim, plus a single `{"category": "อื่นๆ", "total": <sum of the remaining rows>}` appended only when there are more than `n` categories. The grand total is preserved (sum of output == sum of input).

## Clickable integration (reuse drill-down)

Refactor the existing modal-open logic in `dashboard.html` into a single reusable JS function `openCbDetail(dim, key, label)`. Both consumers call it:
- existing `[data-cb-dim]` table rows (rebind to call `openCbDetail`),
- Chart.js `onClick` handlers for chart A (month) and chart B (category, skipping "อื่นๆ").

This keeps one fetch/modal code path. No change to the `/cashbook/api/detail` endpoint.

## Placement

A new two-column charts row inserted **after the headline P&L cards + disclosure note** and **before the per-account table**:
- left `col-lg-6`: Chart A (monthly trend), in a `card`.
- right `col-lg-6`: Chart B (expense donut), in a `card`.

Existing tables stay below, unchanged (charts = at-a-glance, tables = exact numbers + drill-down).

## Tech

- Chart.js 4.4.4 via the existing CDN pattern (`<script src=...chart.umd.min.js>` inside `{% block scripts %}`, same as `trade_dashboard.html`).
- Chart data passed from the route to the template and emitted with Jinja's `|tojson` into the script.
- No DB migration (read-only over existing aggregates). No new route.

## Testing

`tests/test_cashbook_charts.py` (new):
- **`_expense_topn`:** (a) ≤7 categories → returned as-is, no "อื่นๆ"; (b) >7 → exactly 7 + one "อื่นๆ"; (c) grand total preserved at full float precision; (d) "อื่นๆ" total == sum of the tail.
- **Render (admin_client):** dashboard HTML contains both `<canvas>` ids (`cbTrendChart`, `cbExpenseChart`), the Chart.js CDN script, and the emitted data (e.g. a known month / category string), and still contains `openCbDetail` (refactor didn't break the modal).

(Chart rendering itself is client-side JS — covered by the manual browser check, not unit tests.)

## Files touched

- `inventory_app/blueprints/cashbook.py` — add `_expense_topn`; pass `expense_chart` to the template from `dashboard()`.
- `inventory_app/templates/cashbook/dashboard.html` — charts row markup (2 canvases); refactor modal JS into `openCbDetail`; add Chart.js init for both charts with onClick → `openCbDetail`.
- `tests/test_cashbook_charts.py` — new.

## Out of scope

By-person/location chart, income-category chart, date-range filter, chart export. Charts read the same all-time aggregates as the tables.
