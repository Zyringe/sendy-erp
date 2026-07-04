# Product-label print modes and fixed layouts

A ป้ายสินค้า prints in one of two **label modes** — **บาร์โค้ด** (EAN-13 tag) or **สคบ**
(no-barcode compliance tag) — across two sizes (ป้ายใหญ่ 50×30mm 2-up, ป้ายเล็ก 30×25mm 3-up).
We fix **one layout per mode × size (four total) in code** and apply it uniformly to every product;
the operator picks the mode **per print run**, the data is dropped into the fixed slots, and an
optional field the product lacks simply collapses. We chose this over a stored per-product mode and
over user-editable layout templates because the shop team is non-technical and needs
consistency-by-construction: nothing to design or get wrong at print time.

## Considered options

- **Stored per-product label mode (a `label_mode` column).** Rejected — mode is a print-time choice
  (you might want a barcode tag *or* a สคบ tag for the same product), not an attribute of the product.
- **User-editable layout templates.** Rejected — a template editor is machinery a non-technical team
  would only misconfigure; four hand-tuned, physically-validated layouts are safer and simpler.
- **Per-browser (localStorage) card-registration offsets.** Rejected — the team prints from Windows
  while Put tunes from a Mac; offsets must be one server-side value or the two machines disagree.

## Consequences

- **บาร์โค้ด mode is not universal.** ~108 of 1,105 label rows have no barcode; they are flagged
  "ไม่มีบาร์โค้ด" and excluded from a barcode run (they print via สคบ instead). สคบ mode is universal.
- **Card-registration offsets are a server-side constant** (small roll 0/33/67mm), validated by a
  clean 189-sticker run. A self-service alignment UI is **deferred** (YAGNI) until adjustment proves
  frequent; when it lands it must be DB-backed, not per-browser.
- Rendering path is unchanged: HTML → exact-size PDF (headless Chrome) → local print bridge → GoDEX.
  A **Windows** bridge port is a separate future phase (test the vendor-driver-native path first).
