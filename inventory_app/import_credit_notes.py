"""
Idempotent importer for the standalone Express ใบลดหนี้ (SR / credit-note) file.

Also provides:
  populate_sr_writeoffs(conn=None) -> dict
      Scan sales_transactions for unattributable SR docs and persist a write-off
      marker in sr_writeoffs.  See that function's docstring for full details.

  written_off_summary(conn=None) -> dict
      Read-only aggregate over sr_writeoffs for reporting.

Design
------
The standalone file (cumulative, Jan 2024 → present) overlaps entirely with SR
rows already present in sales_transactions via prior weekly BSN imports.  A
naive insert would double-count credit notes and corrupt AR/cash-flow.

Safe strategy (proven by Step-1 investigation on the May 2026 file):
  1. BACKFILL: for each file entry whose doc_no already exists in
     sales_transactions, UPDATE ref_invoice when the DB row has NULL/empty ref
     and the file provides one.  Never overwrite a non-null ref with a
     different value — log the conflict and leave the DB unchanged.
     Never touch qty, net, or any other column of existing rows.
  2. NEW SR (not in sales_transactions): record in the side table
     `credit_note_imports` (migration 059) keyed on UNIQUE(doc_no) so the
     table is also idempotent on re-run.  Do NOT insert into sales_transactions
     — we cannot safely reproduce the weekly-row shape (product_id lookup, unit
     normalisation, stock sync) without going through the normal BSN import flow.
  3. Idempotent: running twice leaves row counts and Σ SR net in
     sales_transactions identical.  Second run produces refs_backfilled=0,
     new_recorded=0.

Parser reuse
------------
Uses parse_weekly.parse_credit_notes() verbatim — NOT reimplemented here.
Encoding (cp874) is handled inside the parser.

Return value
------------
dict with keys:
  parsed           — total entries from the file (including cancelled/placeholder)
  existing_matched — file entries whose doc_no was found in sales_transactions
  refs_backfilled  — existing SR rows updated with a new ref_invoice
  ref_conflicts    — existing SR rows where both DB and file have different non-null refs
                     (logged, not changed)
  new_recorded     — entries inserted into credit_note_imports (not in sales_transactions)
  already_new      — entries already in credit_note_imports from a prior run (no-op)
  skipped          — entries skipped for any other reason (e.g. parse anomaly)
  errors           — list of up to 5 distinct exception reprs

Invariant: existing_matched + new_recorded + already_new + skipped
           == parsed  (minus any entries lost to errors)

Connection contract
-------------------
Accepts an optional caller-supplied sqlite3.Connection (used, not committed/closed)
or opens its own via config.DATABASE_PATH.  The caller is responsible for commit
when passing an existing connection; when no conn is given this function commits
internally.

Python 3.9 — no `X | None` syntax.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from config import DATABASE_PATH
from parse_weekly import (
    parse_credit_notes,
    _SR_MASTER_RE,
    _be_to_iso,
    _clean,
    _parse_float_or_zero,
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _open_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ── Public entry point ────────────────────────────────────────────────────────

def import_credit_notes(
    path: str,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Idempotent import of the standalone ใบลดหนี้ file.

    Parameters
    ----------
    path     : str  — absolute path to the ใบลดหนี้ CSV (cp874)
    conn     : optional caller-supplied sqlite3.Connection
               (used, not committed/closed by this function)
    db_path  : optional override of config.DATABASE_PATH when conn is None

    Returns
    -------
    dict — summary as described in the module docstring.
    """
    own_conn = conn is None
    if own_conn:
        conn = _open_conn(db_path)

    entries = parse_credit_notes(path)

    parsed = len(entries)
    existing_matched = 0
    refs_backfilled = 0
    ref_conflicts = []
    new_recorded = 0
    already_new = 0
    skipped = 0
    errors = []

    for i, entry in enumerate(entries):
        sp = f"sp_cn_{i}"
        try:
            conn.execute(f"SAVEPOINT {sp}")
            result = _process_entry(conn, entry, ref_conflicts)
            outcome = result["outcome"]
            if outcome == "matched":
                existing_matched += 1
                refs_backfilled += result.get("backfilled", 0)
            elif outcome == "new_recorded":
                new_recorded += 1
            elif outcome == "already_new":
                already_new += 1
                existing_matched_bump = result.get("existing_matched_bump", 0)
                existing_matched += existing_matched_bump
            elif outcome == "skipped":
                skipped += 1
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
            skipped += 1
            if len(errors) < 5:
                errors.append(repr(exc))

    # Cache the authoritative per-SR credited amount (master "รวมทั้งสิ้น")
    # so payments_alloc can net the EXACT figure instead of the SR detail-line
    # net sum (which is pre-doc-discount and over-credits the invoice).
    cna = _upsert_credit_note_amounts(conn, path)

    if own_conn:
        conn.commit()
        conn.close()

    return {
        "parsed": parsed,
        "credit_note_amounts": cna,
        "existing_matched": existing_matched,
        "refs_backfilled": refs_backfilled,
        "ref_conflicts": ref_conflicts,
        "new_recorded": new_recorded,
        "already_new": already_new,
        "skipped": skipped,
        "errors": errors,
    }


# ── Per-entry logic ───────────────────────────────────────────────────────────

def _process_entry(conn, entry, ref_conflicts):
    """Process one parsed entry.  Mutates ref_conflicts in-place.

    Returns a dict with:
      outcome: "matched" | "new_recorded" | "already_new" | "skipped"
      backfilled: 0 or 1 (only present when outcome=="matched")
    """
    doc_no = entry["doc_no"]

    # ── Case 1: doc_no already in sales_transactions ─────────────────────────
    existing = conn.execute(
        "SELECT id, ref_invoice FROM sales_transactions WHERE doc_no = ?",
        (doc_no,)
    ).fetchone()

    if existing is not None:
        db_ref = (existing["ref_invoice"] or "").strip()
        file_ref = (entry["ref_invoice"] or "").strip()

        backfilled = 0
        if not db_ref and file_ref:
            # DB is null/empty; file has a ref → backfill
            conn.execute(
                "UPDATE sales_transactions SET ref_invoice = ? WHERE id = ?",
                (file_ref, existing["id"])
            )
            backfilled = 1
        elif db_ref and file_ref and db_ref != file_ref:
            # Both non-null but different → log conflict, do not change
            ref_conflicts.append({
                "doc_no": doc_no,
                "db_ref": db_ref,
                "file_ref": file_ref,
            })

        return {"outcome": "matched", "backfilled": backfilled}

    # ── Case 2: doc_no already in credit_note_imports (prior run) ────────────
    existing_cni = conn.execute(
        "SELECT id FROM credit_note_imports WHERE doc_no = ?",
        (doc_no,)
    ).fetchone()

    if existing_cni is not None:
        # Already recorded in the side table — no-op
        return {"outcome": "already_new"}

    # ── Case 3: genuinely new — record in side table ─────────────────────────
    conn.execute(
        """INSERT INTO credit_note_imports
               (doc_no, doc_base, date_iso, customer, salesperson,
                ref_invoice, ref_invoice_line, vat_type,
                bsn_code, product_name_raw, qty, unit, unit_price,
                discount, total, net, cancelled)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_no,
            entry["doc_base"],
            entry["date_iso"],
            entry.get("customer"),
            entry.get("salesperson"),
            entry.get("ref_invoice"),
            entry.get("ref_invoice_line"),
            entry.get("vat_type", 1),
            entry.get("bsn_code"),
            entry.get("product_name_raw"),
            entry.get("qty", 0.0),
            entry.get("unit"),
            entry.get("unit_price", 0.0),
            entry.get("discount"),
            entry.get("total", 0.0),
            entry.get("net", 0.0),
            1 if entry.get("cancelled") else 0,
        )
    )
    return {"outcome": "new_recorded"}


# ── credit_note_amounts upsert (migration 062) ────────────────────────────────

def _upsert_credit_note_amounts(conn, path):
    """Parse the ใบลดหนี้ MASTER lines and cache one row per SR doc_base in
    credit_note_amounts (migration 062).

    The master row's "รวมทั้งสิ้น" column (parse_weekly._SR_MASTER_RE group
    `total_amt`) is the single authoritative credited value: it already
    incorporates the SR's document-level discount and VAT policy. The SR
    *detail* line net stored in sales_transactions is PRE-doc-discount and
    over-credits the invoice — that mismatch is exactly the bug this table
    fixes (the false ฿105,604 "overpaid" balance).

    We re-scan the raw file with the imported `_SR_MASTER_RE` rather than
    re-deriving from parse_credit_notes() output so the master "total" is
    taken verbatim from the master line (the flattened detail entries carry
    the per-line amount, not the master total).

    Idempotent: ON CONFLICT(sr_doc_base) DO UPDATE — re-running the same or a
    superset cumulative file leaves one row per SR with identical values.

    Cancelled SR masters (leading '*') are skipped: a cancelled credit note
    credits nothing, so it must not net against the invoice.

    Returns dict: {'parsed_masters': int, 'upserted': int, 'skipped_cancelled': int}.
    """
    parsed_masters = 0
    upserted = 0
    skipped_cancelled = 0

    # Migration 062 may not be applied yet (pre-062 snapshots / schema-clone
    # test fixtures). Without the table this is a graceful no-op: AR math
    # transparently falls back to the legacy SR.net `cn` in payments_alloc,
    # so behaviour is byte-identical to pre-062 for those databases.
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='credit_note_amounts'"
    ).fetchone()
    if has_table is None:
        return {
            "parsed_masters": 0,
            "upserted": 0,
            "skipped_cancelled": 0,
            "skipped_no_table": True,
        }

    with open(path, encoding="cp874") as f:
        raw_lines = f.readlines()

    for raw in raw_lines:
        line = _clean(raw)
        if not line:
            continue
        m = _SR_MASTER_RE.match(line.lstrip())
        if not m:
            continue
        parsed_masters += 1
        (cancel, sr_no, date_be, customer, salesperson, ref_inv,
         vat_type, doc_disc, goods_val, vat_amt, total_amt) = m.groups()
        if cancel:
            skipped_cancelled += 1
            continue
        credited = _parse_float_or_zero(total_amt)
        ref = ref_inv.strip() if ref_inv else None
        conn.execute(
            """INSERT INTO credit_note_amounts
                   (sr_doc_base, ref_invoice, credited_amount,
                    sr_date_iso, customer, source)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(sr_doc_base) DO UPDATE SET
                   ref_invoice     = excluded.ref_invoice,
                   credited_amount = excluded.credited_amount,
                   sr_date_iso     = excluded.sr_date_iso,
                   customer        = excluded.customer,
                   source          = excluded.source
            """,
            (sr_no, ref, credited, _be_to_iso(date_be),
             customer.strip(), "ใบลดหนี้"),
        )
        upserted += 1

    return {
        "parsed_masters": parsed_masters,
        "upserted": upserted,
        "skipped_cancelled": skipped_cancelled,
    }


# ── SR write-off helpers ──────────────────────────────────────────────────────

def populate_sr_writeoffs(conn=None, db_path=None):
    """Scan sales_transactions for unattributable SR docs and persist write-off
    markers in sr_writeoffs.

    Classification logic (per doc_no row):
      - no_ref      : ref_invoice IS NULL or '' (after strip)
      - pre_system  : ref_invoice non-empty but no sales_transactions row has
                      doc_base = that ref_invoice (pre-cutoff or HS… refs)
      - excluded    : ref_invoice matches a real doc_base in sales_transactions
                      → NOT written off; these SR credit notes net correctly
                      through payments_alloc

    Grain: one sr_writeoffs row per sales_transactions doc_no (line level).
    This is the stable identity key used throughout the pipeline.

    Idempotency: INSERT … ON CONFLICT(sr_doc_no) DO UPDATE net_amount/reason so
    re-running is safe and produces the same rows.  Only non-excluded SRs are
    touched.

    Connection contract: accepts optional caller-supplied conn (used, not
    committed/closed) or opens/owns one via config.DATABASE_PATH.

    Returns
    -------
    dict with keys:
      pre_system  — count of rows classified as pre_system (this run)
      no_ref      — count of rows classified as no_ref (this run)
      total_net   — Σ net_amount across all rows upserted (this run)
    """
    own_conn = conn is None
    if own_conn:
        conn = _open_conn(db_path)

    # Fetch all SR rows from sales_transactions
    sr_rows = conn.execute(
        """SELECT doc_no, doc_base, date_iso, customer, ref_invoice, net
           FROM sales_transactions
           WHERE doc_base LIKE 'SR%'
        """
    ).fetchall()

    # Build a set of all doc_base values that exist in sales_transactions so we
    # can quickly check whether a ref_invoice points to a real in-system invoice.
    # We only need non-SR doc_bases (an SR pointing to another SR is pre_system).
    real_doc_bases = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT doc_base FROM sales_transactions WHERE doc_base NOT LIKE 'SR%'"
        ).fetchall()
    }

    pre_system_count = 0
    no_ref_count = 0
    total_net = 0.0

    for row in sr_rows:
        doc_no = row["doc_no"]
        doc_base = row["doc_base"]
        ref_raw = (row["ref_invoice"] or "").strip()
        net_amount = row["net"] or 0.0

        # Determine classification
        if not ref_raw:
            reason = "no_ref"
        elif ref_raw in real_doc_bases:
            # Ref matches a real in-system invoice — not unattributable; skip
            continue
        else:
            reason = "pre_system"

        # Upsert into sr_writeoffs
        conn.execute(
            """INSERT INTO sr_writeoffs
                   (sr_doc_base, sr_doc_no, reason, ref_invoice_raw,
                    net_amount, customer, sr_date_iso)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sr_doc_no) DO UPDATE SET
                   reason          = excluded.reason,
                   ref_invoice_raw = excluded.ref_invoice_raw,
                   net_amount      = excluded.net_amount,
                   customer        = excluded.customer,
                   sr_date_iso     = excluded.sr_date_iso
            """,
            (
                doc_base,
                doc_no,
                reason,
                row["ref_invoice"],
                net_amount,
                row["customer"],
                row["date_iso"],
            )
        )

        if reason == "pre_system":
            pre_system_count += 1
        else:
            no_ref_count += 1
        total_net += net_amount

    if own_conn:
        conn.commit()
        conn.close()

    return {
        "pre_system": pre_system_count,
        "no_ref": no_ref_count,
        "total_net": round(total_net, 2),
    }


def written_off_summary(conn=None, db_path=None):
    """Read-only aggregate over sr_writeoffs.

    Returns
    -------
    dict:
      pre_system  — {'count': int, 'net': float}
      no_ref      — {'count': int, 'net': float}
      total       — {'count': int, 'net': float}
    """
    own_conn = conn is None
    if own_conn:
        conn = _open_conn(db_path)

    rows = conn.execute(
        """SELECT reason,
                  COUNT(*)        AS cnt,
                  ROUND(SUM(net_amount), 2) AS net_sum
           FROM sr_writeoffs
           GROUP BY reason
        """
    ).fetchall()

    if own_conn:
        conn.close()

    result = {
        "pre_system": {"count": 0, "net": 0.0},
        "no_ref": {"count": 0, "net": 0.0},
        "total": {"count": 0, "net": 0.0},
    }
    for r in rows:
        reason = r["reason"]
        cnt = int(r["cnt"])
        net = float(r["net_sum"] or 0.0)
        if reason in result:
            result[reason]["count"] = cnt
            result[reason]["net"] = net
        result["total"]["count"] += cnt
        result["total"]["net"] = round(result["total"]["net"] + net, 2)

    return result
