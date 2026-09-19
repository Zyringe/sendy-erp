"""Unit map — one DB table (`unit_map`), keyed by Express book + spelling.

Translates every Express unit code, and every other spelling variant, into
the ONE Sendy word for that หน่วย. See docs/adr/0018 and the "Units (หน่วย)"
section of CONTEXT.md.

This ticket (#596) moves the map from a JSON file (data/reference/
bsn_unit_full.json, now deleted) to the DB with NO MEANING CHANGE: the table
is seeded with exactly the 44 entries the JSON held, all under BSN5657
(`DEFAULT_BOOK`) — a caller that doesn't pass a `book` yet keeps reading
today's translations (`กร` still means `ตัว` here; tickets #599/#601 correct
that and add xp5-specific rows such as `หอ` -> `หลอด`).

No per-process cache. A code `learn()`ed by one gunicorn worker must be
visible to the OTHER worker on its very next request, so every call reads the
DB fresh — the same PR#103 per-worker-state trap the JSON version's
stat-keyed cache existed to dodge, just solved here by not caching at all
(a 44-row indexed SQLite lookup is microseconds; nothing here runs a request
anywhere near the old JSON-reparse hot loop that justified that cache).

Callers that already hold a `sqlite3.Connection` should pass `conn=` — the
lookup then runs on THEIR connection (same transaction, and in a test, the
temp DB the test's fixture pointed at) instead of a fresh one. Callers with
no connection (a bare script, or a call site that never needed a `conn`
before) get one from `database.get_connection()`, imported lazily so this
module still imports with no hard Flask/DB dependency for callers that
always pass their own `conn`.

FAIL LOUD when unseeded. A `conn` whose `unit_map` has zero rows total (a
brand-new DB built from data/schema.sql, or a hand-built test schema that
never ran migration 185) raises `UnitMapNotSeeded` rather than silently
treating every code as unknown — see that class's docstring. A caller
about to do a bulk unit-bearing write (an importer, the VAT-book builder)
should never point `conn=` at a database other than the one main app db
`unit_map` is actually seeded in; `vat_book_builder.py::seed_products_from_
stmas` is the worked example of passing the translation in explicitly
instead.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

BOOK_BSN5657 = 'BSN5657'
BOOK_XP5 = 'xp5'
BOOK_ANY = '*'          # a variant that applies to every book
DEFAULT_BOOK = BOOK_BSN5657


class UnitMapNotSeeded(RuntimeError):
    """`unit_map` has zero rows in total (or doesn't exist at all) — this DB
    was never seeded with migration 185's data. A brand-new DB built from
    data/schema.sql does NOT run migration INSERTs (only their CREATE TABLE
    side): `run_pending_migrations`'s bootstrap-backfill path records every
    migration as already-applied without re-executing it, so a fresh boot
    (bare `git clone`, an empty Railway volume, or a subprocess building a
    throwaway db in its own DATA_DIR) can reach here with a structurally
    correct but completely empty table.

    Raised instead of silently treating EVERY Express code as unknown and
    passing it through untranslated — that would import raw codes across an
    entire fresh environment with nothing to flag it, exactly what #595
    exists to prevent. See docs/adr/0018."""


def _connect():
    from database import get_connection
    return get_connection()


def _no_such_table(exc: sqlite3.OperationalError) -> bool:
    return 'no such table: unit_map' in str(exc)


def _assert_seeded(conn) -> None:
    """One row is enough to prove SOME migration actually ran the INSERTs —
    an empty table (or a missing one) means none did."""
    try:
        seeded = conn.execute("SELECT 1 FROM unit_map LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError as exc:
        if _no_such_table(exc):
            seeded = False
        else:
            raise
    if not seeded:
        raise UnitMapNotSeeded(
            "unit_map has no rows — refusing to translate any unit until it "
            "is seeded (re-run migration 185, or point at a DB that already "
            "has it applied).")


def translate(spelling, book: str = DEFAULT_BOOK, *, conn=None) -> Optional[str]:
    """The one Sendy word for `spelling` in `book`, or None if unknown.

    A book-specific row wins; a `BOOK_ANY` (book-independent) row is the
    fallback for a spelling variant that means the same thing in every book.

    Raises `UnitMapNotSeeded` if the table is empty — see that class's
    docstring. This is deliberately NOT the same as `spelling` being
    unknown (which returns None, the normal "flag it for Put" case): an
    unseeded map cannot tell known from unknown at all, so pretending
    everything is "unknown" would be a silent, DB-wide translation outage.
    """
    if not spelling:
        return None
    own = conn is None
    conn = conn or _connect()
    try:
        _assert_seeded(conn)
        row = conn.execute(
            "SELECT word FROM unit_map WHERE book = ? AND spelling = ?",
            (book, spelling)).fetchone()
        if row is None and book != BOOK_ANY:
            row = conn.execute(
                "SELECT word FROM unit_map WHERE book = ? AND spelling = ?",
                (BOOK_ANY, spelling)).fetchone()
        return row[0] if row is not None else None
    finally:
        if own:
            conn.close()


def normalize_unit(spelling, book: str = DEFAULT_BOOK, *, conn=None):
    """`translate()`, falling back to `spelling` unchanged when unknown — an
    unmapped code surfaces as-is (pending review on /unit-conversions)
    instead of vanishing. Mirrors the JSON version's `map.get(unit, unit)`."""
    if not spelling:
        return spelling
    return translate(spelling, book, conn=conn) or spelling


def full_units(*, conn=None) -> set:
    """Every canonical Sendy word the map currently produces (the `word`
    column, deduplicated) — for suggestion widgets that must offer words,
    never codes. Raises `UnitMapNotSeeded` on an empty table (see
    `translate`'s docstring); an empty SET here would silently tell a
    suggestion widget that NO word is canonical yet."""
    own = conn is None
    conn = conn or _connect()
    try:
        _assert_seeded(conn)
        return {r[0] for r in conn.execute("SELECT DISTINCT word FROM unit_map")}
    finally:
        if own:
            conn.close()


def is_known(spelling, book: str = DEFAULT_BOOK, *, conn=None) -> bool:
    """True if `spelling` already translates, or is itself one of the
    canonical words (so re-checking an already-normalised value still
    reads as known, matching the JSON version's `unit in m or unit in
    set(m.values())`)."""
    if not spelling:
        return False
    if translate(spelling, book, conn=conn) is not None:
        return True
    return spelling in full_units(conn=conn)


def load_unit_map(book: str = DEFAULT_BOOK, *, conn=None) -> dict:
    """One dict snapshot (`spelling -> word`) for a hot loop that reads the
    map ONCE and then does local `.get()` lookups per row (the shape
    `detect_document_drift` needs over ~150k lines). Read fresh on every
    call — never cached across calls — so a second gunicorn worker always
    sees a `learn()` a sibling worker just made.

    Book-specific rows are overlaid on top of `BOOK_ANY` rows, matching
    `translate()`'s precedence. Raises `UnitMapNotSeeded` on an empty table
    (see `translate`'s docstring) — an empty DICT here would make a caller
    like `detect_document_drift` silently treat every raw Express code on
    both sides as already-matching-Sendy's-stored-value, hiding drift
    instead of finding it.
    """
    own = conn is None
    conn = conn or _connect()
    try:
        _assert_seeded(conn)
        out = {}
        for spelling, word in conn.execute(
                "SELECT spelling, word FROM unit_map WHERE book = ?", (BOOK_ANY,)):
            out[spelling] = word
        if book != BOOK_ANY:
            for spelling, word in conn.execute(
                    "SELECT spelling, word FROM unit_map WHERE book = ?", (book,)):
                out[spelling] = word
        return out
    finally:
        if own:
            conn.close()


def learn(spelling: str, book: str, word: str, *, conn=None) -> None:
    """Record a new spelling -> word row (idempotent upsert). Writes on the
    SAME connection when given one, so a caller mid-transaction can still
    roll the whole request back on a later failure; opens + commits its own
    otherwise."""
    spelling = (spelling or '').strip()
    word = (word or '').strip()
    if not spelling or not word or spelling == word:
        return
    own = conn is None
    conn = conn or _connect()
    try:
        conn.execute(
            "INSERT INTO unit_map (book, spelling, word) VALUES (?, ?, ?) "
            "ON CONFLICT(book, spelling) DO UPDATE SET word = excluded.word",
            (book, spelling, word))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def add_acronym(acronym: str, full: str, *, conn=None) -> None:
    """Back-compat name for the /unit-conversions naming flow
    (models/bsn_sync.py::learn_acronyms_normalize) and a couple of scripts:
    `learn()` against `DEFAULT_BOOK`. Pending rows on /unit-conversions come
    only from purchase_transactions/sales_transactions (the BSN weekly-import
    ledger) today, so BSN5657 is the correct book for every caller of this
    function — a caller that knows a different book should call `learn()`
    directly."""
    learn(acronym, DEFAULT_BOOK, full, conn=conn)
