# Sendy — Sendai-Boonsawat ERP — CLAUDE.md

> **App name: Sendy** (มาจาก Sendai). คือ ERP หลังบ้านของ BSN/Sendai Trading.
> เมื่อ user พูดถึง "Sendy", "Sendy app", "เปิด Sendy" → หมายถึง Flask ERP นี้ (folder `inventory_app/`).
> Folder name `inventory_app/` คงเดิม (internal, ไม่ rename เพื่อหลีกเลี่ยง deployment risk).

> **Skill mirrors:** `/erp-context` (schema/routes/session log), `/erp-formats` (file formats), `/erp-deploy` (Railway), `/erp-permissions` (role/POST whitelist) — ของอ่านจาก workspace root `.claude/commands/`.

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
~/.virtualenvs/erp/bin/pip install -r inventory_app/requirements.txt
```

## Stack
- **Framework**: Flask 3.x (Python 3.9), no ORM
- **Database**: SQLite → `inventory_app/instance/inventory.db`
- **Encoding**: UTF-8 สำหรับ DB, **cp874** สำหรับไฟล์ BSN CSV
- **Deploy**: Railway (gunicorn 2 workers), persistent volume `/data`, healthcheck `/healthz`
- **GitHub**: https://github.com/Zyringe/sendy-erp

## โครงสร้างไฟล์สำคัญ
```
sendy_erp/
  CLAUDE.md             ← this file (app-level authoritative)
  Procfile              ← gunicorn --chdir inventory_app -w 2 -b 0.0.0.0:$PORT app:app
  railway.toml          ← nixpacks + healthcheckPath=/healthz
  pytest.ini, requirements*.txt
  data/
    migrations/         ← 0NN_name.sql + .rollback.sql (latest: 037)
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

## Schema ตาราง (ปัจจุบัน — เวอร์ชัน schema migration 037, 2026-05-07)

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

### Tables เพิ่มเติม (รวม ~ 60+ tables)
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
- **Receivables/Payables**: `received_payments`, `paid_invoices`
- **Misc**: `expense_categories`, `expense_log`, `promotions`, `stock_levels`

## BSN Sync Logic
- import ไฟล์รายสัปดาห์ → parse → บันทึกใน `sales/purchase_transactions` (synced_to_stock=0)
- ต้องผูกรหัส BSN ก่อน (`product_code_mapping`)
- ถ้า BSN unit ≠ product unit_type → ต้องกำหนด ratio ใน `unit_conversions`
- `_sync_bsn_to_stock()` สร้าง transaction IN (ซื้อ) / OUT (ขาย) แล้ว set synced_to_stock=1
- `batch_id='history_import'` → IN+OUT pair (net=0) สำหรับข้อมูลก่อน cutoff 3/3/2569
- redirect flow: import → mapping (ถ้า pending) → unit_conversions (ถ้า pending) → sales view

## Routes (กลุ่มหลัก)

ดู `/erp-context` สำหรับ list ครบ. กลุ่มที่ใช้บ่อย:

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

**Mobile** (blueprint `bp_mobile`, breakpoint 992px): `/m/*`

## สิ่งที่ต้องระวัง

- **Python 3.9**: ไม่รองรับ `int | None` syntax → ใช้ `Optional[int]` หรือไม่ใส่ annotation
- **วันที่ BSN**: Buddhist Era (พ.ศ.) ต้องแปลงก่อนบันทึก
- **ปรับสต็อกหน่วย**: ถ้าเปลี่ยน unit_type → ต้อง multiply quantity_change ใน transactions + stock_levels ด้วย ratio
- **ลบ BSN sync**: (1) ลบ transactions ที่ note LIKE 'BSN%' (2) reset synced_to_stock=0 (3) recalculate stock_levels
- **recalculate stock**: `DELETE FROM stock_levels WHERE product_id=?` แล้ว `INSERT` ใหม่จาก `SUM(quantity_change)`
- **merge product**: UPDATE transactions/mapping/sales/purchase/unit_conversions SET product_id=NEW → recalc stock NEW → DELETE stock_levels OLD → is_active=0 OLD
- **products_full VIEW**: ใช้ในงาน reporting — ห้าม INSERT/UPDATE ผ่าน VIEW
- **Blueprint endpoint naming**: routes ใน blueprint ต้องเรียกด้วย `<bp>.<func>` ทั้งใน `_STAFF_POST_OK` และ `url_for()`
- **werkzeug BuildError** หลังเพิ่ม route ใหม่: restart server ด้วยมือทุกครั้ง — auto-reloader reload template ได้ แต่ URL map ใน memory ยังเก่า
- **Auto-reloader double-startup**: ใช้ `use_reloader=False` ใน dev server config

## Migrations recent

| Mig | Date | What |
|-----|------|------|
| 023 | 2026-05-04 | audit_log + commission_overrides |
| 024 | 2026-05-04 | listing_bundles |
| 025 | 2026-05-05 | product_families + product_images + brands.short_code + products.family_id |
| 026–032 | 2026-05-05 to 06 | brands/colors/packaging/typo cleanup rounds 1–4 + bronze color |
| 033 | 2026-05-07 | products structured columns (series/model/size/color/packaging/condition/pack_variant) + products_full VIEW |
| 034–035 | 2026-05-07 | colors round 5 + packaging extend |
| 036 | 2026-05-07 | pending_product_suggestions |
| 037 | 2026-05-07 | smart_mapping_extras |

> Migration runner: `database.py::init_db()` reads `data/migrations/NNN_*.sql` + `.rollback.sql`. SHA256 + duration_ms recorded in `applied_migrations`. เพิ่ม migration ใหม่: เลข NNN ถัดไป → restart → รันอัตโนมัติ. Rollback: รัน `.rollback.sql` + DELETE จาก `applied_migrations` ด้วยมือ.

## Auth + Deploy
ดู `/erp-permissions` (role/POST whitelist) และ `/erp-deploy` (Railway env, DB sync flow). Production env vars: `SECRET_KEY` (rotated 2026-05-05), `ADMIN_PASSWORD`, `DATA_DIR=/data`. Bootstrap-only: `SKIP_DB_INIT`, `BOOTSTRAP_TOKEN` (unset หลัง first seed).
