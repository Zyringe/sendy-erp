# SKU Code Naming Rule

> Locked 2026-05-12; updated 2026-05-28 (mig 087 — dropped `material` slot; `packaging` split into `packaging_th` + `packaging_short`; pack_variant=1 suppression rule added). ใช้กับ `products.sku_code` ของทุกสินค้า (own-brand + 3rd-party).
> Companion doc: [`product_name_naming_rule.md`](product_name_naming_rule.md) (สำหรับ `products.product_name` — Thai display name)
> ตั้งค่าครั้งเดียว ใช้ตลอด — ห้ามเปลี่ยนทุก quarter.

---

## ภาพรวม

**`product_name`** = ชื่อไทย human-readable ที่ลูกค้า/พนักงานเห็น (display)
**`sku_code`** = identifier ASCII English-only สำหรับ catalog/listing/inventory/integration (machine + label/barcode)

ทั้ง 2 ฟิลด์อยู่ใน `products` table — กฎคนละชุด แต่ derive จาก source data เดียวกัน

---

## Template

```
[cat]-[subcat?]-[brand]-[series?]-[model?]-[size?]-[color?]-[pkg?]-[condition?]-[pack_variant?]
```

ทั้งหมด **10 slots** (รวม cat-subcat-brand เป็น 3 mandatory; ที่เหลือ optional). Material slot dropped 2026-05-28 (mig 087) — never wired into `build_sku_code` in practice; use natural-language descriptor (`สแตนเลส`, `เหล็ก`) in `product_name` instead.
ตัวอย่างเต็มๆ จาก DB:
- `BLT-MYM-SD-#230-4in-AC-UN` (slot 1, 2, 3, 5, 6, 7, 8 ใช้; pack_variant=1 suppressed)
- `BLT-MYM-SD-#230-4in-AC-UN-2` (เหมือนด้านบน + slot 10 = 2)
- `HNG-HDOR-SD-JAC-#410-PN` (slot 1, 2, 3, 4, 5, 8 ใช้)
- `DSC-GL-AAA-14in-S279A-BLK` (slot 1, 3, 4, 5/6, 7 ใช้)
- `SND-FLPH-GL-B-#40` (slot 1, 2, 3, 4, 5 ใช้)

---

## กฎ 10 ข้อ

### Format rules (อ่านก่อน)

| # | กฎ | ตัวอย่างผิด → ถูก |
|---|---|---|
| 1 | **English/ASCII only** — A-Z, 0-9, และ symbols `- _ # . / +` เท่านั้น | `บานพับ-SD-#170` → `HNG-HDOR-SD-#170...` |
| 2 | **NULL-aware** — slot ที่ค่าเป็น NULL/empty ต้อง **skip** ห้าม pad ด้วย `-` ติดกัน | `HNG-HDOR-SD--#410-` → `HNG-HDOR-SD-#410` |
| 3 | **Separator** = `-` (dash) ระหว่าง slot · space ใน 1 slot → แทนด้วย `_` | `HNG-HDOR-SD-แกน เล็ก` → `HNG-HDOR-SD-AXLE_SM` (or omit Thai) |
| 4 | **Minimum 2 segments** — ทุก SKU ต้องมีอย่างน้อย `cat + 1 อื่น` (ห้ามมีแค่ `cat` slot เดียว) | `INT` → `INT-OTH` (เติม subcat) |
| 5 | **Drop sub_cat_short ถ้าซ้ำกับ cat_short** | `BOX-BOX-SD-...` → `BOX-SD-...` |
| 6 | **Sub_cat_short** = single segment, 3-8 chars, **ห้ามมี internal dash** | `HDOR-LATCH` → `HDOR` หรือ `LATCH` (เลือก 1) |
| 7 | **Disambiguator** — ถ้า 2 SKUs ได้ sku_code ซ้ำกัน → suffix `-{id}` (product_id; `products.sku` ถูก drop ใน mig 097) เฉพาะ SKU ที่ collide. **Priority (D7, 2026-07-07): ตัว active ชนะ bare code**, inactive/ผู้แพ้ห้อย `-{id}`; status เดียวกัน → pid ต่ำกว่าชนะ | `BLT-SD-230-4in-AC-UN` (สมมติ pid 1 + pid 2) → pid 1: `BLT-SD-230-4in-AC-UN-1`; pid 2: `BLT-SD-230-4in-AC-UN-2` |

### Slot-by-slot rules

| # | Slot | บทบาท | Source | ตัวอย่าง |
|---|---|---|---|---|
| 1 | **cat** | category short code (3-4 char) | `categories.short_code` via `category_id` | BLT (กลอน), HNG (บานพับ), DSC (แผ่นตัด), SND (กระดาษทราย) |
| 2 | **subcat** | sub-category short code (3-8 char single segment) | `products.sub_category_short_code` | HDOR (บานพับประตู), HSST (บานพับ SUS304), FLPH (กระดาษทรายเปียก) |
| 3 | **brand** | brand short code | `brands.short_code` via `brand_id` | SD (Sendai), GL (Golden Lion), AS (A-SPEC), PHO (ใบโพธิ์ทอง) |
| 4 | **series** | series identifier — **English-only**; Thai series → omit จาก sku_code | `products.series` | JAC (บานพับ #410 แหวนทองเหลือง), B/A (กระดาษทราย grit grades), AAA (แผ่นตัด premium grade) |
| 5 | **model** | model code | `products.model` | `#410`, `#230`, `#S-3215`, `230` (ไม่ต้องมี `#` ก็ได้) |
| 6 | **size** | physical dimension | `products.size` | 4in, 14in, 4x3x2mm, 105×1.2mm |
| 7 | **color** | color/finish code | `products.color_code` via `color_finish_codes.code` | AC (รมดำ), GP (ทองเคลือบ), NK (นิกเกิล), SS (สแตนเลส) — ดู `color_finish_codes` table |
| 8 | **pkg** | packaging short code (direct column read, no dict lookup at gen time) | `products.packaging_short` (mig 087; backfilled from `packaging_th`) | UN (ตัว), PN (แผง), BG (ถุง), HP (แพ็คหัว), PP (แพ็คถุง), PK (แพ็ค), SP (ซอง), SC (อัดแผง), TB (หลอด), DZ (โหล), C60 (1กลมี60ใบ) |
| 9 | **condition** | condition note (3-letter code) | derived from product_name suffix `(เก่า)` etc. | BLM (ไม่สวย), DEF (ตำหนิ), BXD (กล่องไม่สวย), OLD (เก่า), RPK (รีแพ็ค), NPT (ไม่มีน็อต), WBP (แผงอ่อน), NSP (ไม่สกรีน), OMD (แบบเก่า), EXP (หมดอายุ), EXP0727 (EXP:07/2027) |
| 10 | **pack_variant** | numeric pack-size variant suffix | `products.pack_variant` | 2, 3, 4, ... (e.g. `BLT-SD-230-4in-AC-UN-2` = variant 2). **`pack_variant=1` is suppressed in both `sku_code` AND `product_name`** — default variant has no suffix. Show only when ≥ 2. |

### Series slot — special clarification (2026-05-14)

- ✅ **English series code** (JAC, B, A, AAA) → **ใส่ใน sku_code slot 4** + เก็บใน `products.series` column
- ❌ **Series สัญลักษณ์ล้วน** (`+`/`-` หัวไขควง) → **omit จาก sku_code** (2026-07-07: กัน segment `-+-`/`---`; subcat/ชื่อบอกหัวอยู่แล้ว — ดู `_series_segment`)
- ❌ **Thai series description** (`แกนเล็ก`, `จุกทอง`, `แกนใหญ่ยอดกลม`) → **omit จาก sku_code** (ใส่ใน series column เท่านั้น)
- ไม่บังคับให้ทุก series ต้องมี English code — แต่ถ้ามี → ต้องใส่ใน sku_code
- ถ้าจะ promote Thai series → English (เช่น "แกนเล็ก" → SHFT-S) ต้อง update ทั้ง series column + sku_code พร้อมกัน

### Condition slot — insertion rule

- Condition code (BLM/DEF/EXP/...) อยู่ใน **slot 10** ก่อน pack_variant
- ถ้าไม่มี pack_variant → condition จะ append ที่ end
- Code ออกแบบไม่ชน packaging code (UN/PN/BG/HP/PP/PK/SP/SC/TB/DZ/C60) หรือ brand/cat short
- EXP แบบมีวันที่: `EXP{MMYY}` 4 หลัก (เดือน 2 หลัก + ปี 2 หลักท้าย พ.ศ.) เช่น EXP0727 = EXP 07/2570

### Disambiguator strip rule

ถ้า SKU มี trailing `-{sku}` AND มี **sibling row** ที่มี bare code (ไม่มี `-{sku}`) → **strip disambiguator ออกก่อน append condition code**

Example:
- pid 39: `INT-39` (disambig version)
- pid 40: `INT` (bare)
- ถ้าจะ append condition (e.g., OLD) → `INT-39-OLD` ❌ → `INT-OLD` ✅ (strip 39 because bare sibling exists)

**Not stripped** ถ้าไม่มี sibling — เช่น `INT-875` (pid 875) — keep 875 because มันคือ model number ไม่ใช่ disambig

---

## Available codes (canonical lists)

### Categories (cat_short)
ดู `categories.short_code` column ใน DB — ตัวอย่าง:
BLT (กลอน), HNG (บานพับ), DKB (ลูกบิด), LCK (กุญแจ), HDL (มือจับ), DSC (แผ่นตัด), DDS (ใบตัดเพชร), GDS (แผ่นเจียร), BLT/SCR (น็อต), ANC (พุก/สมอ), STR (สายเอ็น), GLS (กาวซิลิโคน), RLR (ลูกกลิ้งทาสี), NLS (ตะปู), HRD (กิ๊ปรัด), HSAW (โฮลซอ), SPR (สีสเปรย์), BRH (แปรง), APR (ผ้ากันเปื้อน), SHI (เสื้อ), UMB (ร่ม), TLT (อุปกรณ์ห้องน้ำ), RUB (ยาง), BOX (กล่อง), INT (อินเตอร์/อื่นๆ), SND (กระดาษทราย), OTH (อื่นๆ)

### Brands (brand_short)
ดู `brands.short_code` column ใน DB — ตัวอย่าง:
SD (Sendai/S/D), GL (Golden Lion/สิงห์ทอง), AS (A-SPEC), PHO (ใบโพธิ์ทอง), DRAG (มังกรคู่), RICE (ข้าวสาลี), CROC (จระเข้), HCOP (ข้าวฟ่าง), KC, MOSU, LAMY, SCALA, ATTA, BULL, BELL, SHARK, TIGER, NRK, CK, NRH, AZUM, CRC, DWSL, HITOP, IGIP, LBTY, MXBND, PSW, STAN, SUNCO, TRANE, ANS, TOA — รวม 30+ brands

### Colors (color_code)
ดู `color_finish_codes` table — 49 codes:
AB, AC, ALM, BLK, BLU, BN, BN/AC, BN/PB, BRN, BZ, CR, CRM, DBK, GLD, GP, GRN, GRY, HMT (ลายฆ้อน), IVY, JBB, JSN, LBK, LGY, MAC, MBK, MIX, NAT, NK, ORG, PAB, PAC, PB, PNK, POR (ลายคราม), PRP, RED, REF (สะท้อนแสง), SB, SB/PB, SB/WB, SKY, SLV, SN, SS, SS/BK, TEA, TRN, WHT, YEL

### Packaging (pkg)
UN (ตัว), PN (แผง), BG (ถุง), HP (แพ็คหัว), PP (แพ็คถุง), PK (แพ็ค), SP (ซอง), SC (อัดแผง), TB (หลอด), DZ (โหล), C60 (1กลมี60ใบ — กรณีพิเศษ)

### Condition (3-letter codes)
| Thai | EN | Code |
|---|---|---|
| ไม่สวย | cosmetic blemish | BLM |
| ตำหนิ | defective | DEF |
| กล่องไม่สวย | box damaged | BXD |
| เก่า | old stock | OLD |
| รีแพ็ค | repacked | RPK |
| ไม่มีน็อต | missing parts | NPT |
| แผงอ่อน | weak blister panel | WBP |
| ไม่สกรีน | no screen print | NSP |
| แบบเก่า | old model | OMD |
| หมดอายุ | expired (undated) | EXP |
| EXP:MM/YYYY | dated expiration | EXP{MMYY} |

### Sizes (free-form abbreviations)
SMA (SMALL), MED (MEDIUM), LAR (LARGE) — สำหรับสินค้าที่ไม่มี numeric size (เช่น เสื้อ/ร่ม)
Inch: `4in`, `1.5in`; mm: `120mm`; cm: `5cm`; dimensions: `4x3x2mm`, `105×1.2mm`

---

## ตัวอย่างการ derive sku_code

### Example 1 — บานพับ JAC แหวนทองเหลือง

**Input (DB):**
- category: บานพับ (cat_short=HNG)
- sub_category: บานพับประตู (sub_cat_short=HDOR)
- brand: Sendai (brand_short=SD)
- series: JAC
- model: #410
- size: (none)
- color_code: (none)
- packaging_th: แผง · packaging_short: PN

**Derived sku_code:**
```
HNG-HDOR-SD-JAC-#410-PN
```

### Example 2 — กลอนมะยม Sendai #230 4 นิ้ว สีรมดำ pack_variant=1 (default → hidden)

**Input:**
- cat: กลอน → BLT
- sub_cat: กลอนมะยม → MYM
- brand: Sendai → SD
- model: #230
- size: 4in
- color: รมดำ → AC
- pkg: ตัว → UN
- pack_variant: 1   ← **suppressed** (default)

**Derived:**
```
BLT-MYM-SD-#230-4in-AC-UN
```

(pack_variant=2 would yield `BLT-MYM-SD-#230-4in-AC-UN-2`.)

### Example 3 — แผ่นตัด Golden Lion AAA 14 นิ้ว สีดำ

**Input:**
- cat: แผ่นตัด → DSC
- brand: Golden Lion → GL
- series: AAA
- model: (combined with size)
- size: 14in
- model code: S279A
- color: BLK

**Derived:**
```
DSC-GL-AAA-14in-S279A-BLK
```

### Example 4 — กระดาษทรายฟอง grade B grit #40

**Input:**
- cat: กระดาษทราย → SND
- sub_cat: ฟองน้ำ → FLPH
- brand: Golden Lion → GL
- series: B (grade)
- model: #40 (grit)

**Derived:**
```
SND-FLPH-GL-B-#40
```

---

## Generator function

> Implementation: `sendy_erp/inventory_app/sku_code_utils.py` (build_sku_code function)

ลำดับการ build:
1. Concat slots ตาม order (cat → pack_variant)
2. Skip slot ที่เป็น NULL/empty (NULL-aware)
3. Drop sub_cat_short ถ้าเท่ากับ cat_short
4. Check collision กับ existing `products.sku_code`
5. ถ้า collide → append `-{id}` disambiguator (active ชนะ bare — D7)
6. ถ้า strip rule trigger → strip disambig ก่อน insert condition

---

## Migration history

| Migration | Date | Change |
|---|---|---|
| 039-045 | 2026-05-08 | เพิ่ม `products.sku_code` column + initial generator |
| 046 | 2026-05-12 | เพิ่ม `products.material` column |
| 047 | 2026-05-12 | เพิ่ม REF (สะท้อนแสง) ใน color_finish_codes |
| 048 | 2026-05-12 | เพิ่ม 5 categories: APR, SHI, UMB, TLT, RUB |
| 049 | 2026-05-12 | เพิ่ม POR (ลายคราม) + HMT (ลายฆ้อน) colors; rename SS-BK → SS/BK |
| 050 | 2026-05-12 | เพิ่ม 7 3rd-party brands: PHO, DRAG, RICE, CROC, HCOP, KC, MOSU |
| 051 | 2026-05-12 | เพิ่ม `products.sub_category_short_code` column |
| 052 | 2026-05-12 | เพิ่ม 22 more 3rd-party brands |
| 087 | 2026-05-28 | drop `products.material` column; rename `packaging` → `packaging_th`; add `packaging_short`. 11-slot rule → 10-slot. pack_variant=1 suppression added. |

State หลังจาก migration 052 (2026-05-12):
- 1,963 products total
- 0 empty / 0 Thai / 0 duplicate sku_codes
- 99%+ clean
- ~110 disambig suffixes ที่ legitimate (ไม่ใช่ collision artifact)

---

## ห้าม / ต้อง — สรุป

### ✅ ต้อง
- ASCII English-only (A-Z, 0-9, `- _ # . / +`)
- NULL-aware skip
- Min 2 segments (อย่างน้อย cat + 1)
- Drop subcat ถ้าซ้ำ cat
- Sub_cat_short = single segment, ไม่มี internal dash
- ใช้ condition code ที่ approved (BLM/DEF/EXP/etc.) ไม่ใช่ free-form

### ❌ ห้าม
- ห้ามมี Thai chars ใน sku_code
- ห้าม pad empty slot ด้วย `--`
- ห้ามใช้ space (แทนด้วย `_`)
- ห้ามใส่ Thai series ใน sku_code (เก็บใน series column เท่านั้น)
- ห้าม manual disambig — ใช้ collision check + auto `-{sku}` suffix เท่านั้น
- ห้ามใช้ pack_variant สำหรับสิ่งที่ไม่ใช่ pack size (เช่น สี/ขนาด — ใช้ slot ของมันเอง)

---

## Cross-references

- [`product_name_naming_rule.md`](product_name_naming_rule.md) — Thai display name rule (companion)
- [`feature_smart_product_naming.md`](feature_smart_product_naming.md) — proposed auto-suggest UI feature
- `sendy_erp/inventory_app/sku_code_utils.py` — generator implementation
- ERP DB tables: `categories`, `brands`, `color_finish_codes`, `products` (cols: sku_code, sub_category_short_code, packaging_th, packaging_short)
- Memory: `~/.claude/projects/-Users-putty-Documents-Sendai-Boonsawat/memory/project_2026_05_11_product_review.md`

---

## Change log

- **2026-05-12** — Convention locked. State: 1,963 products, 0 empty/Thai/duplicate sku_codes. Migrations 046-052 applied.
- **2026-05-14** — Added series-slot clarification (English series → in sku_code; Thai series → omit). pid 83 example updated: `HNG-HDOR-SD-#410-BRS-PN` → `HNG-HDOR-SD-JAC-#410-BRS-PN`.
- **2026-07-07** — Naming audit: sku_code regen 77 ตัว (local+prod). Disambiguator = `-{id}` + D7 active-wins-bare. Series สัญลักษณ์ล้วน omit จาก sku. `[ใส]` หลุดจาก model pid 1962 (Thai-in-sku ตัวสุดท้าย).
- **2026-05-28** — 10-slot revision (mig 087). Material slot 8 dropped (never wired into `build_sku_code` despite the rule promising it; column removed too). Packaging split into `packaging_th` (Thai) + `packaging_short` (code) — pkg slot now reads `packaging_short` directly instead of dict lookup at generation time. **pack_variant=1 suppression rule added**: default variant (value 1 or NULL) renders no suffix in both `sku_code` AND `product_name`; only ≥ 2 visible. Also: subcat slot now consumed by `build_sku_code` (was documented in rule but the generator silently skipped it pre-2026-05-28). Example `BLT-MYM-SD-#230-4in-AC-UN` now generates correctly.
