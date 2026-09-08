# ADR 0012 — Outstanding vs chaseable AR: two named populations, and readers must say which

Status: Accepted · 2026-09-08

## Context

"BSN AR" meant two different things and the codebase never named either. `cashflow.BSN_AR_PREDICATE`
holds the **chaseable** definition — outstanding minus `ar_writeoffs`, minus `is_anomalous`
("ลูกหนี้จ่ายแล้ว"), minus pre-2024 legacy debt — and its own comment says *"Every page that totals
BSN AR must apply this"*. Ten readers touch `express_ar_outstanding`; four of them hand-typed their
own WHERE clause instead of importing it, and each landed on a different combination of the three
exclusions:

| surface | write-offs | is_anomalous | pre-2024 |
|---|---|---|---|
| `/ar`, `/cashflow`, `/call` | excluded | excluded | excluded |
| `/customer/code/<code>` + mobile | shown | shown | excluded |
| dunning detail (`/accounting/ar-followup/customer/<key>`) | shown | shown | shown |
| `/express/ar/customer/<code>` | shown | excluded | shown |

The result was a live money defect: written-off invoices read as chaseable debt one click from a list
that correctly dropped them. Measured on prod (snapshot 2026-09-07), the dunning detail page — the
screen a person opens immediately before phoning a customer — showed **฿1,077,193.37 / 212 docs**
where **฿470,035.17 / 153 docs** was actually chaseable: ฿607,158.20 of phantom debt, a 2.3×
overstatement, in front of someone about to make the call.

No test caught it. `test_ar_reconcile.py`'s docstring claims "Every BSN AR surface must total the
SAME canonical figure" while asserting over three of the ten surfaces, and it **re-types** the
predicate's SQL rather than importing it — so production could drift away from the oracle without
turning it red.

## Decision

**Two named populations, in `CONTEXT.md`:**

- **outstanding** — what the snapshot says is unpaid, nothing removed.
- **chaseable** — outstanding minus all three of Put's rulings.

**A reader must name which it wants.** "AR" alone is not an answer. The chaseable definition lives in
exactly one place (`cashflow.BSN_AR_PREDICATE`) and is **imported, never re-typed** — re-typing is
the mechanism by which all four surfaces drifted.

All four per-customer surfaces adopt **chaseable**, because all four are chase-facing.

**`commission.py`'s `open_ar` map deliberately keeps `outstanding`.** This is the part a future
reader will otherwise "fix": the commission engine cannot see the AR snapshot at all (payouts run off
`received_payments JOIN paid_invoices`), and `open_ar` feeds only three read-only cells on the rep's
invoice tab. Applying the write-off exclusion there would flip never-collected invoices to a green
**จ่ายแล้ว ฿0.00** badge — measured at 3 invoices / ฿35,201.25 — which is worse than the problem.
A bill that was forgiven was still never collected, and a page about collection history should say so.

## Consequences

- The exclusion is applied at the point of import, so a new reader that forgets it is visibly reading
  `outstanding` rather than silently producing a wrong `chaseable`.
- `_unpaid_bills` keeps its additional `outstanding_amount > 0` clause, which `/ar` does not have.
  This is deliberate and is a fourth axis, not a fifth divergence: a per-customer bill *list* should
  not render credit rows. On prod it accounts for the whole gap between the two figures
  (150 docs / ฿470,381.17 vs 153 docs / ฿470,035.17 — three rows summing −฿346.00).
- ⚠ The written rules were wrong about this and have to be corrected, not merely extended:
  `.claude/rules/erp-engineering-discipline.md`, `context/current-priorities.md` and
  `context/goals.md` all describe `/ar`'s population as `_unpaid_bills`' filters minus the write-off
  table, and explicitly state the app does **not** drop `is_anomalous`. Measured on prod, `/ar` shows
  ฿470,035.17 / 153 docs; the formula those documents describe yields ฿475,200.04 / 151 docs.
