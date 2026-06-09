#!/usr/bin/env python3
"""Product photo automation for catalog assets.

This workflow reads ERP product metadata, scans image folders, proposes SKU
matches, builds a browser review UI, applies reviewed decisions by copying
files into catalog buckets, and exports normalized image sizes.

It never writes to the ERP database and never moves source files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - environment dependent
    Image = None
    ImageOps = None


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORT_DIR = ROOT / "data" / "exports"
CATALOG_ROOT = WORKSPACE / "Design" / "Catalog" / "photos"
PRODUCT_ROOT = CATALOG_ROOT / "products"
REVIEW_HTML = CATALOG_ROOT / "review.html"

SCAN_CSV = EXPORT_DIR / "product_photo_scan.csv"
MATCH_CSV = EXPORT_DIR / "product_photo_matches.csv"
UNMATCHED_CSV = EXPORT_DIR / "product_photo_unmatched.csv"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

FOLDER_HINTS = {
    "กลอน": "door_bolt",
    "บานพับ": "hinge",
    "มือจับ": "handle",
    "ลูกบิด": "door_knob",
    "KNOB": "door_knob",
    "กุญแจ": "lock_key",
    "ค้อน": "hammer",
    "ฆ้อน": "hammer",
    "ไขควง": "screwdriver",
    "กรรไกร": "cutter",
    "คีม": "plier",
    "เลื่อย": "saw",
    "ใบเลื่อย": "saw",
    "ตะปู": "fastener",
    "พุก": "anchor",
    "ปุ๊ก": "anchor",
    "กาว": "glue",
    "สี": "paint_brush",
    "แปรง": "paint_brush",
    "เครื่องเหล็ก": "drill_bit",
    "แผ่นตัด": "disc",
    "โป้้ว": "trowel",
    "โป้ว": "trowel",
    "ขอสับ": "fitting",
    "ขอแขวน": "hook",
    "สายยู": "fitting",
    "กันชน": "fitting",
    "ชุดเซ็ต": "door_bolt",
    "แผง": "door_bolt",
    "ห้องน้ำ": "faucet",
    "แบบประตู": "door_bolt",
    "anchor": "anchor",
    "box": "box",
    "cutter": "cutter",
    "disc": "disc",
    "door_bolt": "door_bolt",
    "door_knob": "door_knob",
    "drill_bit": "drill_bit",
    "fastener": "fastener",
    "fitting": "fitting",
    "glue": "glue",
    "handle": "handle",
    "hinge": "hinge",
    "lock_key": "lock_key",
    "paint_brush": "paint_brush",
    "plier": "plier",
    "sandpaper": "sandpaper",
    "saw": "saw",
    "screwdriver": "screwdriver",
    "tape_gypsum": "tape_gypsum",
    "trowel": "trowel",
}

BUCKETS = {"single", "pack", "family", "_pending", "_pending_typecheck", "_unmatched"}


@dataclass(frozen=True)
class Product:
    id: int
    sku_code: str
    product_name: str
    model: str
    size: str
    color_code: str
    cat_code: str
    cat_short: str
    brand_short: str
    family_code: str
    family_name: str


def workspace_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE))
    except ValueError:
        return str(path.resolve())


def safe_part(value: str) -> str:
    value = (value or "").strip().replace("/", "_")
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^A-Za-z0-9ก-๙._#,-]+", "-", value)
    return value.strip("-") or "unknown"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_images(
    source: Path,
    limit: int | None = None,
    skip_buckets: set[str] | None = None,
) -> Iterable[Path]:
    count = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
            continue
        if skip_buckets and any(part in skip_buckets for part in path.parts):
            continue
        yield path
        count += 1
        if limit and count >= limit:
            return


def image_meta(path: Path) -> tuple[int, int, str]:
    if Image is None:
        return 0, 0, "pillow_missing"
    try:
        with Image.open(path) as im:
            return im.width, im.height, ""
    except Exception as exc:
        return 0, 0, f"unreadable:{exc.__class__.__name__}"


def quality_flags(path: Path, width: int, height: int, checksum: str, seen: dict[str, str]) -> list[str]:
    flags: list[str] = []
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        flags.append("unsupported_export_format")
    if width and height:
        if min(width, height) < 800:
            flags.append("small_lt_800")
        ratio = max(width, height) / max(1, min(width, height))
        if ratio > 2.2:
            flags.append("odd_aspect_ratio")
    elif Image is not None:
        flags.append("unreadable_image")
    if checksum in seen:
        flags.append(f"duplicate_of:{seen[checksum]}")
    return flags


def scan(args: argparse.Namespace) -> None:
    source = args.source.resolve()
    if not source.exists():
        raise SystemExit(f"source not found: {source}")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    seen: dict[str, str] = {}
    for path in iter_images(source, args.limit):
        checksum = sha256_file(path)
        width, height, read_error = image_meta(path)
        rel = workspace_path(path)
        flags = quality_flags(path, width, height, checksum, seen)
        if read_error:
            flags.append(read_error)
        rows.append({
            "source_abs": str(path.resolve()),
            "source_path": rel,
            "filename": path.name,
            "ext": path.suffix.lower(),
            "bytes": path.stat().st_size,
            "width": width,
            "height": height,
            "sha256": checksum,
            "flags": "|".join(flags),
        })
        seen.setdefault(checksum, rel)

    write_csv(SCAN_CSV, rows, [
        "source_abs", "source_path", "filename", "ext", "bytes",
        "width", "height", "sha256", "flags",
    ])
    print(f"Scanned: {len(rows)}")
    print(f"Wrote:   {SCAN_CSV.relative_to(WORKSPACE)}")


def load_products() -> tuple[list[Product], set[str]]:
    if not DB_PATH.exists():
        raise SystemExit(f"database not found: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT p.id, p.sku_code, p.product_name, p.model, p.size,
               p.color_code,
               c.code AS cat_code, c.short_code AS cat_short,
               b.short_code AS brand_short,
               pf.family_code, pf.display_name AS family_name
          FROM products p
          LEFT JOIN categories c ON c.id = p.category_id
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN product_families pf ON pf.id = p.family_id
         WHERE p.is_active = 1
    """).fetchall()
    products = [
        Product(
            id=r["id"],
            sku_code=str(r["sku_code"] or ""),
            product_name=str(r["product_name"] or ""),
            model=str(r["model"] or ""),
            size=str(r["size"] or ""),
            color_code=str(r["color_code"] or ""),
            cat_code=str(r["cat_code"] or "other"),
            cat_short=str(r["cat_short"] or ""),
            brand_short=str(r["brand_short"] or ""),
            family_code=str(r["family_code"] or ""),
            family_name=str(r["family_name"] or ""),
        )
        for r in rows
    ]
    colors = {str(r[0] or "").upper() for r in conn.execute("SELECT code FROM color_finish_codes")}
    return products, colors


def tokenize(path: Path, source: Path) -> dict[str, object]:
    base = path.stem
    searchable = base.replace("_", "-")
    model_tokens = re.findall(r"#?([A-Za-z]*\d[A-Za-z0-9-]{1,20}|\d{2,5})(?!\.\d)", searchable)
    size_tokens = [
        re.sub(r"\s+", "", m.group(0).lower()).replace("นิ้ว", "in")
        for m in re.finditer(r"\d+(?:\.\d+|/\d+)?\s*(?:in|นิ้ว|mm|cm)\b", base, re.IGNORECASE)
    ]
    size_tokens.extend(m.group(0) for m in re.finditer(r"\b\d+/\d+\b|\b\d+-\d+\b", base))

    folder_hint = ""
    try:
        rel_parts = path.relative_to(source).parts[:-1]
    except ValueError:
        rel_parts = path.parts[:-1]
    for part in rel_parts:
        if part in BUCKETS:
            continue
        if part in FOLDER_HINTS:
            folder_hint = FOLDER_HINTS[part]
            break

    return {
        "raw_base": base,
        "searchable": searchable.upper(),
        "model_tokens": [t.strip("#").upper() for t in model_tokens if t],
        "size_tokens": list(dict.fromkeys(size_tokens)),
        "folder_hint": folder_hint,
    }


def score_product(product: Product, tokens: dict[str, object], colors: set[str]) -> int:
    score = 0
    searchable = str(tokens["searchable"])
    model_tokens = set(tokens["model_tokens"])
    size_tokens = set(tokens["size_tokens"])
    folder_hint = str(tokens["folder_hint"])

    sku_code = product.sku_code.upper()
    family_code = product.family_code.upper()
    model = product.model.strip("#").upper()
    size = product.size.lower().replace(" ", "")
    color = product.color_code.upper()
    brand = product.brand_short.upper()

    if sku_code and sku_code in searchable:
        score += 120
    if family_code and family_code in searchable:
        score += 65
    if folder_hint and product.cat_code == folder_hint:
        score += 30
    if model:
        for tok in model_tokens:
            if tok == model or model in tok or tok in model:
                score += 50
                break
    if size:
        for tok in size_tokens:
            if tok.lower().replace(" ", "") == size:
                score += 25
                break
    if color and color in colors and re.search(rf"(^|[-_\\s]){re.escape(color)}($|[-_\\s])", searchable):
        score += 25
    if brand and re.search(rf"(^|[-_\\s]){re.escape(brand)}($|[-_\\s])", searchable):
        score += 15
    return score


def candidate_products(products: list[Product], tokens: dict[str, object]) -> list[Product]:
    folder_hint = str(tokens["folder_hint"])
    searchable = str(tokens["searchable"])
    model_tokens = set(tokens["model_tokens"])

    candidates = []
    for product in products:
        if folder_hint and product.cat_code != folder_hint:
            continue
        sku_code = product.sku_code.upper()
        family_code = product.family_code.upper()
        model = product.model.strip("#").upper()
        if sku_code and sku_code in searchable:
            candidates.append(product)
        elif family_code and family_code in searchable:
            candidates.append(product)
        elif model and any(tok == model or model in tok or tok in model for tok in model_tokens):
            candidates.append(product)
        elif folder_hint and product.cat_code == folder_hint and model_tokens:
            candidates.append(product)
    return candidates


def match(args: argparse.Namespace) -> None:
    source = args.source.resolve()
    products, colors = load_products()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    matched = []
    unmatched = []
    seen_hashes: dict[str, str] = {}
    # When the source is the catalog tree itself, only review pending files.
    # Already approved/rejected buckets are outputs, not fresh input.
    skip_buckets = {"single", "pack", "family", "_unmatched", "exports"}
    for path in iter_images(source, args.limit, skip_buckets=skip_buckets):
        checksum = sha256_file(path)
        width, height, read_error = image_meta(path)
        flags = quality_flags(path, width, height, checksum, seen_hashes)
        if read_error:
            flags.append(read_error)
        seen_hashes.setdefault(checksum, workspace_path(path))

        tokens = tokenize(path, source)
        candidates = candidate_products(products, tokens)
        scored = [(score_product(p, tokens, colors), p) for p in candidates]
        scored = [(score, p) for score, p in scored if score > 0]
        scored.sort(key=lambda item: item[0], reverse=True)

        if not scored or scored[0][0] < 50:
            best_score = scored[0][0] if scored else 0
            best = scored[0][1] if scored else None
            unmatched.append(unmatched_row(path, tokens, checksum, flags, best_score, best))
            continue

        best_score = scored[0][0]
        top = [item for item in scored if item[0] == best_score]
        best = top[0][1]
        status = "matched"
        if len({p.id for _, p in top}) > 1:
            same_family = len({p.family_code for _, p in top if p.family_code}) == 1
            status = "family_candidate" if same_family else "ambiguous"
            flags.append(status)
        if read_error or any(f.startswith("duplicate_of:") for f in flags):
            status = "typecheck"

        target_family = best.family_code or best.sku_code or f"INT-{best.id}"
        review_file = stable_review_key(path)
        matched.append({
            "review_file": review_file,
            "source_abs": str(path.resolve()),
            "source_path": workspace_path(path),
            "filename": path.name,
            "sha256": checksum,
            "width": width,
            "height": height,
            "flags": "|".join(flags),
            "status": status,
            "best_score": best_score,
            "category_code": best.cat_code,
            "target_sku": best.sku_code or f"INT-{best.id}",
            "target_family": target_family,
            "product_name": best.product_name,
            "brand": best.brand_short,
            "size": best.size,
            "color": best.color_code,
            "folder_hint": tokens["folder_hint"],
            "alternatives": ",".join((p.sku_code or f"INT-{p.id}") for _, p in top[:5]),
            "suggested_decision": "family" if status == "family_candidate" else "single",
        })

    write_csv(MATCH_CSV, matched, match_fields())
    write_csv(UNMATCHED_CSV, unmatched, unmatched_fields())
    print(f"Matched:   {len(matched)}")
    print(f"Unmatched: {len(unmatched)}")
    print(f"Wrote:     {MATCH_CSV.relative_to(WORKSPACE)}")
    print(f"Wrote:     {UNMATCHED_CSV.relative_to(WORKSPACE)}")


def stable_review_key(path: Path) -> str:
    return workspace_path(path)


def unmatched_row(
    path: Path,
    tokens: dict[str, object],
    checksum: str,
    flags: list[str],
    best_score: int,
    best: Product | None,
) -> dict[str, object]:
    return {
        "review_file": stable_review_key(path),
        "source_abs": str(path.resolve()),
        "source_path": workspace_path(path),
        "filename": path.name,
        "sha256": checksum,
        "flags": "|".join(flags),
        "folder_hint": tokens["folder_hint"],
        "tokens": json.dumps(tokens, ensure_ascii=False),
        "best_score": best_score,
        "best_match_sku": (best.sku_code or f"INT-{best.id}") if best else "",
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def match_fields() -> list[str]:
    return [
        "review_file", "source_abs", "source_path", "filename", "sha256",
        "width", "height", "flags", "status", "best_score", "category_code",
        "target_sku", "target_family", "product_name", "brand", "size",
        "color", "folder_hint", "alternatives", "suggested_decision",
    ]


def unmatched_fields() -> list[str]:
    return [
        "review_file", "source_abs", "source_path", "filename", "sha256",
        "flags", "folder_hint", "tokens", "best_score", "best_match_sku",
    ]


def build_review(args: argparse.Namespace) -> None:
    matched = read_csv(args.matches)
    unmatched = read_csv(args.unmatched)
    data = []
    for row in matched:
        data.append(review_record(row, "matched"))
    for row in unmatched:
        data.append(review_record(row, "unmatched"))

    if args.limit:
        data = data[: args.limit]

    REVIEW_HTML.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_HTML.write_text(render_review_html(data), encoding="utf-8")
    print(f"Review rows: {len(data)}")
    print(f"Wrote:       {REVIEW_HTML.relative_to(WORKSPACE)}")


def review_record(row: dict[str, str], source: str) -> dict[str, str]:
    path = Path(row["source_abs"])
    return {
        "file": row.get("review_file") or row.get("source_path") or row.get("filename", ""),
        "abs_uri": path.resolve().as_uri(),
        "source": source,
        "status": row.get("status", "unmatched"),
        "category": row.get("category_code", row.get("folder_hint", "")),
        "sku": row.get("target_sku", row.get("best_match_sku", "")),
        "family": row.get("target_family", ""),
        "product_name": row.get("product_name", ""),
        "brand": row.get("brand", ""),
        "size": row.get("size", ""),
        "color": row.get("color", ""),
        "score": row.get("best_score", ""),
        "flags": row.get("flags", ""),
        "suggested": row.get("suggested_decision", ""),
        "filename": row.get("filename", ""),
    }


def render_review_html(data: list[dict[str, str]]) -> str:
    json_data = json.dumps(data, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>Product Photo Review</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #181818; color: #eee; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }}
header {{ background: #242424; padding: 10px 16px; border-bottom: 2px solid #c41e2a; display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
h1 {{ font-size: 18px; }}
.stats {{ font-size: 13px; color: #bbb; white-space: nowrap; }}
.progress {{ background: #333; height: 4px; }}
.progress-bar {{ background: #f5a800; height: 100%; transition: width .2s; }}
main {{ flex: 1; display: grid; grid-template-columns: minmax(0, 1fr) 390px; gap: 16px; padding: 16px; overflow: hidden; }}
.photo-pane {{ background: #fff; border-radius: 8px; display: flex; align-items: center; justify-content: center; overflow: hidden; min-height: 0; }}
.photo-pane img {{ max-width: 100%; max-height: 100%; object-fit: contain; }}
.empty {{ color: #666; font-size: 18px; padding: 30px; text-align: center; }}
.info-pane {{ background: #222; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; }}
.info-block {{ background: #2b2b2b; padding: 10px 12px; border-radius: 6px; }}
.label {{ font-size: 11px; color: #999; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }}
.value {{ font-size: 14px; overflow-wrap: anywhere; }}
.sku {{ color: #f5a800; font-weight: 700; font-size: 16px; }}
.pname {{ color: #fff; line-height: 1.4; }}
.meta {{ display: grid; grid-template-columns: 94px 1fr; gap: 6px 8px; font-size: 12px; }}
.k {{ color: #999; }} .v {{ color: #ddd; overflow-wrap: anywhere; }}
.actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: auto; }}
button {{ padding: 13px 10px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 700; color: #fff; }}
button:hover {{ opacity: .86; }} button:active {{ transform: scale(.98); }}
.b-single {{ background: #2e7d32; }} .b-pack {{ background: #1565c0; }} .b-family {{ background: #7b4fa3; }}
.b-reject {{ background: #c41e2a; }} .b-skip {{ background: #555; }} .b-prev {{ background: #444; grid-column: span 2; }}
.keys {{ font-size: 11px; color: #aaa; line-height: 1.8; }}
kbd {{ background: #444; color: #fff; padding: 2px 6px; border-radius: 3px; font-family: monospace; font-size: 11px; }}
.footer {{ background: #242424; padding: 10px 16px; display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
.footer button {{ padding: 6px 14px; font-size: 12px; background: #f5a800; color: #000; }}
#goto {{ background: #333; color: #eee; border: 1px solid #555; padding: 4px 8px; border-radius: 4px; width: 70px; }}
@media (max-width: 900px) {{ main {{ grid-template-columns: 1fr; grid-template-rows: minmax(0, 1fr) auto; }} .info-pane {{ max-height: 42vh; }} }}
</style>
</head>
<body>
<header>
  <h1>Product Photo Review</h1>
  <div class="stats"><span id="position">1</span> / <span id="total">0</span> · <span id="decided">0</span> decided · single <span id="ct-single">0</span> · pack <span id="ct-pack">0</span> · family <span id="ct-family">0</span> · reject <span id="ct-reject">0</span></div>
</header>
<div class="progress"><div class="progress-bar" id="bar" style="width:0%"></div></div>
<main>
  <div class="photo-pane" id="photo"><div class="empty">Loading...</div></div>
  <aside class="info-pane">
    <div class="info-block"><div class="label">Proposed SKU</div><div class="value sku" id="sku">-</div></div>
    <div class="info-block"><div class="label">Product name</div><div class="value pname" id="pname">-</div></div>
    <div class="info-block"><div class="label">Details</div><div class="meta">
      <span class="k">Family</span><span class="v" id="family">-</span>
      <span class="k">Category</span><span class="v" id="category">-</span>
      <span class="k">Brand</span><span class="v" id="brand">-</span>
      <span class="k">Size</span><span class="v" id="size">-</span>
      <span class="k">Color</span><span class="v" id="color">-</span>
      <span class="k">Score</span><span class="v" id="score">-</span>
      <span class="k">Status</span><span class="v" id="status">-</span>
      <span class="k">Flags</span><span class="v" id="flags">-</span>
    </div></div>
    <div class="info-block"><div class="label">File</div><div class="value" id="filename">-</div></div>
    <div class="info-block"><div class="label">Current decision</div><div class="value sku" id="current-decision">-</div></div>
    <div class="actions">
      <button class="b-single" onclick="decide('single')">1 · Single</button>
      <button class="b-pack" onclick="decide('pack')">2 · Pack</button>
      <button class="b-family" onclick="decide('family')">4 · Family</button>
      <button class="b-reject" onclick="decide('reject')">3 · Reject</button>
      <button class="b-skip" onclick="decide('skip')">→ · Skip</button>
      <button class="b-prev" onclick="prev()">← · Previous</button>
    </div>
    <div class="keys"><kbd>1</kbd> single <kbd>2</kbd> pack <kbd>3</kbd> reject <kbd>4</kbd> family <kbd>→</kbd>/<kbd>j</kbd> next <kbd>←</kbd>/<kbd>k</kbd> prev <kbd>g</kbd> goto</div>
  </aside>
</main>
<div class="footer">
  <div>Go to: <input id="goto" type="number" min="1" /> <button onclick="exportCSV()">Export decisions CSV</button> <button onclick="clearAll()" style="background:#c41e2a;color:#fff;">Clear All</button></div>
  <div class="stats">Auto-saved to browser localStorage</div>
</div>
<script>
const DATA = {json_data};
const STORAGE_KEY = 'product-photo-review-decisions-v1';
let idx = 0;
let decisions = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}');
function save() {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(decisions)); }}
function counts() {{
  const c = {{ single: 0, pack: 0, reject: 0, skip: 0, family: 0 }};
  Object.values(decisions).forEach(d => {{ if (c[d] !== undefined) c[d]++; }});
  return c;
}}
function set(id, value) {{ document.getElementById(id).textContent = value || '-'; }}
function render() {{
  if (DATA.length === 0) {{ document.getElementById('photo').innerHTML = '<div class="empty">No files found. Run match first.</div>'; return; }}
  idx = Math.max(0, Math.min(idx, DATA.length - 1));
  const r = DATA[idx];
  document.getElementById('photo').innerHTML = '<img src="' + r.abs_uri + '" alt="' + r.file.replace(/"/g, '&quot;') + '">';
  set('sku', r.sku); set('pname', r.product_name || '(no SKU match)'); set('family', r.family);
  set('category', r.category); set('brand', r.brand); set('size', r.size); set('color', r.color);
  set('score', r.score); set('status', r.status); set('flags', r.flags); set('filename', r.file);
  set('current-decision', decisions[r.file] || r.suggested || '-');
  set('position', idx + 1); set('total', DATA.length);
  document.getElementById('bar').style.width = ((idx + 1) / DATA.length * 100) + '%';
  const c = counts(); set('decided', Object.keys(decisions).length); set('ct-single', c.single); set('ct-pack', c.pack); set('ct-family', c.family); set('ct-reject', c.reject);
}}
function decide(action) {{
  const r = DATA[idx];
  if (action === 'skip') delete decisions[r.file]; else decisions[r.file] = action;
  save(); next();
}}
function next() {{ idx = Math.min(idx + 1, DATA.length - 1); render(); }}
function prev() {{ idx = Math.max(idx - 1, 0); render(); }}
document.addEventListener('keydown', e => {{
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '1') decide('single'); else if (e.key === '2') decide('pack'); else if (e.key === '3') decide('reject'); else if (e.key === '4') decide('family');
  else if (e.key === 'ArrowRight' || e.key === 'j') next(); else if (e.key === 'ArrowLeft' || e.key === 'k') prev(); else if (e.key === 'g') document.getElementById('goto').focus();
}});
document.getElementById('goto').addEventListener('keydown', e => {{
  if (e.key === 'Enter') {{ const v = parseInt(e.target.value) - 1; if (!isNaN(v)) {{ idx = v; render(); }} e.target.blur(); }}
}});
function exportCSV() {{
  let csv = 'file,decision\\n';
  Object.entries(decisions).forEach(([k, v]) => {{ csv += '"' + k.replace(/"/g, '""') + '",' + v + '\\n'; }});
  const blob = new Blob([csv], {{ type: 'text/csv' }});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'product_photo_decisions.csv'; a.click();
}}
function clearAll() {{ if (!confirm('Clear all decisions?')) return; decisions = {{}}; save(); render(); }}
render();
</script>
</body>
</html>
"""


def apply_decisions(args: argparse.Namespace) -> None:
    decisions = {r["file"]: r["decision"] for r in read_csv(args.decisions) if r.get("decision")}
    if not decisions:
        raise SystemExit(f"no decisions found in: {args.decisions}")

    rows = {r["review_file"]: r for r in read_csv(args.matches)}
    rows.update({r["review_file"]: r for r in read_csv(args.unmatched)})

    applied = []
    skipped = 0
    for review_file, decision in decisions.items():
        row = rows.get(review_file)
        if not row:
            skipped += 1
            continue
        src = Path(row["source_abs"])
        if not src.exists():
            skipped += 1
            continue
        dest = destination_for(row, decision)
        if dest is None:
            skipped += 1
            continue
        if not args.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        applied.append({
            "file": review_file,
            "decision": decision,
            "source_abs": str(src),
            "dest_path": workspace_path(dest),
        })

    out = EXPORT_DIR / "product_photo_applied.csv"
    write_csv(out, applied, ["file", "decision", "source_abs", "dest_path"])
    print(f"Applied: {len(applied)}{' (dry-run)' if args.dry_run else ''}")
    print(f"Skipped: {skipped}")
    print(f"Wrote:   {out.relative_to(WORKSPACE)}")


def destination_for(row: dict[str, str], decision: str) -> Path | None:
    decision = decision.strip().lower()
    category = safe_part(row.get("category_code") or row.get("folder_hint") or "other")
    checksum = (row.get("sha256") or "nohash")[:10]
    src = Path(row["source_abs"])
    ext = src.suffix.lower()
    if decision == "reject":
        stem = safe_part(src.stem)
        return PRODUCT_ROOT / "_unmatched" / f"{stem}__{checksum}{ext}"
    if decision not in {"single", "pack", "family"}:
        return None
    code = row.get("target_family") if decision == "family" else row.get("target_sku")
    if not code:
        code = row.get("best_match_sku") or src.stem
    return PRODUCT_ROOT / category / decision / f"{safe_part(code)}__{checksum}{ext}"


def export_assets(args: argparse.Namespace) -> None:
    if Image is None or ImageOps is None:
        raise SystemExit("Pillow is not available; image export is disabled.")

    specs = {
        "web": (1200, 1200, "WEBP", ".webp"),
        "catalog": (1500, 1500, "JPEG", ".jpg"),
        "social": (1080, 1080, "JPEG", ".jpg"),
    }
    width, height, fmt, ext = specs[args.profile]
    sources = []
    for bucket in ("single", "pack", "family"):
        sources.extend(PRODUCT_ROOT.glob(f"*/{bucket}/*"))
    sources = [p for p in sorted(sources) if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if args.limit:
        sources = sources[: args.limit]

    dest_root = CATALOG_ROOT / "exports" / args.profile
    exported = []
    for src in sources:
        dest = dest_root / f"{safe_part(src.stem)}{ext}"
        if not args.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            normalize_image(src, dest, width, height, fmt)
        exported.append({"source_path": workspace_path(src), "dest_path": workspace_path(dest), "profile": args.profile})

    out = EXPORT_DIR / f"product_photo_export_{args.profile}.csv"
    write_csv(out, exported, ["source_path", "dest_path", "profile"])
    print(f"Exported: {len(exported)}{' (dry-run)' if args.dry_run else ''}")
    print(f"Wrote:    {out.relative_to(WORKSPACE)}")


def normalize_image(src: Path, dest: Path, width: int, height: int, fmt: str) -> None:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in {"RGB", "RGBA"}:
            im = im.convert("RGBA")
        im.thumbnail((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), "white")
        if im.mode == "RGBA":
            bg = Image.new("RGBA", im.size, "white")
            bg.alpha_composite(im)
            im = bg.convert("RGB")
        else:
            im = im.convert("RGB")
        x = (width - im.width) // 2
        y = (height - im.height) // 2
        canvas.paste(im, (x, y))
        save_kwargs = {"quality": 92}
        if fmt == "WEBP":
            save_kwargs = {"quality": 90, "method": 6}
        canvas.save(dest, fmt, **save_kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Product photo automation workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Scan image files and write product_photo_scan.csv")
    scan_p.add_argument("--source", type=Path, required=True)
    scan_p.add_argument("--limit", type=int)
    scan_p.set_defaults(func=scan)

    match_p = sub.add_parser("match", help="Match image files to ERP products")
    match_p.add_argument("--source", type=Path, required=True)
    match_p.add_argument("--limit", type=int)
    match_p.set_defaults(func=match)

    review_p = sub.add_parser("build-review", help="Generate Design/Catalog/photos/review.html")
    review_p.add_argument("--matches", type=Path, default=MATCH_CSV)
    review_p.add_argument("--unmatched", type=Path, default=UNMATCHED_CSV)
    review_p.add_argument("--limit", type=int)
    review_p.set_defaults(func=build_review)

    apply_p = sub.add_parser("apply-decisions", help="Copy files according to exported review decisions")
    apply_p.add_argument("--decisions", type=Path, required=True)
    apply_p.add_argument("--matches", type=Path, default=MATCH_CSV)
    apply_p.add_argument("--unmatched", type=Path, default=UNMATCHED_CSV)
    apply_p.add_argument("--dry-run", action="store_true")
    apply_p.set_defaults(func=apply_decisions)

    export_p = sub.add_parser("export", help="Export normalized approved assets")
    export_p.add_argument("--profile", choices=["web", "catalog", "social"], required=True)
    export_p.add_argument("--limit", type=int)
    export_p.add_argument("--dry-run", action="store_true")
    export_p.set_defaults(func=export_assets)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
