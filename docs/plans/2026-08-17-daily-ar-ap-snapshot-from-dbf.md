# Daily AR/AP outstanding snapshot from the Express DBF zip

**Date**: 2026-08-17 · **Branch**: `feat/daily-ar-ap-snapshot-from-dbf`

## Problem

The team uploads one Express DBF zip per working day (`/import-express-dbf`). It
imports sales, purchases, invoice refs, payments in/out and both credit-note
sides — but **not** ลูกหนี้คงค้าง / เจ้าหนี้คงค้าง. Those only ever arrive as
separate text reports through `/import-data`, and nobody has exported them since:

| table | latest snapshot (prod, 2026-08-17) | age |
|---|---|---|
| `express_ar_outstanding` (BSN) | 2026-06-05 | 73 days |
| `express_ap_outstanding` (BSN) | 2026-05-29 | 80 days |

`cashflow.ar_aging()`, `ar_followup` (the `/ar` dunning list), the commission
engine's AR check and `models/pricing_ap.py` all read those tables, so every one
of them is showing June numbers. 67 receipts and 733 sales lines have landed
since. The ฿331,107.09 collectable figure is the 2026-06-05 number.

## Decision (Put, 2026-08-17)

Import **both books**, each book keeping its own data — no merging of totals.

The zip already carries two datasets and they are classified at upload:

* `BSN5657` — the no-VAT operational book → the **main DB** (`novat` book)
* `xp5` — the VAT book → **`vat_book.db`** (`vat` book)

Their document series are disjoint (AR: `IV69…` vs `IV…` on separate numbering;
AP: `RR69…` vs `RR26…`), so they are genuinely different debts, not duplicates.

⚠ Pre-existing defect this corrects: today's `express_ap_outstanding` rows are
tagged `entity='BSN'` in the **main** DB but were imported from the **xp5** book
(`RR26…`). After this change the main DB's AP snapshot comes from `BSN5657`
(`RR69…`, 19 open rows) and xp5's AP lives in the VAT book, where it belongs.
The 7 stale 2026-05-29 rows are left in place — readers take
`MAX(snapshot_date_iso)`, so the new snapshot supersedes them automatically.

## Why this is a one-place change

`vat_book_builder.build()` already calls
`import_router.commit_express_dbf(source_dir, since_days=None)` against its own
DB. Adding the snapshot to `commit_express_dbf` therefore feeds **both** books
from a single code path — the BSN branch writes the main DB in-request, the VAT
branch writes `vat_book.db` in its existing detached rebuild.

No schema change: both tables already exist, and the VAT book builds its schema
from the same `schema.sql`. **No migration needed.**

## Field mapping (verified, do not rediscover)

Row set for both sides: `round(REMAMT, 2) != 0`.

⚠ Two traps that bite immediately:
1. **Round before testing REMAMT.** Raw `REMAMT` is an 8-byte double carrying
   IEEE-754 noise: an unrounded `!= 0` selects **1,330** rows on BSN5657 where
   the real answer is **232**.
2. **No date window.** `commit_express_dbf` windows sales/purchase to
   `since_days=60`; outstanding debt must ignore it entirely — the 2026-06-05
   report contains docs dated 2009.

### AR — `ARTRN` + `ARMAS`

| column | source | evidence |
|---|---|---|
| `doc_no` | `DOCNUM` | 95/95 tie |
| `doc_date_iso` | `DOCDAT` | 95/95 |
| `customer_code` | `CUSCOD` | 95/95 |
| `customer_name` | `ARMAS.CUSNAM` | 66/67 (the miss has no ARMAS row; the report is blank there too) |
| `customer_type` | label of `ARMAS.CUSTYP` | `00`→ลูกค้าประจำ, `01`→ลูกค้าประจำ (ซาปั้ว), `02`→ตัวแทนจำหน่าย(ยี่ปั้ว), `05`→ซื้อภายใน — covers 100% of open rows in both books |
| `salesperson_code` | `SLMCOD` | 95/95 |
| `paid_amount` | `RCVAMT` | tie basis |
| `outstanding_amount` | `-REMAMT` when `RECTYP='5'` else `REMAMT` | 51 exact ties; the 2 SR rows are the sign flip |
| `bill_amount` | `RCVAMT + REMAMT` when `RECTYP='9'` else `NETAMT` | `NETAMT == RCVAMT+REMAMT` holds 189/189 for RECTYP 3+5 and **never** for RECTYP 9 (whose header money fields are 0 — MAPPING trap #4) |
| `is_anomalous`, `has_warning` | `RECTYP == '9'` | 43/43 both flags |

`RECTYP` ↔ doc prefix is 1:1 in the snapshot population: `3`=IV, `9`=RE, `5`=SR.

### AP — `APTRN` + `APMAS`

| column | source | evidence |
|---|---|---|
| `doc_no` | `DOCNUM` | 7/7 |
| `supplier_invoice_no` | `REFNUM` (**not** `YOUREF`, which is blank) | 7/7 |
| `supplier_code` / `supplier_name` | `SUPCOD` / `APMAS.SUPNAM` | 7/7 |
| `supplier_type` | label of `APMAS.SUPTYP` | `00`→ผู้จำหน่ายประจำ, `03`→ผู้ค้าส่ง |
| `bill` / `paid` / `outstanding` | `NETAMT` / **see below** / `REMAMT` | invariant holds on every RR row measured |

⚠ **`paid` is NOT always `RCVAMT` on the AP side — pick the column by RECTYP.**

Found 2026-08-17 by re-running against that day's Express export, on `GR6900005`
(a purchase credit note dated 2026-07-31): `NETAMT == RCVAMT == REMAMT == 1040.25`
with `PAYAMT == 0`. On a GR row `RCVAMT` **mirrors the credit instead of recording a
payment**, so `bill = paid + remaining` only balances against `PAYAMT`. On ordinary
`RR` invoices the opposite is true — `RCVAMT` is the paid amount, and that is the
reading that tied 7/7 to the 2026-05-29 prod snapshot.

This is the same family as MAPPING trap #5 (`payments_out` must use `RCVAMT`, not
`PAYAMT`, because PAYAMT diverges on PS rows). On `APTRN` the two columns swap roles
by document type and **neither is safe to use blind**.

The earlier "invariant holds 19/19 and 13/13" measurement was true and still is — it
simply had no open GR row in it. A guard that only ever saw RR rows proved nothing
about GR, which is why the fresh export caught it and the older one could not.

⚠ **AP credit notes have no oracle.** The sign flip (`outstanding = -REMAMT` for
RECTYP `5`) mirrors the AR side's SR handling, which IS tied to a real Express report.
No เจ้าหนี้คงค้าง report containing a GR row has ever been seen, so this one column is
reasoned by symmetry rather than measured. It is ฿1,040.25 of one document today.
Confirm it the first time a real AP report is exported.

⚠ Open: `SUPTYP` `02` (all 19 BSN5657 rows) and `๙๙` have no observed label —
they fall back to the raw code until a real เจ้าหนี้คงค้าง report from BSN5657
shows what Express prints. Cosmetic only; no total depends on it.

### Snapshot date

`date.today()` on the server. Verified the container clock is Asia/Bangkok:
`express_import_log.imported_at` reads `16:57` for the team's ~17:00 upload.

## Phases

**Phase 1 (this branch) — get both books fed daily.**
1. `express_dbf_source.build_ar_snapshot_records` / `build_ap_snapshot_records`
   — pure functions over dict rows, no file IO, no cutoff parameter.
2. `scripts/import_express.py`: `_import_ar_snapshot_records` /
   `_import_ap_snapshot_records`, registered in `_RECORDS_IMPORTERS`, reusing the
   existing DELETE+INSERT-per-`(entity, snapshot_date)` idempotency and the
   `express_import_log` batch row.
3. Wire both into `commit_express_dbf`; surface the counts in the upload flash.

**Phase 2 (separate branch) — make the AR/AP pages book-aware.**
`/ar` and `/ap` are not in `book_registry.PARITY_ENDPOINTS` and read through
`database.get_connection()`, so the xp5 snapshot is written but not yet viewable
under the VAT-book toggle. Moving them onto `get_book_connection()` touches
`ar_followup.py`, `cashflow.py`, `models/payments.py` and
`models/pricing_ap.py` — its own change, with its own reconcile.

## Verification gate

* Unit tests: dict fixtures for every row above, plus the rounding trap, the
  no-window rule, and the RECTYP-9/SR quirks.
* Replay: build from the local 2026-07-31 `BSN5657` dataset and diff against the
  2026-06-05 prod snapshot restricted to rows whose `RCVAMT` is unchanged since
  (i.e. untouched by any receipt in between) — those must tie to the satang.
* ⛔ **Merge gate (money path, per `.claude/rules`):** one ลูกหนี้คงค้าง **and**
  one เจ้าหนี้คงค้าง exported from Express on the SAME day as a daily zip,
  reconciled doc-for-doc and to the satang against the DBF-derived snapshot.
  Reasoning about the field mapping is not a substitute for that tie.
