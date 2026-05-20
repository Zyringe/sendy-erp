"""DEPRECATED: one-off from 2026-05-20. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

One-time, idempotent backfill: normalize historical express_sales.unit.

Why: Express sales were imported with the RAW BSN unit acronym (e.g. 'กล')
while product_code_mapping.bsn_unit is canonical (e.g. 'กล่อง'). The
unit-aware resolver compares the two, so for split BSN codes the exact-unit
override never matched real Express data and rows fell through to the
catch-all product. import_express.py now normalizes on write (parity with
the weekly import, models.py); this script fixes the rows that were already
imported.

NOTE: as of migration 064 this normalization runs AUTOMATICALLY on deploy
(064 seeds bsn_unit_alias and does it in SQL). This script is now OPTIONAL —
kept as an idempotent ad-hoc re-run tool (e.g. after a manual data poke),
not a required deploy step.

    ~/.virtualenvs/erp/bin/python scripts/backfill_express_unit_normalize.py

Idempotent: normalize_unit() is a no-op on already-canonical units, so
re-running changes nothing.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'inventory_app'))  # for bsn_units
import bsn_units  # noqa: E402

DB_PATH = _HERE.parent / 'inventory_app' / 'instance' / 'inventory.db'


def normalize_units(conn):
    """Normalize express_sales.unit in place.
    Returns number of rows updated."""
    rows = conn.execute(
        "SELECT DISTINCT unit FROM express_sales "
        "WHERE unit IS NOT NULL AND unit <> ''"
    ).fetchall()
    units_changed = 0
    for (raw,) in rows:
        canon = bsn_units.normalize_unit(raw)
        if canon != raw:
            cur = conn.execute(
                "UPDATE express_sales SET unit = ? WHERE unit = ?",
                (canon, raw))
            units_changed += cur.rowcount
    conn.commit()
    return units_changed


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        u = normalize_units(conn)
        print(f"[backfill_express_unit_normalize] normalized {u} express_sales rows.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
