# #590 census: every writer of cost, and whether its actor reaches `audit_log`

Branch `feat/590-cost-audit-actor` off `origin/main` 21a58e4. Written 2026-09-19 by agent
`own-590` (tab sendai-boonsawat-a4). Phase 1 of #590: nothing here changes behaviour.

## 1. How `audit_log` gets its `user` today (measured on PROD, read 2026-09-19 15:47Z)

`audit_log` is written two ways:

- **Triggers** (117 `audit_*` triggers). Almost none of them write `user`. Migration 003 says why:
  "Flask session context isn't available inside SQLite". The two exceptions are
  `sales_transactions` and `purchase_transactions`. Since migration 173 those rows carry
  `change_actor` / `change_source` / `change_reason` / `change_token` columns, a BEFORE UPDATE
  trigger refuses an undeclared change, and the audit trigger copies the columns across.
- **App code writing `audit_log` directly** with `user` filled in: cashbook, hr, commission,
  `me`, marketplace_match, and a few scripts.

Nothing that touches cost uses either path, so no cost row has an actor.

| population (prod) | rows | `user` NULL | signed |
|---|---:|---:|---:|
| all of `audit_log` | 237,424 | 230,761 (97.2%) | 6,663 |
| of which `transactions` (stock-ledger churn, pruned at 90 days) | 202,060 | 202,060 | 0 |
| everything except `transactions` | 35,364 | 28,701 (81.2%) | 6,663 |
| `products`, all rows | 13,065 | 13,008 | 57 (none touch cost: 49 from `apply_bolt_family_plan.py` on 09-04, 8 from `merge_script` on 05-11) |
| **`products` rows carrying `cost_price`**: INSERT / UPDATE / DELETE | 2,124 / 1,327 / 2,016 | **all** | **0** |
| `products` cost UPDATE, last 30 days | 173 | 173 | 0 |
| of which 2026-09-18 08:52 (the #546 zero-stock replay, run before approval) | 147 | 147 | 0 |
| `purchase_transactions` (mig 173) | 3,786 | 0 | 3,786 |
| `sales_transactions` (mig 173) | 601 | 0 | 601 |

The issue's 98.7% is mostly `transactions`. The part #590 is about is
**0 signed rows out of 5,467 cost rows**.

Cost-bearing places that have **no audit trail at all**:

- `products.opening_cost`, the WACC basis. `audit_products_update` does not list it.
  886 of 2,070 products have a non-zero value.
- `product_cost_ledger`. It has no audit trigger and no actor column. There are 4,709 rows
  but max id is 72,537, because every recalculation deletes a product's rows and re-inserts
  them. 4,648 of the 4,709 were re-inserted at 2026-09-18 08:52.
- `conversion_cost_log`. 40 rows, no actor column.

The one place a person's name reaches a cost row today is `product_price_history.source`.
Migration 130 fills it through the one-row side table `price_change_source`. For
`cost_price` the values are:

| `source` | rows |
|---|---:|
| NULL | 1,080 |
| `wac-sync` | 239 |
| `script:…` | 7 |
| `manual:admin` | 1 |

The WACC engine always stamps `wac-sync`, whoever set it running.

**Timestamp hazard (forensics).** `app.py` runs `os.environ.setdefault('TZ','ICT-7'); time.tzset()`.
Neither gunicorn nor a `railway ssh` shell sets TZ (checked through `/proc/*/environ` on
prod). So `datetime('now','localtime')` records Bangkok time for anything that imported
`app`, and UTC for a script that imported only `models`/`database`. From the row alone you
cannot tell whether the 09-18 "08:52" is 08:52 ICT or 15:52 ICT.

## 2. The SQL sites that write cost (leaf writers)

Found by `scan_writers.py` (a multi-line text scan for every INSERT/UPDATE/DELETE/REPLACE whose
target is a cost table or a dynamic `{table}` / `%s` / `.format`) plus a name-based reverse
call graph (`callgraph.py`). Both scripts are kept in the agent's scratch dir. Every hit was
then read by hand.

| id | site | writes | shape |
|---|---|---|---|
| L1 | `models/products.py:124` `create_product` | INSERT products (`cost_price`, `opening_cost`) | literal. **Nothing in the app calls it** (only `tests/test_drop_int_sku.py`) |
| L2 | `models/products.py:347` `create_structured_product` | INSERT products (`cost_price`, `opening_cost`) | literal |
| L3 | `models/products.py:518` `update_product` | UPDATE products `{set_clause}` | f-string. `_UPDATABLE_PRODUCT_COLUMNS` lets `cost_price` and `opening_cost` through |
| L4 | `models/wacc.py:274` `_recalculate_product_wacc` | DELETE `product_cost_ledger` | literal |
| L5 | `models/wacc.py:424` same function | INSERT `product_cost_ledger` | literal |
| L6 | `models/wacc.py:441` same function | UPDATE products `cost_price` (stamps `wac-sync`) | literal |
| L7 | `models/conversions.py:626` `run_conversion` | INSERT `conversion_cost_log` | literal |
| L8 | `blueprints/admin.py:482-483` `_replace_master_tables` | `DELETE FROM main.{table}` + `INSERT … SELECT * FROM upl.{table}`, and `products` is in `_MASTER_TABLES` | f-string. **Raw `sqlite3.connect`** |
| L9 | `blueprints/admin.py:615` (full upload), `admin.py:661` (upload confirm), `db_backup.restore_backup` (`db_backup.py:398`, reached from `admin.py:750`), and `app.py:231` (`/bootstrap/upload-db`) | replace the whole DB file | no SQL, so no trigger can see it |
| L10 | `vat_book_builder.py:83` `seed_products_from_stmas` | INSERT products `cost_price` into **vat_book.db** | literal, raw connection |
| L11 | `database.py:443` `run_pending_migrations` | `executescript` on each pending migration. Past cost writers: 111 (opening_cost backfill), 156 (ledger delete), and the table rebuilds 069/097/141 | file |

The script-local SQL sites are listed with their scripts in §3.

## 3. Entry points: who is at the controls, and what reaches `audit_log` today

"Actor at that moment" means the human the system could know at that point.
"Reaches audit_log today" is ✗ for every row, so that column is replaced by the
**only trace that exists today**.

### HTTP routes (U1 to U13 have a Flask session user; U14 has none)

With impersonation (ADR 0003) the session holds the target user, and the real admin is in
`session['_real_username']`.

| # | entry point | leaf writers | actor at that moment | trace today (audit `user` is NULL for all) |
|---|---|---|---|---|
| U1 | POST `/products/new` → `create_structured_product` | L2 | session user | `created_via='manual'` |
| U2 | POST `/products/<id>/edit` → `update_product`, then `recalculate_product_wacc` on a second connection | L3, L4-L6 | session user | `product_price_history.source='manual:<username>'` on the `cost_price` row, then `wac-sync`. `opening_cost` is not audited. ⚠ #577 regression, see §5 |
| U3 | **GET** `/products/<id>/cost-history` → `get_cost_history` (lazy rebuild when a product has no ledger rows) | L4-L6 | whoever is viewing | `wac-sync` |
| U4 | POST `/import-data/confirm` → `commit_file` → `import_weekly` → recalc for each touched product, one connection | L4-L6 | uploader | purchase lines have `change_actor=<filename>`, which names a file, not a person |
| U5 | POST `/import-express-dbf/upload` → `commit_express_dbf` → `import_weekly` → recalc | L4-L6 | uploader | `change_actor='express_dbf:BSN5657'` on document lines |
| U6 | POST `/mapping/save` (create-now) → `create_now` → `approve_pending_suggestion` → `create_structured_product` (cost comes from the suggestion or the clone source) | L2 | session user (`user_id` is passed in but never stamped on cost) | `created_via='smart_mapping[_clone_N]'` |
| U7 | POST `/mapping/suggestions/<sid>/approve` → `approve_pending_suggestion` → `create_structured_product` | L2 | session user (`reviewer_id`) | `created_via` |
| U8 | POST `/mapping/split-save` → `repoint_bsn_code` → recalc (3 call sites) | L4-L6 | session user | `change_actor` on document lines only |
| U9 | POST `/unit-conversions/edit` → `update_unit_conversion_ratio` → recalc on its own connection | L4-L6 | session user | `wac-sync` |
| U10 | POST `/conversions/<id>/run` → `run_conversion` → lazy `get_current_wacc` + `conversion_cost_log` + `recalculate_waccs_for_products` | L4-L7 | session user | none (`conversion_cost_log` has no actor column) |
| U11 | POST `/admin/upload-db` with `mode=master_only` → `_replace_master_tables` | L8 | admin | 1,999 DELETE + 1,999 INSERT audit rows, all NULL (2026-06-13 06:48) |
| U12 | POST `/admin/upload-db` full mode (+ `/confirm`) | L9 | admin | nothing (file swap) |
| U13 | POST `/admin/backups/restore` | L9 | admin | nothing (file swap) |
| U14 | POST `/bootstrap/upload-db` (`app.py:212`, `@csrf.exempt`, live only while `BOOTSTRAP_TOKEN` is set, **no session**) | L9 (`os.replace` at `app.py:231`) | the token holder, who cannot be identified | nothing (file swap). Added in revision 2, because the interrogate panel found it missing (Opus 12) |

### Processes with no request

| # | entry point | leaf writers | actor at that moment | trace today |
|---|---|---|---|---|
| P1 | app boot → `init_db` → `run_pending_migrations` | L11 | the deploy (no human identity) | `applied_migrations` row only |
| P2 | VAT-book builder subprocess, spawned by U5 (`bsn.py:1563`, `DATA_DIR` = empty build dir) | L10 + `import_weekly` → recalc, all on vat_book.db | the U5 uploader, but the uploader's name is **not passed** to the subprocess | none. vat_book.db is rebuilt from scratch on every upload |

### Scripts (`scripts/`, run locally or over `railway ssh`)

There is one Railway account, so the operator can only ever be **self-declared**.

| # | script | writes cost via | connection | trace today |
|---|---|---|---|---|
| S1 | `2026_08_17_bolt_dozen_to_piece.py` | UPDATE products `cost_price`+`opening_cost` (:158), UPDATE ledger rescale (:174) | raw | `price_history.source='script:…'` |
| S2 | `2026_09_19_gross_to_piece.py` (engine `rebase()`) | same shape (:106, :181) | raw | same |
| S3 | `2026_09_19_rebase_689_767.py` (loads S2's engine) | S2's engine | raw | same |
| S4 | `2026_09_19_fix_pack_ratios_592.py` | `recalculate_product_wacc` | raw | `wac-sync` |
| S5 | `2026_09_19_split_belco_582.py` | `create_structured_product` + recalc | raw | doc lines `change_actor`; cost none |
| S6 | `merge_product.py` | recalc | raw | doc lines `change_actor='merge-product'`; cost none |
| S7 | `backfill_opening_cost_20260617.py` | UPDATE `main.products.opening_cost` from an ATTACHed backup (:60) + recalc of every product | `get_connection` | none |
| S8 | `hammer_bundle_datafix.py` | UPDATE `opening_cost` (:355) + recalc | raw | none |
| S9 | `force_stock_targets.py` | recalc | raw | none |
| S10 | `rebuild_opening_balance_v2.py` | recalc | raw | none |
| S11 | `rebuild_opening_balance_from_csv.py` | recalc | raw | none |
| S12 | `phase_c_replay_apply_20260530.py` | recalc | raw | none |
| S13 | `phase_c_dedup_replay_20260530.py` | recalc | raw | none |
| S14 | `remap_bsn_code.py` | `repoint_bsn_code` → recalc | raw | none |
| S15 | `apply_decision_remaps.py` | INSERT products copying **every** column of a source row, cost included (:150) | raw | none |
| S16 | `apply_platform_overview_mapping.py` | INSERT stub products with `cost_price=0` (:124) | raw | none (creation at zero cost) |
| S17 | `import_listing_mapping_csv.py` | same (:151) | raw | none |
| S18 | `cleanup_split_mapping_stubs.py` (marked DEPRECATED) | prints recalc commands for an operator to paste | raw | none |
| S19 | **ad-hoc heredoc over `railway ssh`** (no file in the repo). Example: the #546 option-B replay, 2026-09-18 08:52 | `models.recalculate_product_wacc` in a loop: 147 `cost_price` changes, 4,648 ledger rows | whatever the heredoc opened | `wac-sync` only. **Nobody can tell who ran it** |

**Census total: 35 entry points** (14 HTTP routes, 2 processes, 19 scripts) **over 11 app-side SQL sites
plus 7 script-local ones.** Revision 2 added U14.

## 4. Hits the scan returned that do NOT write cost (each with its reason)

- `naming_cascade.py:209/426/452`: its dynamic `{set_clause}` goes through `_EDITABLE_TEXT`
  (series/model/size/color/packaging) plus `brand_id`, with no cost column.
- `blueprints/products.py:133/761/796/802/831` (category, packaging, sku_code),
  `models/brands.py:49`, `sku_code_utils.py:203`, `models/products.py:213/395/527`
  (family, name, is_active), and `database.py:344` (the `updated_at` trigger).
- `models/_shared.py:86/114`, `bsn_sync.py` and `imports.py`, `mapping.py:674`,
  `express_registers.py:146/151`: dynamic `{table}` targets that are only ever the document
  tables or Express registers.
- Scripts that set only naming/brand/sku/family/is_active/marketplace stock:
  `apply_product_naming`, `apply_sku_*`, `autofix_sku_naming`, `backfill_*` (except S7),
  `brand_backfill_suggest`, `generate_sku_codes`, `apply_*mapping`, `p0_split_3p5in`,
  `p2p3_split_hinges`, `replay_history_apply`, `apply_worksheets_20260530`,
  `apply_normalize_round1` (its `APPLY_FIELDS` has no cost column).
  `import_catalog_pricing.py:494` writes `base_sell_price` only.
- `dump_schema.py:70` generates text and runs no SQL.
- `*.py.txt` one-offs cannot be run.
- **Inputs to WACC that are not cost writers.** `purchase_transactions` is already signed by
  mig 173 (3,786 of 3,786 rows). `transactions` (stock) feeds quantities into WACC but has no
  cost column, and a change there reaches cost only through a recalc, which is one of the
  entry points above.

## 5. Side finding (outside #590, reported to root 2026-09-19)

Since #577 (merged 2026-09-17 09:08Z), `product_edit` always sends `cost_price = <cost box>`,
and it recalculates only when `opening_cost` moved. The cost box shows `opening_cost`. So an
edit that leaves the cost box alone, for example a sell-price change, **overwrites the live
WACC `cost_price` with `opening_cost`, and nothing recalculates it**.

- Reproduced in a fixture: WACC 41.5 → 33.0 after a sell-price-only POST. The canary
  `base_sell_price` did land, and the response was 302.
- 893 active prod products have `cost_price ≠ opening_cost` and are exposed.
- No damage yet: the only manual cost row since the merge is pid 27, an intended change.
