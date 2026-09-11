"""Migration 180 — promotions.date_start stops lying about when a promo started.

Issue #500. Every promo on prod carried the placeholder date_start
'2024-01-01', so price_lookup's `promo_start` epoch source always lost the
max(epoch, today-365) clamp and a promotion could never register as a price
change. Put ruled (2026-09-11) that the 2026-catalogue carry-over rows are
STANDING discounts (date_start = NULL) and that promos imported after that
batch are real price events (date_start = their created_at date).

Every test forces its own rows on throwaway products — `tmp_db_conn` clones
the live dev DB WITH data, so nothing here may inherit state — and scopes its
assertions to those ids. The manual-source and already-dated rows are not
scenery: they are the far side of this migration's two WHERE filters, without
which deleting a filter would change no row (verification-discipline.md, "a
test that cannot fail", shape #8).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from pathlib import Path

import pytest

_MIG_DIR = Path(__file__).resolve().parents[1] / 'data/migrations'
MIG_176 = _MIG_DIR / '176_promo_source_and_dates.sql'
MIG_177 = _MIG_DIR / '177_promo_one_per_slot.sql'
MIG_180 = _MIG_DIR / '180_promo_date_start_truth.sql'
ROLLBACK_180 = _MIG_DIR / '180_promo_date_start_truth.rollback.sql'

PLACEHOLDER = '2024-01-01'

_pid = [960000]


@pytest.fixture
def db(tmp_db_conn):
    """Pre-state: mig 176 (source column) and 177 (the one-per-slot triggers
    whose plain BEFORE UPDATE fires on 180's own UPDATEs) applied, 180 NOT."""
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'source' not in cols:
        tmp_db_conn.executescript(MIG_176.read_text())
    tmp_db_conn.executescript(MIG_177.read_text())      # drop-first, safe to re-run
    tmp_db_conn.executescript(ROLLBACK_180.read_text())  # undo 180 if this clone had it
    tmp_db_conn.commit()
    return tmp_db_conn


def _mk_product(conn, name):
    _pid[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, 'ตัว', 100, 60, 1)", (f'{name} #{_pid[0]}',))
    conn.commit()
    return cur.lastrowid


def _promo(conn, pid, *, date_start, created_at, source='catalog-import',
           promo_type='percent', discount_value=10.0, is_active=1):
    cur = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
        " date_start, date_end, is_active, source, created_at) "
        "VALUES (?,?,?,?,?,NULL,?,?,?)",
        (pid, f'catalog {date_start} (promo)', promo_type, discount_value,
         date_start, is_active, source, created_at))
    conn.commit()
    return cur.lastrowid


def _apply(conn, path):
    conn.executescript(path.read_text())
    conn.commit()


def _date_start(conn, promo_id):
    return conn.execute(
        "SELECT date_start FROM promotions WHERE id = ?", (promo_id,)).fetchone()['date_start']


class TestForwardTransform:
    def test_carryover_batch_becomes_a_standing_discount(self, db):
        pid = _mk_product(db, 'mig180 carryover')
        rid = _promo(db, pid, date_start=PLACEHOLDER, created_at='2026-06-01 12:25:57')
        assert _date_start(db, rid) == PLACEHOLDER        # COUNT/state first
        _apply(db, MIG_180)
        assert _date_start(db, rid) is None

    def test_a_later_batch_is_dated_from_its_created_at(self, db):
        pid = _mk_product(db, 'mig180 later batch')
        rid = _promo(db, pid, date_start=PLACEHOLDER, created_at='2026-08-28 04:45:06')
        _apply(db, MIG_180)
        assert _date_start(db, rid) == '2026-08-28'

    def test_the_batch_boundary_is_inclusive_on_the_carryover_side(self, db):
        """2026-06-01 itself is carry-over; 2026-06-02 is already a new event.
        Pins the <= vs > split so an off-by-one cannot pass silently."""
        pid_in = _mk_product(db, 'mig180 boundary in')
        pid_out = _mk_product(db, 'mig180 boundary out')
        rid_in = _promo(db, pid_in, date_start=PLACEHOLDER, created_at='2026-06-01 23:59:59')
        rid_out = _promo(db, pid_out, date_start=PLACEHOLDER, created_at='2026-06-02 00:00:01')
        _apply(db, MIG_180)
        assert _date_start(db, rid_in) is None
        assert _date_start(db, rid_out) == '2026-06-02'

    def test_a_manual_promo_is_never_touched(self, db):
        """The far side of `source = 'catalog-import'`. Drop that clause and
        this row moves — which is the whole point of asserting it."""
        pid = _mk_product(db, 'mig180 manual')
        rid = _promo(db, pid, date_start=PLACEHOLDER,
                     created_at='2026-06-01 12:25:57', source='manual')
        _apply(db, MIG_180)
        assert _date_start(db, rid) == PLACEHOLDER

    def test_a_promo_already_carrying_a_real_date_is_never_touched(self, db):
        """The far side of `date_start = '2024-01-01'`."""
        pid = _mk_product(db, 'mig180 real date')
        rid = _promo(db, pid, date_start='2026-07-01', created_at='2026-07-01 09:00:00')
        _apply(db, MIG_180)
        assert _date_start(db, rid) == '2026-07-01'


class TestGuards:
    def test_postcondition_aborts_when_a_placeholder_row_survives(self, db):
        """A created_at SQLite cannot parse makes date() NULL, so neither UPDATE
        matches and the row keeps the placeholder. The migration must ABORT
        rather than stamp itself and leave the lie in place."""
        pid = _mk_product(db, 'mig180 unparseable created_at')
        rid = _promo(db, pid, date_start=PLACEHOLDER, created_at='not-a-date')
        with pytest.raises(sqlite3.IntegrityError, match='mig 180 postcondition FAILED'):
            _apply(db, MIG_180)
        assert _date_start(db, rid) == PLACEHOLDER      # rolled back, nothing written

    def test_precondition_aborts_on_an_overlapping_same_slot_pair(self, db):
        """Nulling date_start widens a row to 'since forever', and mig 177's
        plain BEFORE UPDATE trigger fires on 180's UPDATEs. The triggers make
        this state unreachable, so it is seeded with them dropped — the point
        is that 180's own guard fires, with a message naming 180."""
        pid = _mk_product(db, 'mig180 overlap')
        db.executescript("DROP TRIGGER IF EXISTS promotions_one_per_slot_ins;"
                         "DROP TRIGGER IF EXISTS promotions_one_per_slot_upd;")
        _promo(db, pid, date_start=PLACEHOLDER, created_at='2026-06-01 12:25:57')
        _promo(db, pid, date_start=PLACEHOLDER, created_at='2026-06-01 12:25:58')
        with pytest.raises(sqlite3.IntegrityError, match='mig 180 precondition FAILED'):
            _apply(db, MIG_180)


class TestRollback:
    def test_rollback_restores_every_touched_row_and_drops_the_snapshot(self, db):
        pid_a = _mk_product(db, 'mig180 rb carryover')
        pid_b = _mk_product(db, 'mig180 rb later')
        rid_a = _promo(db, pid_a, date_start=PLACEHOLDER, created_at='2026-06-01 12:25:57')
        rid_b = _promo(db, pid_b, date_start=PLACEHOLDER, created_at='2026-08-11 08:00:00')

        _apply(db, MIG_180)
        assert (_date_start(db, rid_a), _date_start(db, rid_b)) == (None, '2026-08-11')
        assert db.execute("SELECT COUNT(*) c FROM migration_180_snapshot").fetchone()['c'] >= 2

        _apply(db, ROLLBACK_180)
        assert _date_start(db, rid_a) == PLACEHOLDER
        assert _date_start(db, rid_b) == PLACEHOLDER
        assert db.execute(
            "SELECT COUNT(*) c FROM sqlite_master WHERE name = 'migration_180_snapshot'"
        ).fetchone()['c'] == 0

    def test_rollback_is_re_runnable(self, db):
        _apply(db, MIG_180)
        _apply(db, ROLLBACK_180)
        _apply(db, ROLLBACK_180)   # must not raise "no such table"
