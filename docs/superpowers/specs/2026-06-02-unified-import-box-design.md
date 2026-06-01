# Unified Import Box (`/import`) — Design

**Date:** 2026-06-02
**Status:** Approved (design); spec under review
**Goal:** Replace the scattered weekly-import buttons with ONE page where Put drags the week's Express exports, the box auto-detects each report, previews the diff, and writes only to canonical tables.

## Why

Sendy keeps the same BSN truth in parallel copies fed by several import buttons that must be kept in lockstep manually — and currently are not (the express mirror froze at 2026-04-30 while finance's tables stayed current). Put is the only operator; one box that "just knows" what each file is removes a class of manual error and is a step toward one source of truth.

Decisions (Put, 2026-06-02): **Full consolidation** (write canonical only; retire the `express_sales`/`express_payments_in` twins); **scope = Express weekly set** (marketplace stays on `/marketplace`); **auto-detect from report header + confirm**; **multi-file batch** upload.

## Approach: thin dispatcher over existing importers

`/import` is a **routing layer**, not a new parser. It detects each file's report type and calls the existing, already-tested canonical importers. Rejected: a unified mega-parser (high risk re-deriving proven parsers; YAGNI).

## Scope

### In (this spec — sub-project A)
- New routes (GET drop zone + POST preview) and a confirm POST. **Implemented at `/import-data` and `/import-data/confirm`** — `/import` was already taken by `bp_products` (Product-Master CSV import). Spec text below says `/import` generically; the live paths are `/import-data*`. Endpoints: `unified_import`, `unified_import_confirm`.
- `detect_express_report(path)` — report-type detection.
- Multi-file preview table + per-file confirm, dispatching to canonical importers.
- Nav points to `/import`; old import pages remain reachable (not yet removed).

### Follow-up (sub-project B — separate spec/PR)
- Retire the twins: stop the `express_sales` / `express_payments_in` writers.
- Re-point the one remaining twin reader (`/express/ar/customer` payment-history panel → `received_payments`).
- Redirect/retire the old import routes.

### Out of scope
- Marketplace order import (`/marketplace` stays separate — Seller-Center files, different cadence).
- `models._topup_pre_feb_for_product` (operates only on the pre-Feb frozen window; untouched).
- AR/AP snapshot data model — unchanged; they keep landing in `express_ar_outstanding` / `express_ap_outstanding` (kept canonical tables, NOT twins).

## Components

### 1. `detect_express_report(path) -> ReportType`
Reads the file's Thai title line (Express prints a distinct report title in the first ~3 lines, cp874) and returns one of: `sales`, `purchase`, `payments_in`, `credit_notes`, `ar_snapshot`, `ap_snapshot`, or `unknown`.
- Primary signal: title-line substring match (e.g. `รายงานการรับชำระหนี้` → `payments_in`; `ใบลดหนี้` → `credit_notes`; `ลูกหนี้คงค้าง` → `ar_snapshot`; `เจ้าหนี้คงค้าง` → `ap_snapshot`; sales/purchase per their report titles).
- Fallback hint: filename prefix (`ขาย_`, `ซื้อ_`, `การรับชำระหนี้_`, …).
- Returns a confidence flag; `unknown`/low-confidence rows force Put to pick the type from a dropdown before confirm.
- Pure function, unit-testable with header fixtures. No DB, no writes.

### 2. Routing map (report → importer → canonical table)
Detection found 8 distinguishable report types (the two ใบลดหนี้ differ by รับคืน=AR vs ส่งคืน=AP). All have a clear canonical importer:

| Detected type | Header marker | Existing importer reused | Canonical table |
|---|---|---|---|
| `sales` | ประวัติการขาย | `import_weekly` (parse_weekly) | `sales_transactions` |
| `purchase` | ประวัติการซื้อ | `import_weekly` (parse_weekly) | `purchase_transactions` |
| `payments_in` | การรับชำระหนี้ | `models.import_payments` | `received_payments` + `paid_invoices` |
| `payments_out` | การจ่ายชำระหนี้ | `express_importer payments_out` | `express_payments_out` *(kept)* |
| `credit_notes_ar` | ใบลดหนี้/รับคืนสินค้า | `import_credit_notes` | `credit_note_amounts` *(kept)* |
| `credit_notes_ap` | ใบลดหนี้/ส่งคืนสินค้า | `express_importer credit_notes` | `express_credit_notes` *(kept)* |
| `ar_snapshot` | ลูกหนี้คงค้าง | `import_weekly` (express snapshot) | `express_ar_outstanding` *(kept)* |
| `ap_snapshot` | เจ้าหนี้คงค้าง | `import_weekly` (express snapshot) | `express_ap_outstanding` *(kept)* |

The box **never** routes to `parse_express_sales`/`express_sales` or the `express_payments_in` writer — `sales` and `payments_in` go to their canonical homes (`sales_transactions`, `received_payments`). The other 6 express_* tables are single-source (not twins) and are kept. So the only twins retired (sub-project B) are `express_sales` + `express_payments_in` (+ refs).

Detection keys on specific titles (`ประวัติการขาย`, not bare `ขาย`) so the wrong `ขายเงินเชื่อ เรียงตามเลขที่` report stays `unknown` → operator picks. Implemented in `import_router.detect_express_report` (10 TDD tests).

### 3. `/import` flow (multi-file, preview → confirm)
1. **GET `/import`** — drop zone (multiple files) + recent-imports list.
2. **POST `/import`** (preview) — for each uploaded file: save to a temp dir, detect type, run that importer's **preview/dry-run** to compute the diff (rows to add / update / skip). Render a table: `filename · detected type (editable) · period · add/update/skip counts · warnings`. Persist the staged files + detected types under a short-TTL token (mirrors the existing CN-preview pattern).
3. **POST `/import/confirm`** — for each staged file, dispatch to its importer with the (possibly overridden) type and commit. Each importer keeps its own idempotency/transaction. Render a per-file result summary.

### Preview capability per importer
- `import_weekly`, `import_credit_notes`: already have preview/confirm — reuse.
- `import_payments`: idempotent upsert; add a thin dry-run wrapper (parse + classify new-vs-existing by `re_no`, count, rollback) so it shows add/update counts without committing.
- AR/AP snapshots: `express_importer` has a `dry_run` parse-count; surface the count (snapshots replace-by-date, so the preview states "replaces snapshot for <date>").

## Data flow
File(s) → `detect_express_report` → staged (temp + token) → per-importer preview (read-only/rolled-back) → Put confirms → per-importer commit to canonical table → result summary. No new tables; no schema migration. Auth: admin/manager only (matches existing import routes); CSRF token on both POST forms.

## Error handling
- `unknown`/low-confidence detection → row is blocked from confirm until Put picks a type (no silent guess).
- A file whose chosen importer raises during preview → shown with the error, excluded from confirm; other files proceed (per-file isolation).
- Confirm is per-file transactional: one file failing rolls back only that file (SAVEPOINT / the importer's own transaction), reported in the summary; the batch is not all-or-nothing.
- cp874 decode failure → surfaced as a per-file error, not a 500.

## Testing (TDD)
- `detect_express_report`: header-fixture tests for all 6 types + `unknown` (one tiny cp874 sample per report title). The decisive RED-first test.
- Routing: each detected type dispatches to the expected importer (assert via a seam / spy, not by re-importing).
- Multi-file preview: a batch of 2–3 files returns one preview row per file with correct detected types.
- Override: a misdetected type can be overridden at confirm and routes accordingly.
- Smoke (live `tmp_db` copy): a real `การรับชำระหนี้` file previews add/update counts without writing; confirm writes to `received_payments`.
- After adding routes: `sendy-down && sendy-up` + `curl` the new URLs (200/302, not 500) — werkzeug URL-map gotcha.

## Rollout
- Restart Sendy after adding routes (in-memory URL map).
- Nav: add "นำเข้าข้อมูล (/import)" entry; leave old pages reachable this PR.
- Sub-project B retires the twins + old routes once A is proven in real weekly use.
