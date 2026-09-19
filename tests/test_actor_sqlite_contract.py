"""#590: the SQLite behaviour the cost guards rest on, pinned by assertions.

These replace the print-only spike from the design branch. Each test is a
claim the design (docs/specs/2026-09-19-590-cost-audit-actor-design.md §2)
makes about SQLite itself, run against the REAL resolver (`actor.install`)
on a scratch in-memory schema. A newer SQLite that changes any of it turns
one of these red before a guard built on it silently stops working.

Each test carries a control: the same statement on a connection where it must
succeed, so a failure cannot be explained by a broken fixture.
"""
import sqlite3

import pytest

import actor

SCHEMA = """
CREATE TABLE p (id INTEGER PRIMARY KEY, name TEXT, cost REAL DEFAULT 0);
CREATE TABLE log (who TEXT, what TEXT);
"""
COST_TRIGGERS = """
CREATE TRIGGER p_cost_guard BEFORE UPDATE OF cost ON p
WHEN OLD.cost IS NOT NEW.cost AND sendy_actor('who') IS NULL
BEGIN SELECT RAISE(ABORT, 'cost change needs an actor'); END;
CREATE TRIGGER p_cost_audit AFTER UPDATE OF cost ON p
WHEN OLD.cost IS NOT NEW.cost
BEGIN INSERT INTO log VALUES (sendy_actor('who'), 'cost ' || NEW.id); END;
"""
PUT = actor.Actor(source='manual', who='put', kind='script', detail='x.py: why')


@pytest.fixture
def db(tmp_path):
    """A file DB, so several connections can see one schema. The triggers are
    created on a RAW connection: CREATE TRIGGER must not need the function."""
    path = str(tmp_path / 'contract.db')
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA + COST_TRIGGERS)
    raw.execute("INSERT INTO p (id, name, cost) VALUES (1, 'a', 10), (2, 'b', 20)")
    raw.commit()
    raw.close()
    return path


def _signed(path, bound=PUT):
    return actor.install(sqlite3.connect(path), bound)


def test_a_persistent_trigger_calls_the_registered_resolver(db):
    c = _signed(db)
    c.execute('UPDATE p SET cost = 11 WHERE id = 1')
    assert c.execute('SELECT who FROM log').fetchall() == [('put',)]


def test_a_column_list_trigger_spares_raw_non_cost_writes(db):
    raw = sqlite3.connect(db)
    raw.execute("UPDATE p SET name = 'renamed' WHERE id = 1")      # control: allowed
    assert raw.execute('SELECT name FROM p WHERE id = 1').fetchone()[0] == 'renamed'
    for sql in ('UPDATE p SET cost = 12 WHERE id = 1',
                'UPDATE p SET cost = cost WHERE id = 1'):          # even a no-op
        with pytest.raises(sqlite3.OperationalError, match='no such function: sendy_actor'):
            raw.execute(sql)


def test_an_insert_trigger_naming_the_function_breaks_every_raw_insert(db):
    """Why products INSERT stays a ceiling: the WHEN below is always false, and
    the raw INSERT still fails, because SQLite resolves the function when it
    compiles the statement."""
    raw = sqlite3.connect(db)
    raw.execute("INSERT INTO p (name) VALUES ('before')")           # control
    raw.execute("CREATE TRIGGER p_ins AFTER INSERT ON p WHEN 0 "
                "BEGIN INSERT INTO log VALUES (sendy_actor('who'), 'ins'); END")
    with pytest.raises(sqlite3.OperationalError, match='no such function: sendy_actor'):
        raw.execute("INSERT INTO p (name) VALUES ('after')")


def test_an_unsigned_connection_is_refused_by_the_guard(db):
    anon = actor.install(sqlite3.connect(db), None)
    actor.set_fallback(None)
    with pytest.raises(sqlite3.IntegrityError, match='cost change needs an actor'):
        anon.execute('UPDATE p SET cost = 13 WHERE id = 1')
    _signed(db).execute('UPDATE p SET cost = 13 WHERE id = 1')      # control


def test_trusted_schema_off_makes_the_trigger_unsafe(db):
    c = _signed(db)
    c.execute('PRAGMA trusted_schema = OFF')
    with pytest.raises(sqlite3.OperationalError, match='unsafe use of sendy_actor'):
        c.execute('UPDATE p SET cost = 14 WHERE id = 1')
    c.execute('PRAGMA trusted_schema = ON')                         # control
    c.execute('UPDATE p SET cost = 14 WHERE id = 1')


def test_an_identity_bound_to_one_connection_never_resolves_on_another(db):
    actor.set_fallback(None)
    a = _signed(db, PUT)
    b = actor.install(sqlite3.connect(db), None)
    a.execute('UPDATE p SET cost = 15 WHERE id = 1')                # control: a is signed
    a.commit()
    with pytest.raises(sqlite3.IntegrityError, match='needs an actor'):
        b.execute('UPDATE p SET cost = 25 WHERE id = 2')


def test_raise_abort_backs_out_only_its_own_statement(db):
    """Why A2 checks the actor in Python BEFORE the first write: a guard's
    ABORT leaves the transaction's earlier statements in place, committable."""
    actor.set_fallback(None)
    c = actor.install(sqlite3.connect(db), None)
    c.execute('BEGIN')
    c.execute("INSERT INTO log VALUES ('nobody', 'an earlier write')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute('UPDATE p SET cost = 16 WHERE id = 1')
    c.commit()
    assert c.execute("SELECT count(*) FROM log WHERE what = 'an earlier write'").fetchone()[0] == 1


def test_replace_skips_update_triggers_and_upsert_fires_them(db):
    """Why REPLACE INTO is named in the ceiling and UPSERT is not."""
    actor.set_fallback(None)
    anon = actor.install(sqlite3.connect(db), None)
    anon.execute("INSERT OR REPLACE INTO p (id, name, cost) VALUES (1, 'a', 99)")
    assert anon.execute('SELECT cost FROM p WHERE id = 1').fetchone()[0] == 99
    with pytest.raises(sqlite3.IntegrityError, match='needs an actor'):
        anon.execute("INSERT INTO p (id, name, cost) VALUES (2, 'b', 98) "
                     "ON CONFLICT(id) DO UPDATE SET cost = excluded.cost")
