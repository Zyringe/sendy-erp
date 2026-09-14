"""Migration 181 — existing employee rows move to the canonical identity shape.

#464 (spec #460). The write path now stores national ID, phone and bank
account as bare digits and "not recorded" as NULL (`hr_queries.
_identity_to_digits`). This migration brings the rows written BEFORE that to
the same shape: separators stripped, '' collapsed to NULL.

Measured on the prod snapshot of 2026-09-14 (9 employees): 2 national IDs and
2 phones stored as '', 3 bank accounts stored as '', 2 bank accounts stored
with dashes. Nothing else was non-canonical, and the only separator anywhere
was '-'.

Every test seeds its own rows with raw SQL — the app's writer normalizes, so it
cannot produce the "before" state — and scopes value assertions to those ids.
`tmp_db_conn` clones the dev DB WITH its real staff, whose identity values are
real personal data: the whole-table comparisons below report WHICH (id, column)
differs, never the value.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import hashlib
import sqlite3
from pathlib import Path

import pytest

import hr_queries as hrq

_MIG_DIR = Path(__file__).resolve().parents[1] / 'data/migrations'
MIG_181 = _MIG_DIR / '181_employee_identity_canonical.sql'
ROLLBACK_181 = _MIG_DIR / '181_employee_identity_canonical.rollback.sql'

FIELDS = ('national_id', 'phone', 'bank_account_no')

_code = [0]


@pytest.fixture
def db(tmp_db_conn):
    """Pre-state: 181 NOT applied — undone AND unstamped if this clone had it.

    Forced, never inherited: once 181 ships, the dev DB pulled from prod carries
    both the canonical values and the applied_migrations row. Without the
    un-stamp, the runner test below would find nothing pending and go red for a
    reason that has nothing to do with the migration."""
    tmp_db_conn.executescript(ROLLBACK_181.read_text())
    tmp_db_conn.execute("DELETE FROM applied_migrations WHERE filename = ?",
                        (MIG_181.name,))
    tmp_db_conn.commit()
    return tmp_db_conn


def _seed(conn, **cols):
    _code[0] += 1
    row = {'emp_code': f'T181_{_code[0]}', 'full_name': 'ทดสอบ ย้ายข้อมูล',
           'company_id': 1, **cols}
    names = ', '.join(row)
    marks = ', '.join('?' * len(row))
    cur = conn.execute(f"INSERT INTO employees ({names}) VALUES ({marks})",
                       tuple(row.values()))
    conn.commit()
    return cur.lastrowid


def _apply(conn, path):
    conn.executescript(path.read_text())
    conn.commit()


def _row(conn, emp_id, cols=FIELDS):
    r = conn.execute(f"SELECT {', '.join(cols)} FROM employees WHERE id=?",
                     (emp_id,)).fetchone()
    assert r is not None, "CONTROL — the seeded employee exists"
    return {c: r[c] for c in cols}


def _employees_state(conn):
    """{(id, column): sha256 of (typeof, value)} over every employee cell.
    typeof is part of it: '' (text) and NULL must not compare equal."""
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(employees)")]
    out = {}
    for r in conn.execute("SELECT * FROM employees ORDER BY id"):
        for c in cols:
            v = r[c]
            out[(r['id'], c)] = hashlib.sha256(
                repr((type(v).__name__, v)).encode()).hexdigest()
    return out


def _schema(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"))


def _cells_that_differ(a, b):
    assert set(a) == set(b), "the set of employee rows changed"
    return sorted(k for k in a if a[k] != b[k])


# ── forward ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('field, stored', [
    ('national_id',     '1-2345-67890-12-3'),
    ('national_id',     '1 2345 67890 12 3'),
    ('phone',           '081-234-5678'),
    ('phone',           '(02) 123-4567'),
    ('bank_account_no', '123-4-56789-0'),
])
def test_separators_are_stripped_the_same_way_the_write_path_strips_them(db, field, stored):
    """The expected value is the write-path normalizer's own answer for the
    same input, so the SQL and the Python cannot drift apart — plus a literal,
    so both cannot be wrong together."""
    emp_id = _seed(db, **{field: stored})
    expected = {field: stored}
    hrq._identity_to_digits(expected)
    assert expected[field] == ''.join(c for c in stored if c.isdigit())

    _apply(db, MIG_181)

    assert _row(db, emp_id)[field] == expected[field]


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('blank', ['', '   ', '-'])
def test_a_blank_value_becomes_null(db, field, blank):
    emp_id = _seed(db, **{field: blank})
    _apply(db, MIG_181)
    got = db.execute(f"SELECT typeof({field}) t FROM employees WHERE id=?",
                     (emp_id,)).fetchone()['t']
    assert got == 'null'


def test_null_and_canonical_values_are_left_alone(db):
    emp_id = _seed(db, national_id='1234567890123', phone=None,
                   bank_account_no='1234567890')
    changed = _seed(db, national_id='1-2345-67890-12-3')   # CONTROL
    _apply(db, MIG_181)
    assert _row(db, changed)['national_id'] == '1234567890123', \
        "CONTROL — the migration ran and did rewrite a dirty row"
    assert _row(db, emp_id) == {'national_id': '1234567890123', 'phone': None,
                                'bank_account_no': '1234567890'}


def test_every_other_column_is_left_alone(db):
    """Digits and dashes inside a name, an address or an account holder's name
    are that text. updated_at is not bumped: nobody edited the employee."""
    cols = ('full_name', 'bank_account_name', 'address', 'emp_code',
            'bank_name', 'note', 'updated_at')
    emp_id = _seed(db, full_name='นาย ทดสอบ-2', bank_account_name='บ-ช 1',
                   address='12/3 ม.4 (ซอย 5)', note='โทร 081-234-5678',
                   bank_name='ธนาคารกสิกรไทย', updated_at='2026-01-01 00:00:00',
                   bank_account_no='123-4-56789-0')
    before = _row(db, emp_id, cols)
    _apply(db, MIG_181)
    assert _row(db, emp_id)['bank_account_no'] == '1234567890', "CONTROL"
    assert _row(db, emp_id, cols) == before


# ── precondition: abort, never guess ────────────────────────────────────────

@pytest.mark.parametrize('field, dirty', [
    ('phone',           '081-234-5678, 02-123-4567'),  # two numbers: a strip glues them
    ('phone',           '+66 81 234 5678'),            # stripping '+' leaves 11 digits
    ('national_id',     '๑๒๓๔๕๖๗๘๙๐๑๒๓'),              # Thai numerals: not [0-9]
    ('bank_account_no', 'ยังไม่มี'),                     # prose: a strip keeps nothing
])
def test_precondition_aborts_on_a_value_it_would_have_to_destroy(db, field, dirty):
    """Only '-', ' ', '(' and ')' may be stripped. Anything else is a value a
    human has to look at, so the migration refuses to run rather than skip the
    row or mangle it. The dirty row also carries a strippable bank account on
    the same employee: nothing may be written, not even the safe half."""
    other = 'bank_account_no' if field != 'bank_account_no' else 'phone'
    emp_id = _seed(db, **{field: dirty, other: '123-4-56789-0'})

    with pytest.raises(sqlite3.IntegrityError, match='mig 181 precondition FAILED'):
        db.executescript(MIG_181.read_text())
    db.rollback()                               # what the runner does

    assert _row(db, emp_id) == {**{f: None for f in FIELDS},
                                field: dirty, other: '123-4-56789-0'}
    assert db.execute("SELECT COUNT(*) c FROM sqlite_master "
                      "WHERE name='migration_181_snapshot'").fetchone()['c'] == 0


def test_the_runner_does_not_stamp_181_when_the_precondition_fails(db, tmp_path, monkeypatch):
    """Through database.run_pending_migrations: the boot fails loud and the
    migration stays pending, so fixing the row and restarting re-runs it."""
    import database
    _seed(db, phone='081-234-5678, 02-123-4567')
    mig_dir = tmp_path / 'migrations'
    mig_dir.mkdir()
    (mig_dir / MIG_181.name).write_text(MIG_181.read_text(), encoding='utf-8')
    monkeypatch.setattr(database, 'MIGRATIONS_DIR', str(mig_dir))
    assert db.execute("SELECT COUNT(*) c FROM applied_migrations").fetchone()['c'] > 0, \
        "CONTROL — the runner takes the pending path, not the bootstrap backfill"

    with pytest.raises(sqlite3.IntegrityError, match='mig 181 precondition FAILED'):
        database.run_pending_migrations(db, verbose=False)

    assert db.execute("SELECT COUNT(*) c FROM applied_migrations WHERE filename=?",
                      (MIG_181.name,)).fetchone()['c'] == 0


# ── idempotent ──────────────────────────────────────────────────────────────

def test_a_second_run_changes_nothing(db):
    emp_id = _seed(db, national_id='1-2345-67890-12-3', phone='',
                   bank_account_no='123-4-56789-0')
    _apply(db, MIG_181)
    assert _row(db, emp_id)['national_id'] == '1234567890123', "CONTROL"
    state = _employees_state(db)
    snap = db.execute("SELECT * FROM migration_181_snapshot "
                      "ORDER BY employee_id, field").fetchall()
    audit = db.execute("SELECT COUNT(*) c FROM audit_log").fetchone()['c']

    _apply(db, MIG_181)

    assert _cells_that_differ(state, _employees_state(db)) == []
    assert db.execute("SELECT * FROM migration_181_snapshot "
                      "ORDER BY employee_id, field").fetchall() == snap
    assert db.execute("SELECT COUNT(*) c FROM audit_log").fetchone()['c'] == audit


# ── rollback ────────────────────────────────────────────────────────────────

def test_rollback_restores_every_employee_cell_and_the_schema_byte_identical(db):
    """Over the WHOLE employees table (the cloned real staff included, whose
    '' and dashed values the forward run does rewrite), not just seeded rows."""
    emp_id = _seed(db, national_id='', phone='(081) 234-5678',
                   bank_account_no='123-4-56789-0')
    state, schema = _employees_state(db), _schema(db)

    _apply(db, MIG_181)
    assert _row(db, emp_id) == {'national_id': None, 'phone': '0812345678',
                                'bank_account_no': '1234567890'}, "CONTROL"
    assert _cells_that_differ(state, _employees_state(db)), \
        "CONTROL — the forward run changed something to undo"

    _apply(db, ROLLBACK_181)

    assert _cells_that_differ(state, _employees_state(db)) == []
    assert _schema(db) == schema


_DASHED = {'national_id': '1-2345-67890-12-3', 'phone': '081-234-5678',
           'bank_account_no': '123-4-56789-0'}
_EDITED = {'national_id': '9999999999999', 'phone': '0899999999',
           'bank_account_no': '9999999999'}


@pytest.mark.parametrize('edited', FIELDS)
def test_rollback_keeps_an_edit_made_after_the_migration(db, edited):
    """A field HR changed after 181 is newer than the snapshot: restoring the
    old spelling would silently undo that edit. Only a value still exactly as
    181 wrote it goes back.

    The guard is written once PER COLUMN in the rollback, so it is pinned once
    per column: for the bank account, a rollback that forgot it would put back
    the OLD account and the next payroll would pay into it."""
    emp_id = _seed(db, **_DASHED)
    _apply(db, MIG_181)
    db.execute(f"UPDATE employees SET {edited} = ? WHERE id = ?",
               (_EDITED[edited], emp_id))
    db.commit()

    _apply(db, ROLLBACK_181)

    got = _row(db, emp_id)
    for f in FIELDS:
        if f != edited:
            assert got[f] == _DASHED[f], f"CONTROL — untouched {f} did go back"
    assert got[edited] == _EDITED[edited]


def test_rollback_is_re_runnable_and_forward_runs_again_after_it(db):
    emp_id = _seed(db, bank_account_no='123-4-56789-0')
    _apply(db, MIG_181)
    _apply(db, ROLLBACK_181)
    _apply(db, ROLLBACK_181)       # must not raise "no such table"
    assert _row(db, emp_id)['bank_account_no'] == '123-4-56789-0'
    _apply(db, MIG_181)
    assert _row(db, emp_id)['bank_account_no'] == '1234567890'
