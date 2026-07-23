# Product Name Naming Rule

> Locked 2026-05-05. ใช้กับ `products.product_name` (ชื่อไทย display) สำหรับสินค้าทุกชนิดในร้าน (own-brand + 3rd-party).
> Companion doc: [`sku_code_naming_rule.md`](sku_code_naming_rule.md) (English-only identifier สำหรับ machine/integration)
> ตั้งค่าครั้งเดียว ใช้ตลอด — ห้ามเปลี่ยนทุก quarter.

---

## Template

```
[ประเภท][ซีรีส์?] [Brand] #[Model]-[ขนาด] [วัสดุ/สี] (แผง/ตัว)
```

ตัวอย่างเต็มๆ:
- `กลอนพฤกษา Sendai #260-4in สีรมดำ (AC) (แผง)`
- `บานพับ Sendai #170-3in สแตนเลส`
- `กรรไกรตัดกิ่ง META #S-101`

---

## กฎ 11 ข้อ

| # | กฎ | ตัวอย่างผิด → ถูก |
|---|---|---|
| 1 | ลำดับคงที่ตาม template | `Sendai บานพับ #170` → `บานพับ Sendai #170` |
| 2 | ประเภทสินค้า — mandatory | `Sendai #170-3นิ้ว` → `บานพับ Sendai #170-3นิ้ว` |
| 3 | ซีรีส์/รุ่นย่อย — **ภาษาไทยติด, อังกฤษ/เลขเว้น** (decided 2026-05-06): ถ้า series เริ่มด้วย Thai → ติดกับ category ไม่มี space; ถ้าเริ่มด้วย ASCII letter หรือเลข → เว้น 1 space | `กลอน พฤกษา` → `กลอนพฤกษา`<br>`กันชนสแตนเลสDOME` → `กันชนสแตนเลส DOME`<br>`สายยู3 ตอน` → `สายยู 3 ตอน`<br>`บานพับใบโพธิ์ทอง` → `บานพับใบโพธิ์ทอง` (ไม่เปลี่ยน) |
| 4 | Brand — mandatory เสมอ ใช้ชื่อตาม `brands.name` | `(P)#260` → `Sendai #260` |
| 5 | Model code — `#` prefix เสมอ ถ้ามี | `170 Sendai` → `Sendai #170` |
| 6 | Model + ขนาด — เชื่อมด้วย `-` ไม่มี space | `#170 3นิ้ว` → `#170-3นิ้ว` |
| 7 | inch → `in` เสมอ (ไม่ใช่ `นิ้ว` หรือ `"`) | `3"`, `3 นิ้ว`, `3นิ้ว` → `3in` |
| 8 | เศษส่วน → decimal — รับทั้ง `1 1/2` และ `1.1/2` | `1 1/2นิ้ว`, `1.1/2นิ้ว` → `1.5in` |
| 9 | สี — ภาษาคน + โค้ดในวงเล็บ | `AC` → `สีรมดำ (AC)` |
| 10 | Packaging — `(แผง)` หรือ `(ตัว)` ที่ท้ายชื่อ ถ้ามีทั้ง 2 แบบ | `(P)` → `(แผง)` |
| 11 | Format — single space, no double space, max 60 chars (**soft limit** ตั้งแต่ 2026-07-07: เกินได้เมื่อทุกคำเป็น spec จริง ตัดแล้วข้อมูลหาย — Put ปิด 15 ชื่อยาวโดยไม่ตัด) | `บานพับ  Sendai` → `บานพับ Sendai` |
| 12 | Condition note — `(เก่า)`/`(ไม่สวย)`/`(ตำหนิ)` ที่ **ท้ายสุด** หลัง `(แผง)/(ตัว)` | `Sendai(P) AC (เก่า)` → `Sendai #230 (แผง) (เก่า)` |
| 13 | Pack-size variant — ตาม mig087 (2026-05-28, ทับกฎเดิม): `pack_variant=1` **ซ่อน** suffix ในชื่อ+sku_code; `pack_variant>=2` **โชว์** (` 2`, ` 3`) ให้ชื่อคู่ขนานกับ sku `-2` และสองตัว active ไม่ชื่อซ้ำกัน. `units_per_carton` เก็บขนาดลังตามจริงเสมอ | `Sendai #230-4in AC 2` → `Sendai #230-4in สีรมดำ (AC) (แผง)` + set `units_per_carton` ตามขนาดลัง |
| 14 | Packaging values = `แผง` / `ตัว` / `ถุง` / `แพ็คหัว` / `แพ็คถุง` (auto-strip `รุ่น` prefix) | `รุ่นแผง` / `(P)` / `(แพ็คหัว)` → `(แผง)` / `(แผง)` / `(แพ็คหัว)` |
| 15 | mm/cm — ไม่มี space ระหว่างเลขกับหน่วย; lowercase เสมอ (`cm`/`mm`); `มิล` → `mm` | `120 mm.` / `5 CM.` / `50 มิล` → `120mm` / `5cm` / `50mm` |
| 16 | Annotations เช่น `(มีบาโค๊ต)` `(ไม่มีบาโค้ต)` — strip ออกจากชื่อ ไม่ใช่ส่วนของ rule | `... SS (มีบาโค๊ต)` → `... สีเงิน-สแตนเลส (SS)` |
| 17 | Bare model code (letters+digits ไม่มี `#`) — auto-prefix `#`; รักษา `-N` suffix | `SD9951` / `HL316` / `HL9991-2` → `#SD9951` / `#HL316` / `#HL9991-2` |
| 18 | Spaces ภายใน `# CODE` — ลบ | `# HL316` / `#HL 9991-2` → `#HL316` / `#HL9991-2` |
| 19 | Bare Thai/English colors — recognize เป็น `color_th`. Codes อยู่ใน color dictionary; bare ที่ไม่มี code อยู่เก็บเฉพาะ Thai. Match case-insensitive (`Black` = `BLACK` = `black`) แต่ตรวจ word boundary เคร่ง (Thai vowel marks ไม่ break) | `WHITE` / `Black` / `บรอนซ์` / `งา` → `สีขาว` / `สีดำ` / `สีบรอนซ์` / `สีงา` |
| 20 | Series ภาษาอังกฤษที่รู้จัก (DOME, TOP, HEAVY, MAX, PRO, MINI, PLUS, BALL, NEW TOP, DEAD LOCK) — แยกเป็น series field มี space ขั้นกับ category | `กันชนสแตนเลส DOME` → category=`กันชนสแตนเลส`, series=`DOME` |
| 21 | Series Thai ที่ติดประเภท (`ใบโพธิ์ทอง`, `กลอนพฤกษา`) — เก็บคู่กับประเภท ไม่แยก color แม้จะมี `ทอง` หรือ `แดง` ในชื่อ | `บานพับ ใบโพธิ์ทอง` → category=`บานพับ`, series=`ใบโพธิ์ทอง` (ไม่ใช่ color=ทอง) |
| 22 | Multi-color combo codes (เช่น `SS-BK`, `BN/AC`, `SB/WB`) — เก็บเป็น code เดียวใน `color_finish_codes` (ไม่ split) | `SS-BK` / `BN/AC` → `สีเงิน-สแตนเลส-ดำ (SS-BK)` / `สีน้ำตาลเข้ม-รมดำ (BN/AC)` |
| 23 | Bare-size รีเวท (รูปแบบ `\d-\d` ไม่มี unit) — recognize เป็น size ใช้กับลูกรีเวท/ตะปูยิงรีเวท | `4-2` / `4-4` / `4-5` → size=`4-2` / `4-4` / `4-5` |

---

## Brand short_code (ไว้ใช้ใน family_code, ไม่ใช่ใน product_name)

ใน product_name → ใช้ชื่อเต็ม `Sendai`, `META`, `Golden Lion`
ใน family_code → ใช้ short → `SD-170`, `META-S101`, `GL-520`

| brand | short_code | ใช้ใน product_name |
|---|---|---|
| Sendai | SD | `Sendai` |
| Golden Lion | GL | `Golden Lion` |
| A-SPEC | AS | `A-SPEC` |
| META | META | `META` |
| Eagle One | EAGLE | `Eagle One` |
| TOA | TOA | `TOA` |
| SOMIC | SOMIC | `SOMIC` |
| King Eagle | KING | `King Eagle` |
| Fastenic | FAST | `Fastenic` |
| BRAVO | BRAVO | `BRAVO` |
| Yokomo | YOKO | `Yokomo` |
| SANWA | SANWA | `SANWA` |
| SOLEX | SOLEX | `SOLEX` |
| BAHCO | BAHCO | `BAHCO` |
| FION | FION | `FION` |
| ORBIT | ORBIT | `ORBIT` |
| STAR | STAR | `STAR` |
| ตราจิงโจ้ | KANGA | `ตราจิงโจ้` |
| HORSE SHOE | HORSE | `HORSE SHOE` |
| NITTO | NITTO | `NITTO` |
| BAC | BAC | `BAC` |
| MACOH | MACOH | `MACOH` |
| INTER TAPE | INTER | `INTER TAPE` (alias: `INTER`) |
| Sonic | SONIC | `Sonic` |
| Heller | HELLER | `Heller` |
| เหรียญทอง | COIN | `เหรียญทอง` |
| ตราม้า | MAA | `ตราม้า` |
| นก | BIRD | `นก` |
| นกนางแอ่น | SWAL | `นกนางแอ่น` |
| STAR (ตราดาว) | STAR | `STAR` หรือ `ตราดาว` (Thai alias) |
| KP | KP | `KP` |
| KPS | KPS | `KPS` |
| Kobe | KOBE | `Kobe` |
| Red Fox | FOX | `Red Fox` |
| Maxweld | MAXWELD | `Maxweld` |
| ASAHI | ASAHI | `ASAHI` |
| Keenness | KEEN | `Keenness` |
| Other (3rd-party) | 3RD | ตามชื่อจริงของแบรนด์ |
| No Name | NN | (ไม่ระบุ brand ในชื่อ) |

---

## Color/Finish Dictionary (locked)

| โค้ด | Thai | ที่ใช้ใน product_name |
|---|---|---|
| AC | สีรมดำ | `สีรมดำ (AC)` |
| MAC | สีเมทัลลิกดำ | `สีเมทัลลิกดำ (MAC)` |
| AB | สีทองแดงรมดำ | `สีทองแดงรมดำ (AB)` |
| PAB | สีทองดำเงา | `สีทองดำเงา (PAB)` |
| PB | สีทองเงา | `สีทองเงา (PB)` |
| SB | สีทองด้าน | `สีทองด้าน (SB)` |
| SB/WB | สีทองด้าน-ขาว | `สีทองด้าน-ขาว (SB/WB)` |
| CR | สีโครเมียม | `สีโครเมียม (CR)` |
| SS | สีเงิน-สแตนเลส | `สีเงิน-สแตนเลส (SS)` |
| SS-BK | สีเงิน-สแตนเลส-ดำ | `สีเงิน-สแตนเลส-ดำ (SS-BK)` |
| SN | สีเงินด้าน | `สีเงินด้าน (SN)` |
| NK | สีนิกเกิล | `สีนิกเกิล (NK)` |
| BN | สีน้ำตาลเข้ม | `สีน้ำตาลเข้ม (BN)` |
| BN/AC | สีน้ำตาลเข้ม-รมดำ | `สีน้ำตาลเข้ม-รมดำ (BN/AC)` |
| BZ | สีบรอนซ์ | `สีบรอนซ์ (BZ)` |
| PAC | สีสเปรย์ | `สีสเปรย์ (PAC)` |

**Thai aliases** (parser auto-converts to code):

| Thai bare | Code |
|---|---|
| `บรอนซ์` / `บรอน` | `BZ` |

**หมายเหตุ dictionary (2026-07-07, Put ตัดสิน):**
- `JSN` = `สีนิกเกิ้ล` / `NK` = `สีนิกเกิล` — **ตั้งใจสะกดต่างกัน** (ผิวต่างจริง) ห้าม typo-detector จับคู่นี้เป็น near-duplicate
- `BN` อ่านต่างความหมายตาม combo: เดี่ยว + ใน `BN/AC` = น้ำตาลเข้ม, ใน `BN/PB` = Brushed Nickel (สีนิกเกิล-ทองเงา) — คงตามนี้
- แบรนด์ `จระเข้` = Crocodile (id 44) แบรนด์แยก **ไม่ใช่** TOA (alias เก่าถูกลบ 2026-07-07 ทั้งจาก doc และ scripts)

### Bare colors (ไม่มี code)

ใช้ Thai canonical name โดยตรงในชื่อสินค้า ถ้าไม่มี code:

| Bare token | Thai canonical |
|---|---|
| ดำ / Black | สีดำ |
| ขาว / White | สีขาว |
| แดง / Red | สีแดง |
| น้ำเงิน / Blue | สีน้ำเงิน |
| เขียว / Green | สีเขียว |
| เหลือง / Yellow | สีเหลือง |
| น้ำตาล / Brown | สีน้ำตาล |
| ทอง | สีทอง |
| เงิน | สีเงิน |
| ฟ้า | สีฟ้า |
| ชา | สีชา |
| งา | สีงา |
| เทา | สีเทา |
| ธรรมชาติ / Nature | สีธรรมชาติ |

→ ถ้าเจอโค้ดสีใหม่ที่ไม่อยู่ในตาราง: **เพิ่มเข้า `color_finish_codes` table** (อย่าแค่ใส่ในชื่อ) จะได้ track ได้

### Patterns / Textures (ไม่ใช่สี — เก็บเป็น Thai bare)

ลายผิวสินค้า — ใช้ Thai bare token เก็บไว้ในชื่อ ไม่ต้องเป็น color code:

| Pattern | ตัวอย่าง | เหตุผล |
|---|---|---|
| `ลายฆ้อน` | `สายยู 3 ตอน NRK ลายฆ้อน` | hammer texture — เป็น finish ไม่ใช่สี |
| `ลายคราม` | `สายยู 3 ตอน NRK ลายคราม` | indigo pattern — เป็น finish |

→ Pattern + color รวมกันได้ในชิ้นเดียว เช่น `... BZ ลายฆ้อน` ในอนาคต — ถ้ายัด pattern เป็น code จะ conflict กับ color code

---

## Examples — Before / After

| ก่อน (ชื่อเดิมใน DB) | หลัง (ตามกฎ) |
|---|---|
| `กลอนพฤกษา (P)#260-4นิ้ว AC` | `กลอนพฤกษา Sendai #260-4in สีรมดำ (AC) (แผง)` |
| `กลอนมะยม Sendai(ตัว)#230-4นิ้ว AC 1` | `กลอนมะยม Sendai #230-4in สีรมดำ (AC) (ตัว)` |
| `กลอนสแตนเลส Sendai(ตัว)#360-6นิ้ว SS` | `กลอนสแตนเลส Sendai #360-6in สีเงิน-สแตนเลส (SS) (ตัว)` |
| `บานพับสแตนเลส #170 Sendai 3 นิ้ว` | `บานพับ Sendai #170-3in สแตนเลส` |
| `ไขควงสลับ META หัวโต 1 1/2นิ้ว` | `ไขควงสลับ META หัวโต 1.5in` |
| `ฉากวัดไม้ #10นิ้ว META` | `ฉากวัดไม้ META #10in` |
| `กรอบจตุคาม (P) 5 CM. สีทอง` | `กรอบจตุคาม Sendai 5cm สีทองเงา (PB) (แผง)` |
| `ลูกบิด Sendai (P)#5112 SB` | `ลูกบิด Sendai #5112 สีทองด้าน (SB) (แผง)` |
| `ดจ.สแตนแลส META 11/32` | `ดจ. META #11/32 สแตนเลส` |
| `กรรไกรตัดกิ่ง META #S-101` | `กรรไกรตัดกิ่ง META #S-101` ✓ already clean |

---

## Edge cases

- **สินค้าไม่มี model** → ข้าม `#[Model]-` ไป เช่น `ไขควงสลับ META 3นิ้ว`
- **สินค้าไม่มี brand** → ใส่ภายใต้ "No Name" แต่ไม่ต้องเขียน "No Name" ในชื่อ
- **บรรจุลังต่างกัน (สินค้าเดียวกัน)** → 2 SKUs แยก, ชื่อเหมือนกันเป๊ะ, แตกต่างที่ `units_per_carton` ใน DB. ห้ามต่อเลข ` 1`/` 2` ที่ท้ายชื่อ (rule 13). Catalog renderer แสดง "บรรจุ N ตัว/ลัง" จาก `units_per_carton` ให้ลูกค้าเห็นความต่าง.
- **สินค้าตำหนิ/เก่า** → ใส่ `(เก่า)` / `(ไม่สวย)` / `(ตำหนิ)` ท้ายสุดหลัง `(แผง)/(ตัว)` (rule 12)
- **สินค้าหลายขนาดในชื่อเดียว** (เช่น `M6x20mm`, `3x4นิ้ว`) → เก็บเดิมเป็นรูปแบบ `[w]x[h][unit]`
- **สินค้ามี packaging แบบเดียว** (มีแค่ตัว) → ไม่ต้องใส่ `(ตัว)` — ใส่เฉพาะเมื่อมีทั้ง 2 แบบในกลุ่ม
- **Color codes ที่ไม่อยู่ใน dictionary** → INSERT ใหม่เข้า `color_finish_codes` ก่อน
- **ทับศัพท์ไทย + อังกฤษคู่กัน (precedent ลูกบิดคริสตรัล, Put 2026-07-22)**: เมื่ออยากให้ค้นหาได้ทั้งสองภาษา ใส่คำอังกฤษถัดจากคำไทยทับศัพท์: `ลูกบิดคริสตรัล Crystal บุศราคัม Sendai สีน้ำตาล (แผง)`

---

## Brand alias normalization

หลายชื่อเดิมในระบบใช้ alias หรือชื่อย่อแทน brand canonical name ตอน rename ให้ใช้ชื่อตามคอลัมน์ `brands.name`:

| ใน DB เดิม | normalize เป็น |
|---|---|
| `S/D`, `SD-` (prefix) | `Sendai` |
| `เซ็นได` | `Sendai` |
| `สิงห์`, `สิงห์ทอง` | `Golden Lion` |

→ Audit script ยอมรับทั้ง `name`, `name_th`, `short_code`, และ alias ข้างบนว่าเป็น "มี brand แล้ว" — แต่ตอน rename ให้ใช้ canonical name ทุกครั้ง

## ซีรีส์ `กล่องสี` (สิงห์ทอง)

แบรนด์ Golden Lion (สิงห์ทอง) มีซีรีส์ที่แยกด้วยสีกล่อง — เก็บเป็น series ติดกับประเภทสินค้า:

| Series | Count | ใช้ใน product_name |
|---|---:|---|
| กล่องเหลือง | 29 | `[ประเภท] Golden Lion กล่องเหลือง #...` |
| กล่องแดง | 18 | `[ประเภท] Golden Lion กล่องแดง #...` |
| กล่องน้ำเงิน | 11 | `[ประเภท] Golden Lion กล่องน้ำเงิน #...` |
| กล่องเขียว | 7 | `[ประเภท] Golden Lion กล่องเขียว #...` |

ตัวอย่าง:
- `ดจ.สแตนเลส Golden Lion กล่องน้ำเงิน 3/16in`
- `ดจ.สแตนเลส Golden Lion กล่องเหลือง 5/32in`

> ใส่ "กล่องสี" หลัง brand ก่อน model — เป็น series classifier ของ Golden Lion

## ซีรีส์ JBB (บานพับมียอด) — ไม่ใช่สี

**JBB = บานพับที่มียอด (finial) — เป็น feature ไม่ใช่สี** (Put ยืนยัน 2026-07-23; ก่อนหน้านั้น dict เคยผิดเป็น "สีทองแดงรมดำ").
- รูปแบบ: `บานพับสแตนเลสมียอด JBB Sendai #2543-4inx3inx2.5mm (แผง)` — JBB อยู่ตำแหน่ง series ตาม rule 20
- structured: `series='JBB'`, `color_code=NULL` (กลุ่มนี้สแตนเลสเปลือย ไม่มีสี) — ห้ามใส่คำสี
- โค้ด JBB ถูกถอนออกจาก `color_finish_codes` แล้ว (2026-07-23) — ห้ามเพิ่มกลับเป็นสี

## Token `JAC` (บานพับ #410/#412) — สื่อทั้งสีและรุ่น

Put ยืนยัน 2026-07-22: **JAC บอกทั้งสีและรุ่นในตัว** — เจอ JAC ในชื่อแล้ว อย่า flag ว่า "ขาดสี" หรือ "ขาดรุ่น" เพิ่ม

## Typos ที่แก้แล้ว

| typo | correct | count | migration |
|---|---|---:|---|
| `สแตนแลส` | `สแตนเลส` | 43 | 027 |
| `โครเมี่ยม` | `โครเมียม` | 5 | 027 |
| `แสตนเลส` | `สแตนเลส` | 14 | 028 |
| `แสตนแลส` | `สแตนเลส` | 3 | 028 |
| `น๊อต` | `น็อต` | 10 | 028 |
| `เหรีญทอง` | `เหรียญทอง` | (small) | 030 |
| `ปุ๊ก` | `พุก` | 2 | apply script 2026-07-07 |
| `บอร์น` | `บรอนซ์` | 1 | apply script 2026-07-07 (pid 716, Put confirm) |

ถ้าเจอ typo ใหม่ในอนาคต ให้แก้ผ่าน migration ใหม่ (UPDATE...REPLACE) เพื่อให้ audit_log track ได้

## Change log (เพิ่มเติม)

- **2026-07-23** — **Naming Round 2** (projects/product-naming-round2): family-consistency + twins + sku/photo sync. 133+4 ops applied local+prod (ชื่อ 80 + fields 57 + sku regen 61 รวม drift), photo folders 6 + photo_meta re-key + reshoot batch.json sync. Policy ใหม่: JBB=มียอด (ถอนจาก color dict), JAC=สี+รุ่น, (ไม่สวย) ถอนจากชื่อ (tag=condition+`-BLM`), Crystal ทับศัพท์คู่, ฝักบัว/สายชำระรุ่นใหม่มีสี-รุ่นเก่าไม่มี. เครื่องมือ: `audit_family_consistency.py`, `compile_round2_ops.py` (generic decisions CSV), `diff_sku_map.py`, `_review/rename_sku_folders.py`.
- **2026-07-07** — Product-naming audit ครั้งใหญ่ (projects/product-naming-audit): แก้ชื่อ 115 + fields 53 + SKU 77 ทั้ง local+prod. Rule 11 → soft limit; rule 13 → align mig087; typo dict + ปุ๊ก/บอร์น; JSN=สีนิกเกิ้ล; ลบ alias จระเข้→TOA; ตัวอย่างในเอกสารเปลี่ยน นิ้ว→in ตาม rule 7.

## Conditions (last bracket)

| condition | meaning | example |
|---|---|---|
| `(เก่า)` | สินค้าค้างสต็อกนาน, ไม่ใหม่ | `... (แผง) (เก่า)` |
| ~~`(ไม่สวย)`~~ | **ถอนออกจากชื่อแล้ว (Put 2026-07-22, applied 07-23)** — tag อยู่ที่ `products.condition='ไม่สวย'` + sku ลงท้าย `-BLM`; ข้อยกเว้น pid 824 คงไว้จนสต็อกหมด (กันชื่อชนฝาแฝด 1664). Bracket อื่น (เก่า/ตำหนิ/หมดอายุ/ไม่สกรีน/ไม่มีน็อต) **ยังอยู่ในชื่อ** (safety-relevant) | — |
| `(ตำหนิ)` | สินค้ามีรอยเสียหาย | `... (แผง) (ตำหนิ)` |
| `(หมดอายุ)` | กาว/เคมีภัณฑ์ที่หมดอายุแล้ว | `... 3in (หมดอายุ)` |
| `(ไม่สกรีน)` | ไม่ได้พิมพ์ลาย/โลโก้ | `... (ไม่สกรีน)` |
| `(ไม่มีน็อต)` | ขาดอะไหล่ | `... (ตัว) (ไม่มีน็อต)` |

→ Parser ยอมรับ open bracket `(` แม้ไม่มี close `)` (กรณี input บกพร่อง)

---

## Rule 24 — Bundles & sets

สินค้าที่ขาย 1 SKU แต่ภายใน 1 กล่อง/แพ็คมีของหลายชิ้น มี 3 sub-pattern:

### 24A. Component bundle (ของคนละชนิดรวมกัน)

ใช้ `+` เป็นตัวเชื่อม **ไม่มี space ทั้งสองฝั่ง** (เพื่อไม่ปนกับสัญลักษณ์หัวแฉก `(+)` หรือ ` +` ลอยปลาย)

```
[ItemA]+[ItemB] [Brand] [sizeA/sizeB] [details]
```

- size ของแต่ละ component แยกด้วย `/` ตามลำดับ (`sizeA/sizeB`)
- หน่วย/spec อื่นๆ ตามหลังลำดับ component
- category = ของหลัก (ItemA) เท่านั้น เพื่อให้จัด catalog ได้ถูกหมวด

ตัวอย่าง:
- `ลูกกลิ้งทาสี+ด้าม Sendai 4in/17in 13mm` (roller 4in 13mm + handle 17in)
- `ลูกกลิ้งขนแกะ+ด้าม Eagle One 4in/26in`
- `ฆ้อนยางเล็ก+ด้ามไม้`
- `เลื่อยตัดกิ่ง 2 คม+ปลอก #SD7203`
- `โครงเลื่อยคันธนู+ใบ #GL4010 30in`
- `ลูกบิด+DeadLock #8101 AC Sendai`

### 24B. Multi-pack (ของชนิดเดียวกัน N ชิ้น)

ใช้ `Nตัวชุด` หลังชื่อ item

```
[Item] Nตัวชุด [Brand]
```

ตัวอย่าง:
- `โฮลซอ 2 ตัวชุด META`
- `ไขควงลองไฟ 5 ตัวชุด CHAMPION`
- `ประแจแหวนข้างปากตาย 9 ตัวชุด`

### 24C. Full kit (ชุดประกอบครบ)

ขึ้นต้นด้วย `ชุดเซ็ต[type]`

```
ชุดเซ็ต[type] [Brand] #[Model] [Color]
```

ตัวอย่าง:
- `ชุดเซ็ตประตู Sendai #DL-04 AC`
- `ชุดเซ็ตหน้าต่าง 1 Sendai AC`

### ที่ NOT bundle (เก็บไว้กันสับสน)

`(+)` หรือ `(-)` ในชื่อ = สัญลักษณ์หัวแฉก/หัวแบน ของไขควง/ดอกไขควง — **ไม่ใช่ bundle**

ตัวอย่าง:
- `ดอกไขควงลม 6x65 mm (+)(+) META` ← หัวแฉกทั้งสองข้าง
- `ไขควงด้ามใสสีดำ-น้ำเงิน หัวเดียว 6in (+)` ← หัวแฉก

→ Parser ต้อง skip rows ที่ match pattern `(+)` / `(-)` / ` +$` เมื่อ flag bundle

### Future schema (ยังไม่ทำ)

ปัจจุบัน bundle เก็บ flat ใน `products.product_name` 1 row. ถ้าจะ track stock/cost ตาม component จริง:
- เพิ่ม table `product_bundles (parent_product_id, component_product_id, qty)`
- ขาย bundle 1 ตัว → trigger auto-decrement components
- cost = SUM(component cost × qty)
- ผูก logic เดียวกับ `listing_bundles` (ของ ecommerce ที่มีอยู่แล้ว)

→ Deferred จนกว่าทำ catalog เสร็จ.

---

## Application code rules

- **Family display_name** = ชื่อย่อกลุ่ม → ตัด ขนาด/สี/packaging ออก
  - SKU: `กลอนพฤกษา Sendai #260-4in สีรมดำ (AC) (แผง)`
  - Family display_name: `กลอนพฤกษา Sendai #260`
- **family_code** = `{BRAND_SHORT}-{MODEL}` (immutable, ASCII uppercase) → `SD-260`
- **products.color_code** = FK to `color_finish_codes.code` — เก็บโค้ดเป็น structured field; ใน product_name ก็ยังเก็บไว้ด้วยเพื่อ readability
- **products.packaging** = `'แผง'` / `'ตัว'` / NULL — structured; ใน product_name ก็ยังมี `(แผง)`/`(ตัว)` ด้วย

---

## Validation

ใช้ script: `sendy_erp/scripts/audit_sku_naming.py`
Output: `sendy_erp/data/exports/sku_naming_audit.csv` — แสดง SKU ที่ off-rule + ประเภทปัญหา

ดูที่ `sendy_erp/docs/product_name_naming_rule.md` (this file) เป็น authoritative source.
