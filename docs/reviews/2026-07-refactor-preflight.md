# Refactor Preflight Review — 2026-07-02

> Phase 0 of the structural refactor (plan: `~/Sendai-Boonsawat/projects/sendy-refactor/plan.md`).
> Two independent read-only reviewers (app-side: app.py/database.py/blueprints/templates; models-side:
> models.py/conftest.py). Findings in section B are REPORT-ONLY: they are not fixed by the refactor
> and need Put's adjudication. Sections A feed directly into phases 1-12 as binding design deltas.

## A. App-side: refactor blockers and facts

### A1. Duplicate route-function names
- Zero duplicates within app.py (131 top-level functions, AST-verified).
- Cross-file reuse exists and is safe (blueprint prefixes disambiguate): `dashboard` (app.py + cashbook + hr + marketplace), `_require_admin` (app.py commission-only + hr.py's own), `_fmt_baht`, `_require_admin_or_manager`, `leave_edit`, `index`.
- Phase 4 merge target checked: zero overlap between bp_products' 19 existing functions and the incoming leftovers. Clean.

### A2. Module-global mutable state (gunicorn -w 2 hazard)
- None. Zero `global` usage; all 21 top-level `_NAME` bindings in app.py and every blueprint constant are immutable (frozenset/dict/tuple/regex) built at import and never mutated at request time.
- The two historical per-worker bugs (DB_ROUTES_ENABLED, commission._OVERRIDES_CACHE) remain fixed. No new instance in moving code.

### A3. Endpoint-string comparison surface (beyond url_for)
- Python-side, beyond the known catalog (`_ENDPOINT_MODULE`, POST-whitelist frozensets, `_GENERAL_ALLOWED`, auth-exempt tuple, admin-simulate checks):
  - `_ACCT_FINANCE_ENDPOINTS` frozenset (app.py:537-541): `accounting_summary`, `cashflow_dashboard`, `revenue_dashboard`, `revenue_unmapped_drilldown`, `ar_followup`, `ar_followup_customer`, `ar_followup_log_new`, `ar_followup_log_delete`, `ar_followup_export` — ALL nine rename in Phase 9 (bp_accounting). Feeds `_mobile_active_slot()`.
  - `_MOBILE_NAV_SLOTS` (app.py:526-531): endpoint literals `trade_dashboard` (renames Phase 6) and `accounting_summary` (renames Phase 9); `products.product_list`/`hr.dashboard` already blueprinted.
- Template-side: ALL `request.endpoint` comparisons live in `base.html` only — 40 sites (sidebar active-state). `_mobile_drawer.html` and every other template: zero. Two notable patterns:
  - `'product' in (request.endpoint or '')` (base.html:104) — substring check, keeps working as long as moved names contain "product".
  - `(request.endpoint or '').startswith('conversion_')` (base.html:112) — prefix check; Phase 3 must either keep function names starting `conversion_` (endpoint becomes `inventory.conversion_*`, so the startswith BREAKS: `inventory.conversion_...` does not start with `conversion_`) — rewrite these sites to match the new names explicitly.
- Every phase's endpoint-rename checklist already greps both patterns; this section is the complete inventory.

### A4. Import-cycle risks and re-export obligations
- No blueprint imports `app`; no non-test module imports `app`. No existing cycles.
- 5 test files import symbols directly off `app` that Phase 1 relocates. app.py MUST re-export after Phase 1: `_ENDPOINT_MODULE` (test_ap_page, test_ar_page, test_hr_phase7), `_STAFF_POST_OK` + `_MANAGER_POST_OK` + `_MODULE_DEFS` (test_post_whitelist), `build_mobile_nav_slots` (test_hr_phase6, test_mobile_nav).
- `import review_rules as rr` is the one cross-cutting import: used by `inject_auth()` (Phase 1 → access_control.py) AND `unified_import_confirm()` (Phase 2 → bp_bsn). Both destination files import it themselves.
- `import hr as hr_mod` (app.py:2946) sits after its only call site `dashboard()` (stays in app.py) — legal, leave as is.
- Doc drift: `sendy_erp/CLAUDE.md` claims `auth.py`/`permissions.py` exist (they do not) and lists only 3 blueprints (there are 11). Fix in the final docs PR.

### A5. Route mechanics that constrain Phase 1
- No `add_url_rule`, no `endpoint=` overrides anywhere; one `@csrf.exempt` (`bootstrap_upload_db`, stays in app.py).
- `require_login` (before_request), `inject_auth` (context_processor), and the 3 template filters are APP-scoped. After moving to access_control.py/filters.py they must be registered onto `app` from app.py (`app.before_request(require_login)`, `app.context_processor(inject_auth)`, `app.template_filter('fmt_price')(fmt_price)` etc.) — a blueprint-scoped hook would not fire for other blueprints' routes.

### A6. Helper routing (all 21 private app.py helpers have single destinations)
- Stay in app.py / Phase 1: `_role_home`, `_mobile_active_slot`.
- Phase 2 (bp_bsn): `_snapshot_before_import`.
- Phase 3 (bp_inventory): `_pair_prefill`.
- Phase 4 (bp_products): `_walk_review_files`, `_safe_under`.
- Phase 5 (bp_partners): `_parse_bsn_customers`.
- Phase 7 (bp_commission): `_months_with_payment_activity`, `_require_admin` (app.py:3467, commission-only; do NOT consolidate with hr.py's), `_safe_clear_override_cache`.
- Phase 9 (bp_accounting): `_arf_require_manager`, `_arf_require_admin`.
- Phase 10 (bp_admin): `_set_account_employee`, `_count_rows`, `_table_exists`, `_replace_master_tables`, `_backups_dir`, `_reload_workers_after_restore`. (`_diff_master_tables` is dead — see C.)
- Module aliases: `commission_mod` Phase 7 only; `cf_mod`/`rev_mod`/`pa_mod` Phase 9 only; `import_router` Phase 2 only; `hr_mod` stays.

### A7. Route counts and ORPHAN assignment (binding correction to the plan)
104 routes total, zero duplicate URLs. Auth core staying in app.py = 9. Actual per phase: bp_admin 12, bp_bsn 9 (+2 orphans below = 11), bp_inventory 9 (incl. transaction_history), bp_products leftovers 9, bp_partners 10, bp_sales 7, bp_commission 12, bp_ecommerce 9, bp_accounting 15.
Three routes had no assigned home; now assigned:
- `unified_import` + `unified_import_confirm` (/import-data*) → **Phase 2 bp_bsn** (Express/BSN import flow; carries `_snapshot_before_import` + the `import_router` alias).
- `transaction_history` (/transactions) → **Phase 3 bp_inventory** (stock ledger view).

## A. Models-side: split blockers and facts

### A1m. Line-range map corrections (binding for phases 11-12)
- `deactivate_product` (336) → products. `_detect_removed_lines` (1356) → imports_weekly (not mapping). `get_purchases` (2669) → sales_purchases_trade (not commission/payments).
- Overlap 1159-1206 resolved semantically: `upsert_mapping` (1159) → mapping; `upsert_unit_conversion` (1190) → bsn_sync_unit_conversions.
- `prune_audit_log` (1295) + `AUDIT_LOG_RETENTION_DAYS`/`_AUDIT_PRUNE_PREDICATE` → NOT mapping; give it a housekeeping home (suggest models/_shared.py or accounting).
- `_WACC_INITIAL_DATE` (4553) → wacc, not suppliers.
- `_clean_for_match` + `_NOISE_WORDS`/`_QTY_PREFIX`/`_re_mod` (3804-3826) → NOT conversions; only callers are `suggest_platform_mapping` (platform_skus) and `suggest_listing_mapping` (ecommerce). Extract to a shared leaf `models/_shared.py` so those two modules don't depend on each other.
- `to_base_units` (44) is dead (see C) — parking spot irrelevant.

### A2m-A3m. Cross-module edges and import order (no cycles after corrections)
Edges: bsn_sync→wacc; conversions→wacc; imports_weekly→{bsn_sync, mapping, wacc}; mapping→bsn_sync; suggestions→{mapping, products}; platform_skus→_shared; ecommerce→_shared. `wacc` is a pure sink (safest first extraction).
Topological import order: `_shared → products → brands → stock → transactions → promotions → customers_regions → commission → payments_ar → pricing_summary_ap → platform_skus → conversions → customers_from_bsn_geo → suppliers → wacc → accounting → ecommerce → bsn_sync → mapping → imports_weekly → sales_purchases_trade → suggestions → marketplace`.

### A4m. Module-level state
All immutable (constants/regexes) — no mutable caches, no multi-worker risk. Homes: `_BULK_MAX` → customers_regions; `_FEE_LABELS`/`_RECON_CUSTOMER`/`_CANCEL_RETURN_STATUSES`/`_BATCH_TOLERANCE` + the `marketplace_fee_buckets` import → marketplace; audit/wacc/match constants per A1m. Note: 7 marketplace functions do a local `from database import get_connection` inside the body — harmless, do not "clean up" (they interact with the `conn=None` optional-connection pattern).

### A5m. Facade re-export obligations
148 distinct names accessed as `models.X` externally. Private names: exactly the 4 known (`_sync_bsn_to_stock`, `_resolve_mapping`, `_get_base_qty`, `_fee_pct_str`). Also re-export as real top-level bindings: `models.bsn_units` (submodule ref) and `models.get_connection`. Four scripts (`scripts/phase_c_*.py`, `scripts/replay_history_*.py`) do `from models import _sync_bsn_to_stock, recalculate_product_wacc` — these must be genuine bindings in `models/__init__.py`.

### A6m. Monkeypatch landmine (MUST be resolved in phase 11/12 design)
16 sites (13 `setattr(models, ...)` + 3 via `_models` alias) patching 9 unique names. 7 are safe (facade-qualified callers). 2 break silently after a naive split:
1. `get_connection` — patched by 7 tests, but ~130 call sites call it as a bare name; per-submodule `from database import get_connection` bindings are unreachable by a facade patch. Affected tests would silently run against the real DB path.
2. `resolve_pending_mappings` — `approve_pending_suggestion` (suggestions module) calls it bare; `test_approve_pending_suggestion.py:311` patches it to verify a rollback/atomicity regression (historical orphan-product bug). A missed patch = the regression test goes green without testing anything.
Decision options: (a) route those specific call sites through the package object at call time (`import models; models.get_connection()`), preserving patch semantics; or (b) update the ~9 test files to patch the owning submodule. RECOMMENDATION: (b) for `resolve_pending_mappings` (one test), (a) or (b) for `get_connection` — decide in Phase 11 spec, and add an explicit test that the patch actually intercepts (e.g. assert the seeded conn is used) so a silent bypass can never recur.

### A7m. Flask imports in models.py
None (no request/session/current_app). Import order unconstrained by framework state.

## B. Bug/risk findings — for Put to adjudicate (NOT fixed by the refactor)

| # | Sev | Where | Defect | Failure scenario |
|---|-----|-------|--------|------------------|
| B1 | HIGH | `models.py:927-938` `_sync_bsn_to_stock` | history_import compensating-IN guards on file-level `txn_type` instead of per-row `row_txn_type` (SR rows flip to IN at 862) | Importing a historical sales file containing an SR return row adds +2× base_qty to CURRENT stock instead of net zero. No test covers history_import + SR/GR combination |
| B2 | HIGH | `app.py:1211` `upload_db()` full-replace (legacy) | Raw `shutil.move` swaps the live DB without clearing `-wal`/`-shm` sidecars or reloading workers (unlike the WAL-safe backup_restore path); the pre-upload backup at app.py:1206 also uses WAL-unsafe `shutil.copy` (torn-snapshot risk) | Full-replace upload under live gunicorn -w 2 can replay stale WAL frames / serve inconsistent reads until manual restart, with only a possibly-torn backup to fall back on |
| B3 | MED | `models.py:911-924` `_sync_bsn_to_stock` | `platform_deduct = max(1, round(remaining/qps))` deducts a whole platform unit even when the true remainder is under half a unit | `platform_skus.stock` over-drawn vs base-unit reality; self-heals on next marketplace upload (online counter, not ledger), so MED |
| B4 | MED | `app.py:2085-2093` `photos_review_assign()` | DB insert before file move, commit after: a commit failure AFTER a successful move orphans the photo | Disk-full/locked DB at commit time silently drops the photo from the review queue with no product_images row |
| B5 | LOW | `app.py:2836` `customer_geocode()` | No role gate beyond login; live Nominatim call per request, no rate limit | Scripted/repeated clicks can get the shop IP banned by the free OSM geocoder |
| B6 | LOW | `app.py:2746` `_parse_bsn_customers()` | Hardcoded CSV path opened with no existence check | Missing file = unhandled FileNotFoundError 500 on an admin route |
| B7 | LOW | commission_overrides_toggle/delete | Call `clear_override_cache()` raw while new/edit go through `_safe_clear_override_cache` (flash instead of 500) | Same action, two error behaviors; align opportunistically in Phase 7 |

Money paths read closely and found sound: `recalculate_product_wacc`, `import_payments` savepoint-per-record, `get_order_margin`, `match_orders_to_amount`, `get_ap_outstanding`. Security checked clean: photo path traversal contained (`os.path.commonpath`), CSRF on JSON POSTs handled via X-CSRFToken, no SQL string formatting, no cost_price string anywhere in app.py (all cost/margin routes admin/manager/shareholder-gated).

## C. Report-only inventory (no action in this project)

- **Dead code**: app.py `_diff_master_tables()` (1025-1040, never called), `import import_express as express_importer` (2952, never referenced). models.py: `to_base_units` (44), `delete_transactions_by_ids` (1131), `find_payment_candidates` (3098), `get_recent_imports` (1679), `get_pending_suggestion` (5186, singular). Plus previously known: `hr_bank.py` (0 app importers), `import_credit_notes.py` (UI never wired).
- **Intentional legacy redirect stubs (NOT dead)**: `import_weekly`, `express_import`, `express_ar_dashboard`, `express_ap_dashboard`, `payment_status`, `payment_customers` — pure redirects kept for old bookmarks.
- **Live-via-JS false positives** (no url_for, called by hardcoded fetch paths): mapping_suggest/save/suggestion_approve, photos_review*/api_photos_review_queue, api_product_barcodes.
- **Connection leaks**: 92 of 128 self-connecting models.py functions have no try/finally; worst three (long bodies + writes): `import_weekly` (1474), `recalculate_product_wacc` (4556), `import_customers_from_bsn` (4257).
- **VAT idiom duplication**: 10 sites — models.py 2933, 2952, 2978, 2982, 3054, 3109, 3180, 3243, 3258, 6198 (documented-intentional; a helper would be a separate follow-up).
