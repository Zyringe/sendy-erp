# Sendy — Sendai-Boonsawat ERP — CLAUDE.md

> **App name: Sendy** (มาจาก Sendai). คือ ERP หลังบ้านของ BSN/Sendai Trading.
> เมื่อ user พูดถึง "Sendy", "Sendy app", "เปิด Sendy" → หมายถึง Flask ERP นี้ (folder `inventory_app/`).
> Folder name `inventory_app/` คงเดิม (internal, ไม่ rename เพื่อหลีกเลี่ยง deployment risk).

## Dev Server
```
runtimeExecutable: /Users/putty/.virtualenvs/erp/bin/python
runtimeArgs: ["/Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/app.py"]
port: 5001
```

**หมายเหตุ macOS sandbox**: `mcp__Claude_Preview__preview_start` มักโดน TCC block เพราะอ่าน `~/Documents` ไม่ได้ → start ผ่าน Bash แทน:
```
cd inventory_app && /Users/putty/.virtualenvs/erp/bin/python app.py
```
venv ต้องอยู่นอก `~/Documents` (เช่น `~/.virtualenvs/erp`) เพื่อหลบ sandbox.
Shell shortcuts: `sendy-up` / `sendy-down` / `sendy-log` (logs ที่ `/tmp/sendy.log`).

## First-time setup (เครื่องใหม่)
```
/usr/bin/python3 -m venv ~/.virtualenvs/erp
~/.virtualenvs/erp/bin/pip install -r requirements.txt
```

## Stack
- **Framework**: Flask 3.x (Python 3.9), no ORM
- **Database**: SQLite → `inventory_app/instance/inventory.db`
- **Encoding**: UTF-8 สำหรับ DB, **cp874** สำหรับไฟล์ BSN CSV
- **Deploy**: Railway (gunicorn 2 workers), persistent volume `/data`, healthcheck `/healthz`
- **GitHub**: https://github.com/Zyringe/sendy-erp

## Commands (most common)

```bash
# Run dev server (prefer over Claude Preview MCP — TCC sandbox blocks it)
~/.virtualenvs/erp/bin/python sendy_erp/inventory_app/app.py
# or (when shell aliases are loaded)
sendy-up        # starts server, logs → /tmp/sendy.log
sendy-down      # kills server
sendy-log       # tails /tmp/sendy.log

# Tests (from sendy_erp/ — pytest.ini sets pythonpath=inventory_app .)
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest                            # full suite
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashflow.py     # single file
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest -k vat                     # by name keyword
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest -x -ra                     # stop on first fail

# Manual DB backup (launchd backup is paused — see project_backup_paused memory)
sendy_erp/scripts/backup_db.sh

# Migration: drop a new `NNN_name.sql` + `NNN_name.rollback.sql` into
# data/migrations/ then restart server — runner auto-applies on init_db().
```

## โครงสร้างไฟล์สำคัญ
```
sendy_erp/
  CLAUDE.md             ← this file (app-level authoritative)
  Procfile              ← gunicorn --chdir inventory_app -w 2 -b 0.0.0.0:$PORT app:app
  railway.toml          ← nixpacks + healthcheckPath=/healthz
  pytest.ini, requirements*.txt
  data/
    migrations/         ← 0NN_name.sql + .rollback.sql (latest: **070**)
    source/, source-backup.zip, exports/
  docs/                 ← engineering notes
  scripts/              ← apply_sku_*, parse_*, import_*, backup_db.sh, com.boonsawat.erp.backup.plist
  tests/                ← pytest
  inventory_app/
    app.py              ← routes ส่วนใหญ่ + POST whitelist (~line 109-122)
    models.py           ← business logic + DB queries
    database.py         ← schema + init_db() + migration runner
    parse_weekly.py     ← BSN weekly parser (cp874)
    parse_platform.py   ← Shopee/Lazada/TikTok parser
    bsn_suggest.py      ← smart-mapping suggestions
    commission.py       ← commission engine
    config.py           ← DATABASE_PATH, UPLOAD_FOLDER, SECRET_KEY, sessions
    blueprints/         ← products, supplier_catalogue, mobile
    imports/            ← Express AR/AP parsers
    templates/, static/
    instance/inventory.db
```

## Schema ตาราง (ปัจจุบัน — เวอร์ชัน schema migration 070, 2026-05-21)

### products
```
id, sku(INT), product_name, units_per_carton, units_per_box,
unit_type(default 'ตัว'), hard_to_sell, cost_price, base_sell_price,
low_stock_threshold, is_active, created_at, updated_at,
shopee_stock, lazada_stock,
brand_id, category_id, family_id,                    -- FK (mig 025)
series, model, size, color_code, packaging,          -- structured (mig 033)
condition, pack_variant                              -- structured (mig 033)
```

### product_families *(mig 025)*
`id, family_code, display_name, brand_id, sort_order, note, created_at, updated_at`
- 1 family = 1 catalog card (สินค้าเดียวกันหลายไซส์/หลายสีรวม 1 ใบ)
- ปัจจุบัน count=0 (schema พร้อม รอ populate)

### product_images *(mig 025)*
`id, family_id, sku_id, image_path, presentation_tag, sort_order, note, ...`

### brands *(+short_code mig 030/031)*
`id, code, name, name_th, is_own_brand, sort_order, note, short_code, ...`
- 38 brands, own-brand 3 (Sendai, Golden Lion, A-SPEC)

### products_full (VIEW — mig 033)
LEFT JOIN ของ products + brands + categories + color_finish_codes + stock_levels
→ ใช้ VIEW นี้แทน raw products สำหรับ reporting/UI ทุกครั้ง

### transactions (stock ledger)
`id, product_id, txn_type(IN/OUT/ADJUST), quantity_change(REAL), unit_mode, reference_no, note, created_at`
- Trigger `after_transaction_insert` อัปเดต `stock_levels` อัตโนมัติ
- qty เป็น REAL (ไม่ปัดทศนิยม)

### stock_levels
`product_id, quantity` — ยอดสต็อกปัจจุบัน

### sales_transactions / purchase_transactions (ข้อมูล BSN)
`id, batch_id, date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
 customer/supplier, customer_code/supplier_code, qty, unit, unit_price,
 vat_type, discount, total, net, created_at, synced_to_stock(0/1)`

**Column semantics (verified 2026-04-28):**
- `total` = `unit_price × qty × (1 − line_discount)` — line subtotal, **pre-VAT, pre-doc-discount**
- `net` = line's share หลังหัก doc-level discount (e.g. 2% cash/early-pay)
- ~96.5% rows: `total == net`; ~3.5%: `net = total × 0.98`
- **`net` = ยอดก่อน VAT (ex-VAT) เสมอ** — ใช้ `vat_type` หา **ยอดที่ลูกค้าจ่ายจริง**:
  - `0` = ยกเว้น VAT → ลูกค้าจ่าย `net`
  - `1` = ไม่บวก VAT ตอนเก็บเงิน (เช่น ขายหน้าร้าน/เงินสด) → ลูกค้าจ่าย `net`
  - `2` = **แยก VAT** (ขายต้องเพิ่ม VAT 7%) → ลูกค้าจ่าย **`net × 1.07`**
  - **Idiom เดียวทั้ง codebase**: `CASE WHEN vat_type=2 THEN net*1.07 ELSE net END`
    (models.py · payments_alloc.py · cashflow ar_aging · tests/test_vat_math.py)
  - ⚠️ ก่อน 2026-05-19 doc นี้เขียนกลับด้าน (`1→×1.07, 2→÷1.07`) — ผิด.
    payments_alloc/cashflow เคยใช้ `SUM(net)` เปล่า ทำให้บิล `แยก VAT` ที่จ่าย
    ครบทุกใบดู "จ่ายเกิน 7%" → ยอดเครดิตค้างคืนลูกค้าปลอม ~฿446k. แก้แล้ว.
- **Revenue column for analysis: `net`** (ex-VAT, after all discounts) — VAT 7%
  ของ `vat_type=2` เป็น **ภาษีขายที่ต้องนำส่ง ไม่ใช่รายได้**; แตะเฉพาะการกระทบ
  ยอดเงินสด/ลูกหนี้ (`billed`/`collected`) ห้ามเอาไปคิดเป็นรายได้
- **Parser fixed 2026-04-28** — `_DISCOUNT_COL` regex ขยาย char class รองรับ `.` และ `%` ใน line/doc-level discount.
  - Bugs ที่ fix: (1) line discount แบบ decimal baht (`32.00`) shift column ผิด, (2) doc-level discount แบบ `%` (`2%`) truncate net.
  - Re-import historical files แล้วเพื่อแก้ ~695 rows.

### product_code_mapping
`id, bsn_code, bsn_name, product_id, is_ignored, created_at`
- Authoritative — อย่าเดา SKU จาก raw_name หรือ "(1ขีด)" suffix
- Duplicate import check: `(doc_base + bsn_code + unit_price)` weekly / `(doc_no + bsn_code)` history

### unit_conversions
`id, product_id, bsn_unit, ratio, created_at` — UNIQUE(product_id, bsn_unit)
- BSN sync ข้ามแถวที่ไม่มี conversion → unsynced ค้าง
- ratio: 1 BSN unit = ratio × product unit_type

### product_locations
`id, product_id, floor_no, created_at` — สินค้า 1 ชนิดมีได้หลายแถว (multi-location)

### pending_product_suggestions *(mig 036)*
รหัส BSN ที่ smart-mapping ยังไม่ได้ผูก — ใช้คู่กับ `/mapping/suggestions/<id>/approve`

### Tables เพิ่มเติม (รวม ~ 70+ tables)
- **Audit/system**: `audit_log` (mig 023), `applied_migrations`, `import_log`, `users`
- **Taxonomy**: `categories`, `color_finish_codes`, `product_attributes`, `product_brand_map`, `product_barcodes`
- **Geography/sales rep**: `regions`, `salespersons`, `customer_regions`, `customers`, `suppliers`, `companies`
- **Cost/Price**: `product_cost_ledger`, `product_price_history`, `product_price_tiers`
- **Commission**: `commission_assignments`, `commission_overrides`, `commission_payouts`, `commission_tiers`
- **Manufacturing conversions**: `conversion_formulas`, `conversion_formula_inputs`, `conversion_cost_log`
- **Supplier catalog**: `supplier_catalogue_items/versions/price_history`, `supplier_product_mapping`, `supplier_quick_updates`
- **Purchase orders**: `purchase_orders`, `purchase_order_lines`, `po_receipts`, `po_sequences`
- **Express (Sendai Trading)**: `express_sales`, `express_ar_outstanding`, `express_credit_notes(_lines)`, `express_payments_in/out`, `express_payment_in_invoice_refs`, `express_payment_out_receive_refs`, `express_import_log`
- **Ecommerce**: `ecommerce_listings`, `platform_skus`, `listing_bundles`
- **Receivables/Payables**: `received_payments`, `paid_invoices`; per-IV upsert via mig 058 (adds `received_payments.amount_applied` for per-invoice allocation); `credit_note_imports` (mig 059), `credit_note_amounts` (mig 062 — authoritative per-SR from ใบลดหนี้ master, replaces `sales_transactions.SR.net` for credit math), `sr_writeoffs` (mig 060)
- **HR** (mig 054 — 9 tables): `employees`, `employee_salary_history`, `leave_types`, `employee_leave_entitlements`, `leave_requests`, `payroll_runs`, `payroll_items`, `hr_config`, `company_holidays`; `salary_advances` added mig 057
- **Cashbook** (mig 055–056): `cashbook_accounts`, `cashbook_categories`, `cashbook_transactions` (`is_transfer` flag added mig 056)
- **Unit aliases** (mig 064): `bsn_unit_alias` — normalizes Express unit strings before resolver matching
- **Misc**: `expense_categories`, `expense_log`, `promotions`, `stock_levels`

## BSN Sync Logic
- import ไฟล์รายสัปดาห์ → parse → บันทึกใน `sales/purchase_transactions` (synced_to_stock=0)
- ต้องผูกรหัส BSN ก่อน (`product_code_mapping`)
- ถ้า BSN unit ≠ product unit_type → ต้องกำหนด ratio ใน `unit_conversions`
- `_sync_bsn_to_stock()` สร้าง transaction IN (ซื้อ) / OUT (ขาย) แล้ว set synced_to_stock=1
- `batch_id='history_import'` → IN+OUT pair (net=0) สำหรับข้อมูลก่อน cutoff 3/3/2569
- redirect flow: import → mapping (ถ้า pending) → unit_conversions (ถ้า pending) → sales view

## Denormalized cache contracts (read before touching payment math)

Two tables hold derived values that the app must keep in sync with their source ledger. Drift here causes silent finance bugs.

### `payment_amounts` (mig 058)
- **Source of truth:** `received_payments` per-IV allocations.
- **Invariant:** for every `received_payments` row with allocations, there is exactly one `payment_amounts` row per (payment_id, doc_no) with `amount_applied` summing to the payment.
- **Drift signal:** `SUM(payment_amounts.amount_applied) ≠ received_payments.amount` for the same payment_id.
- **Recovery:** rerun `payments_alloc.allocate_fifo()` for the affected customer; it is idempotent and recomputes from scratch.
- **History:** drift caused phantom-credit ฿446k bug (fixed by VAT-aware `billed` formula, commit 339e92a in PR #27).

### `credit_note_amounts` (mig 062)
- **Source of truth:** ใบลดหนี้ master CSV — NOT `sales_transactions` SR rows (those are pre-VAT line totals, not the customer-facing CN amount).
- **Invariant:** every CN doc_no has exactly one row in `credit_note_amounts` with the master-CSV amount.
- **Drift signal:** missing doc_no, or `amount` differs from the CSV.
- **Recovery:** rerun `/payment-status` CN import (PR #36/#37 two-step preview/confirm UI).
- **History:** drift caused phantom-overpay ฿105k bug (fixed by mig 062, commit 2535c86 in PR #27).

## Recent business modules (mig 054–064, shipped May 2026)

> ทั้งกลุ่มนี้เป็น **production code** ที่ระเบียบ ledger + reporting พึ่งพา. อย่าเสนอ rewrite/duplicate ก่อนอ่าน module CLAUDE-MD ใน blueprints/ แต่ละตัว.

- **HR (Phase 1)** — `bp_hr` blueprint. SSO 5%, probation-raise next-full-month, เบี้ยขยัน auto-forfeit, manager=read-all/admin=write. Phase 4 `/accounting` NOT started.
- **Cashbook** — `import_cashbook` (`/cashbook/import`, vat flag). Excel round-trip; NoVat imported, Vat workbook pending. Transfer-acct auto-detect via `is_transfer`. Re-importing Salary_Sheet overwrites employee nicknames → add override before frequent re-imports.
- **Cash Flow + payments allocation** — `/cashflow` dashboard. `payments_alloc.py::allocate_fifo()` allocates received_payments oldest-first; legacy NULL allocations = fully-paid. Hook `cashflow.revenue_by_month` feeds the Revenue dashboard (`/revenue`, shipped — `revenue_dashboard` route).
- **Credit-note math** — `credit_note_amounts` is **authoritative**. Don't compute credit from `sales_transactions` SR rows (gross ≠ ใบลดหนี้ master). `collected = ΣIV(+) − ΣSR(−)` using authoritative CN amounts. `import_credit_notes.py` exists but UI route not wired (gap).
- **Brand-kind unit-aware resolver** (mig 061–064, PR #29 merged) — product resolution must consider `brand_kind` + unit; pure `bsn_code` join overstates split-code lines. **Open: commission engine `_BASE_QUERY` still joins by `bsn_code` only** (GitHub issue #30 — money path, validate before merging).

## Routes (กลุ่มหลัก)

List ครบดูได้จาก `git grep -nE "@.*\.route" inventory_app/` หรือ `flask routes` ภายใน app context. กลุ่มที่ใช้บ่อย:

**Core/admin**: `/`, `/healthz`, `/login`, `/logout`, `/users`, `/admin/simulate-role`, `/admin/exit-simulate`, `/admin/toggle-db-routes`, `/admin/upload-db`, `/admin/upload-db/confirm` (selective), `/admin/download-db`, `/bootstrap/upload-db` (token-gated)

**Products** (blueprint `bp_products`): `/products`, `/products/<id>`, `/products/<id>/{stock-in,stock-out,adjust,location,online-stock,pricing,trade}`, `/api/products/search`, `/api/products/<id>/barcodes`, `/labels`

**Trade**: `/trade-dashboard`, `/sales`, `/sales/doc/<doc_base>`, `/purchases`, `/purchases/doc/<doc_base>`, `/transactions`

**BSN flow**: `/import-weekly`, `/mapping`, `/mapping/suggest/<bsn_code>`, `/mapping/save`, `/mapping/suggestions/<id>/approve`, `/unit-conversions`, `/unit-conversions/{save,edit}`, `/review-transactions`

**Customers/Suppliers**: `/customers*`, `/customer/<name>`, `/customers/{map,import-bsn,bulk-reassign}`, `/customers/geocode/<code>`, `/api/customers/geojson`, `/regions`, `/suppliers`, `/supplier/<name>`

**Payments**: `/payment-status*`, `/import-payments`

**Ecommerce**: `/ecommerce*`

**Conversions (manufacturing)**: `/conversions*`

**Commission**: `/commission*`, `/commission/overrides*`

**Express (Sendai Trading)**: `/express/{import,ar,ap}`, `/express/ar/customer/<code>`

**HR** (blueprint `bp_hr`): `/hr/*` — employees, salary history, payroll runs, leave management

**Cashbook**: `/cashbook`, `/cashbook/import` (vat / novat workbooks)

**Cash Flow**: `/cashflow` (AR aging, allocations, payments-in dashboard)

**Mobile** (blueprint `bp_mobile`, breakpoint 992px): `/m/*`

## สิ่งที่ต้องระวัง

- **Python 3.9**: ไม่รองรับ `int | None` syntax → ใช้ `Optional[int]` หรือไม่ใส่ annotation
- **วันที่ BSN**: Buddhist Era (พ.ศ.) ต้องแปลงก่อนบันทึก
- **ปรับสต็อกหน่วย**: ถ้าเปลี่ยน unit_type → ต้อง multiply quantity_change ใน transactions + stock_levels ด้วย ratio
- **ลบ BSN sync**: (1) ลบ transactions ที่ note LIKE 'BSN%' (2) reset synced_to_stock=0
  - ⚠️ ก่อน mig 080 ต้อง recalculate stock_levels ใน step 3 ด้วย — ตอนนี้ trigger `after_transaction_delete` ทำให้อัตโนมัติ (ห้ามทำซ้ำ จะ double-decrement)
- **recalculate stock (drift recovery เท่านั้น)**: `DELETE FROM stock_levels WHERE product_id=?` แล้ว `INSERT` ใหม่จาก `SUM(quantity_change)`
  - ใช้เมื่อ stock_levels drift จาก ledger (e.g., legacy ก่อน mig 080 หรือ direct sqlite3 cleanup ที่ปิด trigger). **ไม่ใช่** steady-state operation
- **merge product**: UPDATE transactions/mapping/sales/purchase/unit_conversions SET product_id=NEW → DELETE stock_levels OLD → is_active=0 OLD
  - ⚠️ ก่อน mig 080 ต้อง "recalc stock NEW" ระหว่างกลาง — ตอนนี้ trigger `after_transaction_update` decrement OLD's stock + increment NEW's stock ทีละ row อัตโนมัติ (ห้าม recalc ซ้ำ)
  - **ลำดับสำคัญ:** UPDATE transactions ก่อนเสมอ (ให้ trigger จัดการ stock_levels) แล้วค่อย DELETE stock_levels OLD. ถ้า DELETE ก่อน → trigger ที่ตามมาเจอ row ว่าง → silent miss → drift
- **stock_levels auto-maintenance (mig 080 onwards)**: INSERT/UPDATE/DELETE บน transactions ทำให้ stock_levels sync ผ่าน 3 business triggers (`after_transaction_{insert,update,delete}`). Manual `UPDATE stock_levels` ใน migration / one-off SQL = double-count ⛔ — ใช้เฉพาะ drift-recovery flow ข้างต้น
- **products_full VIEW**: ใช้ในงาน reporting — ห้าม INSERT/UPDATE ผ่าน VIEW
- **Blueprint endpoint naming**: routes ใน blueprint ต้องเรียกด้วย `<bp>.<func>` ทั้งใน `_STAFF_POST_OK` และ `url_for()`
- **werkzeug BuildError** หลังเพิ่ม route ใหม่: restart server ด้วยมือทุกครั้ง — auto-reloader reload template ได้ แต่ URL map ใน memory ยังเก่า
- **Auto-reloader double-startup**: ใช้ `use_reloader=False` ใน dev server config
- **CSRF protection**: ทุก POST form template ต้องมี `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">` หลัง `<form method="post">`. flask-wtf `CSRFProtect(app)` ปฏิเสธ POST ที่ไม่มี token (HTTP 400 → จัด redirect+flash โดย global handler). Production = on by default; tests รันด้วย `WTF_CSRF_ENABLED=False` (set ใน `tests/conftest.py`). Route ใหม่ POST ไม่ต้องเพิ่ม decorator — ป้องกันอัตโนมัติ. Exempt เฉพาะ `/bootstrap/upload-db` (gated ด้วย BOOTSTRAP_TOKEN, ไม่มี session).

## Migrations (latest: 080 — see `data/migrations/` for canonical files)

| Mig | Date | What |
|-----|------|------|
| 023 | 2026-05-04 | audit_log + commission_overrides |
| 024 | 2026-05-04 | listing_bundles |
| 025 | 2026-05-05 | product_families + product_images + brands.short_code + products.family_id |
| 026–032 | 2026-05-05/06 | brands/colors/packaging/typo cleanup rounds 1–4 + bronze color |
| 033 | 2026-05-07 | products structured columns (series/model/size/color/packaging/condition/pack_variant) + products_full VIEW |
| 034–037 | 2026-05-07 | colors round 5, packaging extend, pending_product_suggestions, smart_mapping_extras |
| 038 | 2026-05-08 | basic_color_codes |
| 039 | 2026-05-17 | sku_codes_and_categories |
| 040 | 2026-05-17 | categories_short_codes_and_new |
| 041 | 2026-05-17 | more_broad_categories |
| 042 | 2026-05-17 | color_variants_round6 |
| 043 | 2026-05-17 | product_families_display_format |
| 044 | 2026-05-17 | **drop_redundant_tables** (destructive — check before rerun) |
| 045 | 2026-05-17 | brand "Jolan" |
| 046 | 2026-05-17 | products.material column |
| 047 | 2026-05-17 | reflective color codes |
| 048 | 2026-05-17 | apparel categories |
| 049 | 2026-05-17 | color codes round 7 |
| 050 | 2026-05-17 | 3rd-party brands (batch 1) |
| 051 | 2026-05-17 | sub_cat_short_code |
| 052 | 2026-05-17 | 3rd-party brands (batch 2) |
| 053 | 2026-05-17 | customer geo-map fields (gmap_lat/lng/place_id) |
| **054** | 2026-05-18 | **HR module (9 tables) + bp_hr** |
| **055** | 2026-05-18 | **Cashbook (accounts/categories/transactions)** |
| 056 | 2026-05-18 | cashbook.is_transfer |
| 057 | 2026-05-18 | salary_advances |
| **058** | 2026-05-18 | **payment_amounts (per-IV allocation) → enables /cashflow** |
| 059 | 2026-05-18 | credit_note_imports |
| 060 | 2026-05-18 | sr_writeoffs |
| 061 | 2026-05-19 | mapping_unit_aware (rebuilds product_code_mapping with unit awareness) |
| **062** | 2026-05-19 | **credit_note_amounts (authoritative per-SR) → fixes ฿105k phantom-overpaid bug** |
| 063 | 2026-05-20 | brand_kind_unit_aware_trigger |
| 064 | 2026-05-20 | bsn_unit_alias + express_unit normalize enforced (PR #29 merged) |
| **065** | 2026-05-20 | **ar_followup_log + outreach workspace** (PR #38 merged) |
| 066 | 2026-05-20 | data-quality cleanup (โหล/กล่อง normalize, 33 fixes) |
| **067** | 2026-05-20 | **drop cashbook_vat_flag** (NoVat-only by design, PR #39 merged) |
| 068 | 2026-05-21 | drop express_sales.brand_kind (write-only cache removal, PR #44 merged) |
| **069** | 2026-05-21 | **products.units_per_carton/box NOT NULL DEFAULT 1** (this PR) |
| **070** | 2026-05-21 | **audit_log triggers on transactions + received_payments** (this PR) |
| 071–076 | 2026-05-21 | HR audit migs (reopen + draft banner + image coverage + money-path audit) |
| 077 | 2026-05-23 | data-quality brand+kg-rename from tracker XLSX |
| 078 | 2026-05-23 | data-quality mapping+UC cleanup + pid 771 stock ADJUST (rollback updated 2026-05-25 post-mig-080) |
| **079** | 2026-05-25 | **audit_log UPDATE/DELETE triggers on transactions** (closes mig 070's append-only-only gap) |
| **080** | 2026-05-25 | **stock_levels integrity on transactions UPDATE/DELETE** (`after_transaction_update` + `after_transaction_delete` — mirrors INSERT trigger; auto-reconciles stock on mutations) |

> Migration runner: `database.py::init_db()` reads `data/migrations/NNN_*.sql` + `.rollback.sql`. SHA256 + duration_ms recorded in `applied_migrations`. เพิ่ม migration ใหม่: เลข NNN ถัดไป → restart → รันอัตโนมัติ. Rollback: รัน `.rollback.sql` + DELETE จาก `applied_migrations` ด้วยมือ.
>
> **⚠ Applied migrations are immutable by default.** Fix bugs by writing a new forward migration with a higher NNN — that's the safe path that keeps prod/dev/restore environments in sync.
>
> **Escape hatch (rare):** runner is filename-keyed and does NOT re-check sha256, so in-place editing an already-applied mig is *technically possible* without bumping the number. Only acceptable when **all** of these hold: (1) edit is rerun-safe (idempotent), (2) the change hasn't been deployed to Railway yet, (3) no other dev/restore DB has the old filename in `applied_migrations`. Otherwise prod will silently keep the old SQL while fresh environments run the new SQL → schema drift. When in doubt, write a new mig.

## Auth + Deploy
- **Auth/permissions**: role enum + POST whitelist live in `inventory_app/auth.py` + `inventory_app/permissions.py`. Read those for the source of truth.
- **Deploy**: Railway project linked to `Zyringe/sendy-erp` main; PR-merge auto-deploys. DB sync flow via `/admin/upload-db` (selective master tables only) — never push full DB over friend's rows.
- **Production env vars**: `SECRET_KEY` (rotated 2026-05-05), `ADMIN_PASSWORD`, `DATA_DIR=/data`. Bootstrap-only: `SKIP_DB_INIT`, `BOOTSTRAP_TOKEN` (unset หลัง first seed).
