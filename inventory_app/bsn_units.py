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

A MISSING table raises; an EMPTY one reads as "every spelling unknown".
- Missing (`no such table: unit_map`) means the DB predates migration 185,
  e.g. a whole pre-185 file pushed through /admin/upload-db. Translating
  nothing there would silently store raw Express codes, so the error
  propagates from every call.
- Empty is the state of a schema-only clone (tests' `empty_db`). A real DB
  always has rows: an existing one gets them from migration 185, a fresh one
  from data/schema.sql, which scripts/dump_schema.py writes the live map's
  rows into (a fresh DB never runs migrations, so a map changed by a later
  migration reaches it only that way).
"""
from __future__ import annotations

import re
from typing import Optional

BOOK_BSN5657 = 'BSN5657'
BOOK_XP5 = 'xp5'
BOOK_ANY = '*'          # a variant that applies to every book
DEFAULT_BOOK = BOOK_BSN5657


def _connect():
    from database import get_connection
    return get_connection()


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


# ── tier labels ──────────────────────────────────────────────────────────
#
# A tier's `qty_label` is a COUNT plus a หน่วย ('1 โหล', '1กิโล', '200 ใบ').
# Only the unit part is a spelling the map owns; the count is the tier's
# identity and is kept byte-for-byte (#595: "tier labels — the leading count
# is kept and only the unit part is translated", the same rule migration 186
# applied to the stored rows via `ltrim(qty_label, '0123456789 ')`).
#
# Deliberately exact after splitting: '1 โหลคู่' and '1 โหล (special)' must
# NOT collapse onto 'โหล' — dozen-PAIRS and a hand-set special price are
# different tiers of different SKUs, and the map has no entry for either, so
# they pass through untouched.
_TIER_QTY_PREFIX_RE = re.compile(r'^\d+\s*')


def split_tier_label(qty_label):
    """`('1 ', 'โหล')` — the leading count (verbatim) and the unit part.

    Does NOT translate; `normalize_tier_label` is the translating form.
    Kept separate because price lookup needs the unit part on its own to
    compare against the unit a caller asked about.
    """
    s = qty_label or ''
    m = _TIER_QTY_PREFIX_RE.match(s)
    prefix = m.group(0) if m else ''
    return prefix, s[len(prefix):].strip()


def normalize_tier_label(qty_label, book: str = DEFAULT_BOOK, *, conn=None):
    """`qty_label` with its unit part translated and its count untouched.

    Returns the ORIGINAL string (not a rebuilt one) whenever the unit part
    does not translate, so an unknown label — or one whose spacing the split
    would not reproduce — is never silently rewritten.
    """
    prefix, unit = split_tier_label(qty_label)
    if not unit:
        return qty_label
    word = translate(unit, book, conn=conn)
    if word is None or word == unit:
        return qty_label
    return prefix + word
