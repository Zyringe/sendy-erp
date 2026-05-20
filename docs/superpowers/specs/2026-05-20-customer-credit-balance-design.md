# Customer-Credit-Balance Section on `/cashflow`

**Date:** 2026-05-20
**Status:** Approved by Put, ready for implementation plan
**Origin:** [[project_2026_05_20_resume_here]] open item #1 — "Customer-credit-balance page on /cashflow"

## Problem

After the VAT-baseline fix in PR #27 (commit `339e92a`), the true whole-company
customer-credit balance is ≈฿372 across 10 invoices — money the customer
overpaid that we (notionally) still owe back. The only material entry is
**IV6800219 หน้าร้านS ฿290** (paid full then returned 1 unit; CN issued,
counter likely settled cash). All others are ≤฿1 rounding / counter
artifacts plus two small ones (เส็งหลี ฿40.61, เมืองคงวัสดุ ฿35).

This data exists only as a CSV export and the `over_credited` flag in
`payments_alloc._reconcile()`. There is no screen — Put has no in-app way to
audit "what credits do we owe customers?" alongside the AR aging view.

## Goal

Add a read-only section on `/cashflow` that lists invoices where
`outstanding < 0` (customer overpaid), filtered to material rows by default
and unlockable to see everything. Existing `payments_alloc.invoice_settlement()`
already computes the per-invoice reconciliation — the new section reuses it.

## Non-Goals

- **No DB schema change.** No migration, no new tables, no new columns.
- **No mark-resolved / acknowledge / outreach log.** Read-only only.
- **No standalone `/accounting/customer-credit` page.** Section embedded in
  `/cashflow`. If volume grows, can extract later.
- **No mobile (`/m/*`) variant.** Desktop only for v1.
- **No CSV export from the section.** CSV already exists; this section is
  in-app visibility, not a data-pipe.

## Design — Approach A (chosen)

A small pure helper in `payments_alloc.py` filters
`invoice_settlement()` output; `cashflow_dashboard()` calls it and passes
the rows to `cashflow.html`, which renders a new card under the AR aging
section. (Approaches B/C — standalone route, inline filter — rejected:
B is overkill for ≤10 rows; C couples view to payments_alloc internals.)

### 1. Backend helper — `payments_alloc.customer_credit_rows()`

```python
def customer_credit_rows(threshold: float = 5.0,
                        as_of: Optional[str] = None,
                        conn: Optional[sqlite3.Connection] = None,
                        db_path: Optional[str] = None) -> list[dict]:
    """Per-invoice list where the customer overpaid (outstanding < 0).

    Filters `invoice_settlement()` for invoices whose reconciled
    `outstanding` is strictly negative AND |outstanding| >= threshold.
    Pass `threshold=0` to surface everything down to the _EPS clamp.

    Sort: credit DESC (largest first), then invoice_date DESC tiebreak.

    Returns dicts with keys:
        doc_base, customer, customer_code, invoice_date,
        billed, credit_notes, collected,
        credit            # = -outstanding, always positive in this list
        days_old          # int, today - invoice_date
    """
```

- Reuses `invoice_settlement()` — no new SQL.
- `credit = -outstanding` so the template never has to negate.
- `days_old` computed in Python (date today() − invoice_date).
- `_EPS` clamp inherited from `_reconcile()`; rows that round to zero
  never appear regardless of threshold.

### 2. Route — `cashflow_dashboard()` in `app.py`

```python
show_all = request.args.get('show_all') in ('1', 'true', 'on')
credit_threshold = 0.0 if show_all else 5.0
credit_rows = pa_mod.customer_credit_rows(threshold=credit_threshold)
credit_total = round(sum(r['credit'] for r in credit_rows), 2)
# Hidden count: re-query at threshold=0 minus already-shown count
credit_hidden_count = (len(pa_mod.customer_credit_rows(threshold=0.0))
                       - len(credit_rows))
```

- Pass to template: `credit_rows, credit_total, credit_threshold,
  credit_hidden_count, show_all`.
- No change to existing permission check (admin/manager only — section
  inherits the gate).
- No change to `from`/`to` period filter (credit balance is point-in-time,
  same as AR aging — does not respect the period).

### 3. Template — new section in `cashflow.html`

Placement: directly under the AR aging card (the natural inverse view).
Card header: **"ยอดเครดิตลูกค้าค้างคืน"** with subtle subtext "บิลที่ลูกค้าจ่ายเกิน
ยอดสุทธิ — เราเป็นหนี้ลูกค้า".

Columns:

| Column | Source | Format |
|--------|--------|--------|
| IV | doc_base → `/sales/doc/<doc_base>` (endpoint `sales_doc`) | link |
| ลูกค้า | customer → `/customer/<customer_name>` (endpoint `customer_summary`) | link |
| วันที่บิล | invoice_date | DD/MM/YY (พ.ศ.) |
| ยอดบิล | billed | ฿X,XXX.XX |
| ใบลดหนี้ | credit_notes | ฿X,XXX.XX or "—" if 0 |
| รับชำระ | collected | ฿X,XXX.XX |
| **ยอดเครดิต** | credit | **bold red ฿X,XXX.XX** |
| อายุ | days_old | "N วัน" |

Footer:
- รวม: **฿{credit_total}** ({len} รายการ)
- When filtered: "ซ่อน {credit_hidden_count} รายการต่ำกว่า ฿5 (rounding noise) —
  [แสดงทั้งหมด](?show_all=1)"
- When `show_all`: link reverts to "[ซ่อนรายการต่ำกว่า ฿5](?...)" (remove
  show_all param while preserving from/to).

Empty state (no rows ≥ threshold): centered note "ไม่มียอดเครดิตค้างคืน
(เกณฑ์ ≥฿5)" with the show-all link still available.

### 4. Tests

New file: `tests/test_customer_credit_rows.py`

| Test | What it pins |
|------|--------------|
| `test_filters_below_threshold` | rows where `|outstanding| < threshold` excluded |
| `test_show_all_threshold_zero` | threshold=0 includes everything > _EPS |
| `test_eps_clamp_excludes_near_zero` | `outstanding ≈ 0` (rounding) never appears |
| `test_sort_credit_desc_then_date_desc` | order matches spec |
| `test_empty_when_no_overpaid` | returns `[]` cleanly |
| `test_credit_is_positive` | `credit == -outstanding`, always > 0 |
| `test_days_old_computed_from_today` | matches `date.today() - invoice_date` |

Light route test (extend `tests/test_routes_cashflow.py` if exists,
else inline in test_customer_credit_rows.py): GET `/cashflow` as admin
returns 200 + section header `"ยอดเครดิตลูกค้าค้างคืน"` in body.

### 5. Files touched

| File | Change |
|------|--------|
| `inventory_app/payments_alloc.py` | +1 function, ~30 lines |
| `inventory_app/app.py` | `cashflow_dashboard()` +6 lines |
| `inventory_app/templates/cashflow.html` | +1 card section, ~50 lines |
| `tests/test_customer_credit_rows.py` | new, ~120 lines |

No migration. No `.claude/` change. No `bsn_unit_full.json` change.

### 6. Risk + rollback

- Read-only computation reusing tested `invoice_settlement()`.
- Worst case: section shows wrong number → revert the 4 changed files,
  no data damage.
- No money path change (reconciliation logic untouched).
- Permission gate inherited from `/cashflow` (admin/manager).

## Acceptance criteria

1. Open `/cashflow` as admin → see "ยอดเครดิตลูกค้าค้างคืน" card under AR
   aging, showing IV6800219 (฿290), เส็งหลี ฿40.61, เมืองคงวัสดุ ฿35 (and
   any other ≥฿5 entries on live data); footer reads "รวม ≈฿372 (Nรายการ)"
   modulo current live data.
2. Click "แสดงทั้งหมด" → URL becomes `/cashflow?show_all=1`, all 10
   overpaid invoices appear, footer shows full count.
3. Click an IV → lands on `/sales/doc/<doc_base>` for that invoice
   (endpoint `sales_doc`).
4. Click a customer name → lands on `/customer/<customer_name>`
   (endpoint `customer_summary`).
5. Visit as a `staff` role → existing admin-only redirect fires before
   the new section is rendered (no leak).
6. Full pytest suite passes (was 392 at end of PR #38; this branch adds
   12 helper tests + 5 route tests → 405 pass on this branch).

## Post-Codex-review additions (2026-05-20 same session)

After the initial implementation Codex flagged 4 WARN + 2 NIT (no
BLOCK). All were addressed in the same commit:

- **`as_of` parameter on `customer_credit_rows()`** — default today, caps
  future-dated invoices out so `days_old` is always ≥ 0.
- **Single-snapshot route refactor** — `cashflow_dashboard()` calls
  `customer_credit_rows(threshold=0.0)` once and Python-filters; removes
  the double-query and any drift risk.
- **5 adversarial tests added** — SR fully-credited, cancelled-receipt
  ignored, multiline IV emits one row, vat_type=2 billed is inclusive,
  as_of future-date cap.
- **1 row-render route test added** — asserts the top live-DB credit row
  (`doc_base`, `url_for('sales_doc')` link) actually renders in HTML.
- **Template VAT clarifier** — "ยอดบิล" header now has a tooltip noting
  it is VAT-inclusive when `vat_type=2`, matching AR aging semantics.
- **Dropped unused `credit_threshold` template kwarg** — the template
  hardcodes "฿5" so passing it was dead context.
