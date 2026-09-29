# ADR 0020 — The Express ledger (ขาย / ซื้อ) arrives only through the daily zip

Status: Accepted · 2026-09-30 (Put, `decisions/log.md` 2026-09-30)

## Context

Two doors wrote `sales_transactions` / `purchase_transactions` and the stock ledger behind them: the daily
Express DBF zip (`/import-express-dbf`, `import_router.commit_express_dbf`) and the ขาย / ซื้อ text
reports on `/import-data` (`preview_file` / `commit_file`). Measured on PROD 2026-09-29:

- ขาย / ซื้อ **text-door** batches: 76 lifetime, the last on 2026-07-08, none since 2026-08-18.
- daily zip uploads: 40 since 2026-08-18, the last on 2026-09-29.

The text door's preview and commit compute differently (`preview_import` vs `_import_weekly`: unit
normalisation without the book, unmapped lines counted vs inserted, pass 2, WACC, alerts), so a preview
could promise `removed 0` and the confirm remove lines. Making the two agree was a rewrite of the
importer for a door nobody was using (parked: plan Appendix A).

Two facts about the zip made the retirement safe to declare:

- The zip reversed source lines that vanished from a re-uploaded doc **by omission of the kwarg**
  (`import_weekly`'s own default was `apply_removals=True`). PR-0 (#685) made the argument explicit and
  keyword-only with no default, and the zip now passes `apply_removals=True` in the open.
- Real-DBF replay through `commit_express_dbf` on a migrated `.backup` of the dev DB, dataset
  `projects/express-integration/data/BSN5657` (newest `DOCDAT` 2026-08-25): sales `imported 197,
  overwritten 5, unchanged 423, removed 0`; purchase `imported 20, unchanged 27, removed 0`;
  payments_in `17 imported, removed_links 4`. Repeated on this branch (`1fc7196`, DB sha in the log):
  identical, **removed 0 / 0**. The latest daily zip from the flash drive is Put's pre-merge gate; its
  numbers go in the PR body.

## Decision

1. **ขาย and ซื้อ are retired on the text box.** `ReportType.retired_reason` on the two registry entries is
   the **single switch**. `labels()` derives the ` — ปิดแล้ว ใช้ zip รายวัน` suffix from it and
   `removal_capable_keys()` excludes a retired type; `label` and `supports_removals` stay as declared
   facts. Deleting `retired_reason=` reopens the door whole, tests included.
2. **Enforced at three layers, all reading the registry at call time:**
   - preview marks a detected-retired row `blocked='retired'` (red panel, the reason, a
     `ไปหน้านำเข้า Express (zip)` button, no ticks; the count column shows `—`);
   - confirm refuses a retired **submitted** type (an `unknown` file overridden to ขาย in the dropdown
     has no block) before the file-exists check and before the watermark claim;
   - `import_router.preview_file` / `commit_file` raise `RetiredReportType(ValueError)` for any
     retired type, so no caller can reach `models.import_weekly` for ขาย / ซื้อ. `commit_file` also
     loses its `apply_removals` default; the route is its only caller.
3. **The zip's removals become numbers.** `results['bsn']['removed_lines'] = {'sales': n, 'purchase': m}`
   goes into the run record (`import_log.notes`) and the flash appends
   ` · ลบบรรทัดที่หายจากต้นทาง N` when `n + m > 0`.
4. **S4 fix** (payments_in text preview): `new` / `existing` are counted over the merged receipts, the
   same collapse `import_payment_records` applies, and `merged` is shown (`· รวมซ้ำ N`).
5. **Other ขาย text paths**: `scripts/import_express.py sales` (writes `express_sales`, read by
   `models/brands.py`; last run 2026-05-01) loses its CLI `choices` entry; `_import_sales` and
   `run_import('sales')` stay for that table. `scripts/reimport_2026_04_28/run.py` is a dead inline
   one-off and is left alone.

## Considered

- **Keep the door and guard it** (decision fingerprint, `PlanChanged`, previewed-import entry point):
  rev-2 design, parked in the plan's Appendix A. A large rewrite for a door with no use since 2026-07-08.
- **Keep the door, remove only the removals opt-in**: leaves preview and commit disagreeing on
  unmapped lines, pass 2 and alerts.
- **Delete the dormant code**: Put chose "re-enable = one field"; revisit after a quarter has closed
  (2027-01).
- **Retire payments_in / credit-note text uploads too**: not measurable as unused, not asked.

## Consequences

- The text-door code (`preview_import`, `commit_file`'s ขาย/ซื้อ branch, `_reject_history_export`, the
  #655 weekly marks, the `skipped-removals` / `ignored-lines` panels, the post-confirm review hook)
  is **dormant, not deleted**. Its route and dispatcher tests keep running under the
  `unretired_text_door` fixture (`tests/conftest.py`); `tests/test_retired_report_types.py` runs
  without it and pins the retirement.
- The bill-review feed no longer depends on the text confirm: the zip route re-scans every upload
  since #682, and `/review/scan` is the manual fallback.
- Pass 2 stays table-wide in this PR. Counting what it first-syncs beyond the file's own products is
  PR-2 of the same plan.
