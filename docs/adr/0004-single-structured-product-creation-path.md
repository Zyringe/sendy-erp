# Single canonical structured path for product creation

## Status

accepted

## Context

Products could be created four ways: the hand form (`/products/new`), Smart Suggest
approval on `/mapping`, a legacy admin quick-create (`/mapping` action `new`), and the
CSV product-master importer (`/import`, `bulk_import_products`). Only Smart Suggest
produced a **structured product** (spec columns filled → derived `sku_code`); the other
three produced **bare products** (free-text name only, no `sku_code`). The catalog was
therefore inconsistent, and even Smart Suggest didn't itself generate the `sku_code` —
that came from a separate batch backfill via the naming engine.

## Decision

Consolidate all manual product creation onto **one canonical structured path**: type a
raw name → the existing `parse_sku_names.parse_name` parser fills the spec fields →
admin reviews/edits → the naming engine (`build_name` + `regenerate_for_product`, which
already auto-suffixes `-<id>` on a `sku_code` collision) produces the derived name +
`sku_code` → insert. Both the rebuilt hand form and Smart Suggest approval call this one
function, which closes the Smart-Suggest `sku_code` gap.

Every product also gets a `created_via` provenance stamp (`manual` / `smart_mapping` /
`legacy`), backfilled `legacy` for the ~1,958 existing rows and shown as a badge on the
product detail page.

Removed in the same change: the legacy admin quick-create path, the CSV master importer
entirely (button + `csv_import`/`csv_import_confirm` routes + `bulk_import_products` +
`_parse_csv_content` + `import.html`), and the unused **stock-in / stock-out** buttons and
their routes (`stock_in`/`stock_out` + `transactions/stock_form.html`) — stock movements
happen via the weekly import and the ปรับ (stock-adjust) flow, so those buttons were dead.

## Considered options

- **Keep the CSV importer** (leave it out of scope). Rejected: it re-opens the bare-product
  side door, and Put doesn't use it — deleting it makes the structured form the only way
  in. Bulk fixes, if ever needed, go through other tooling.
- **Strictly derived (read-only) name.** Rejected in favour of an **editable** derived name
  (parity with Smart Suggest) because the auto-builder can't always express the ideal name.
- **Route manual creation through staging/approval** like Smart Suggest. Rejected: the hand
  form is admin-only and immediate; the admin is the reviewer, so self-approval staging is
  pointless friction. Staging exists in Smart Suggest only because *staff* propose.

## Consequences

- New products get consistent, collision-safe `sku_code`s going forward; Smart Suggest
  approvals now generate their `sku_code` at creation instead of relying on a later backfill.
- The `/import` product-master CSV workflow no longer exists.
- `created_via` is provenance only — not a permission, status, or lifecycle state.
- Unchanged: the weekly Express transaction import (`/import-data`) and the marketplace/BSN
  mapping paths. `created_via` for CSV-created historical rows is folded into `legacy`.
