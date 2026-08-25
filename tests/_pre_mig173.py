"""Put a test database back into the world a pre-mig-173 script ran in.

mig 173 makes an UPDATE of a source-document row abort unless it declares who
changed it and why. That is the point of it, and every LIVE writer now declares.
But four test files drive code that ran BEFORE the guard existed:

  · a historical data migration (133), re-applied to a reconstructed pre-state.
    Its own file cannot declare — it is SQL that shipped in April, every
    environment applied it long before 173 was written, and editing an applied
    migration is forbidden here (the runner keys on filename, so an edited body
    reaches only environments that have not run it yet).
  · three DATED ONE-OFF scripts whose own docstrings say not to re-run them.
    `tests/test_source_doc_writer_coverage.py` already records the decision that
    these abort if re-run and that this is ACCEPTED, not overlooked. Their tests
    exist to document what those runs did, so they must run in the world the
    script ran in.

⛔ THIS IS NOT A WAY TO SILENCE THE GUARD. Anything reachable at runtime — a
route, a live tool, the importer — must DECLARE, and `test_pre_mig173_usage.py`
fails if a file outside the recorded list calls this. The one live tool that was
found broken (`scripts/merge_product.py`) was FIXED, not exempted.
"""
import os
import sqlite3

GUARD_TRIGGERS = (
    'sales_transactions_change_needs_declaration',
    'purchase_transactions_change_needs_declaration',
)


def emulate_pre_mig173(target):
    """Drop mig 173's two refusal triggers from `target` (a path or a conn).

    The six AFTER-trigger audit writers are deliberately LEFT IN PLACE: they
    record, they never refuse, and removing them would also remove the evidence
    a test might want to assert on.

    Returns the number of triggers that were actually there, so a caller can
    tell "removed the guard" from "there was no guard to remove" — the second
    means the fixture is not what the test thinks it is.
    """
    own = isinstance(target, (str, os.PathLike))
    conn = sqlite3.connect(str(target)) if own else target
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?)",
            GUARD_TRIGGERS)}
        for name in GUARD_TRIGGERS:
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
        conn.commit()
        return len(present)
    finally:
        if own:
            conn.close()
