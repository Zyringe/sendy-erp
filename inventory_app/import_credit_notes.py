"""
Idempotent importer for the standalone Express ใบลดหนี้ (SR / credit-note) file.

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
from parse_weekly import parse_credit_notes


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

    if own_conn:
        conn.commit()
        conn.close()

    return {
        "parsed": parsed,
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
