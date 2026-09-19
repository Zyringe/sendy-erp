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

NEVER EMPTY BY CONSTRUCTION, not by a runtime guard. A fresh DB built from
data/schema.sql gets the `unit_map` TABLE (schema.sql is DDL) but, without
help, none of migration 185's rows (a fresh-DB boot backfills every
migration as already-applied without re-running it). `database.py::init_db()`
closes that gap directly: it re-seeds `unit_map` from migration 185's own
SQL whenever the table exists but is empty, so every DB this module's
`_connect()` can ever reach already has the real 44 rows before a request
is served. See `init_db()`'s docstring for why this is (a), not a per-call
raise: an early version of this file raised on every call when a table had
zero rows, which broke ~100 unrelated tests that build a schema-only DB
clone for other purposes and only incidentally pass through a bsn_units
call — the guard's blast radius was disproportionate to the deployment-only
risk it existed to catch, and `init_db()` closes the ACTUAL risk (a fresh
boot) without touching every test fixture in the suite.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

BOOK_BSN5657 = 'BSN5657'
BOOK_XP5 = 'xp5'
BOOK_ANY = '*'          # a variant that applies to every book
DEFAULT_BOOK = BOOK_BSN5657


def _connect():
    from database import get_connection
    return get_connection()


def _no_such_table(exc: sqlite3.OperationalError) -> bool:
    return 'no such table: unit_map' in str(exc)


def translate(spelling, book: str = DEFAULT_BOOK, *, conn=None) -> Optional[str]:
    """The one Sendy word for `spelling` in `book`, or None if unknown.

    A book-specific row wins; a `BOOK_ANY` (book-independent) row is the
    fallback for a spelling variant that means the same thing in every book.
    """
    if not spelling:
        return None
    own = conn is None
    conn = conn or _connect()
    try:
        row = conn.execute(
            "SELECT word FROM unit_map WHERE book = ? AND spelling = ?",
            (book, spelling)).fetchone()
        if row is None and book != BOOK_ANY:
            row = conn.execute(
                "SELECT word FROM unit_map WHERE book = ? AND spelling = ?",
                (BOOK_ANY, spelling)).fetchone()
        return row[0] if row is not None else None
    except sqlite3.OperationalError as exc:
        # Only a DB whose migrations never ran (a test importing this module
        # in isolation, before any fixture has run init_db()) hits this — a
        # REAL boot always runs init_db() before serving a request, and
        # init_db() guarantees unit_map is seeded (see module docstring).
        # Read paths degrade to "unknown" rather than crash; learn() below
        # does not.
        if _no_such_table(exc):
            return None
        raise
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
    never codes."""
    own = conn is None
    conn = conn or _connect()
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT word FROM unit_map")}
    except sqlite3.OperationalError as exc:
        if _no_such_table(exc):
            return set()
        raise
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
    `translate()`'s precedence.
    """
    own = conn is None
    conn = conn or _connect()
    try:
        out = {}
        for spelling, word in conn.execute(
                "SELECT spelling, word FROM unit_map WHERE book = ?", (BOOK_ANY,)):
            out[spelling] = word
        if book != BOOK_ANY:
            for spelling, word in conn.execute(
                    "SELECT spelling, word FROM unit_map WHERE book = ?", (book,)):
                out[spelling] = word
        return out
    except sqlite3.OperationalError as exc:
        if _no_such_table(exc):
            return {}
        raise
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
