"""Phase 2b — migration 177's stacking guard: ONE current promo per
(product, slot), judged by DATE OVERLAP, not by counting active rows.

Every test forces its own state on a throwaway product (`tmp_db_conn` clones
the live dev DB WITH data — never inherit it) and asserts a COUNT before any
property. Break-it-once is recorded in task-2a-review.md's follow-up.

⚠ The brief's test (e) called pid 445 "grandfathered". Measured on PROD
2026-08-27 with the app's own promo_slot_sql: **0 products violate one-per-slot
in either slot** — 445 and 1811 each hold two active promos, but in DIFFERENT
slots (their `mixed` row has discount_value IS NULL, so it is qty-only). So (e)
is a regression guard on `p.id <> NEW.id` and on the slot split, NOT an
exemption for known-bad data. See task-2-brief.md's CORRECTION BLOCK, C3.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from pathlib import Path

import pytest

from models import promotions as promo_models

MIG_176 = Path(__file__).resolve().parents[1] / 'data/migrations/176_promo_source_and_dates.sql'
MIG_177 = Path(__file__).resolve().parents[1] / 'data/migrations/177_promo_one_per_slot.sql'
ROLLBACK_177 = Path(__file__).resolve().parents[1] / 'data/migrations/177_promo_one_per_slot.rollback.sql'

TRIGGERS = ('promotions_one_per_slot_ins', 'promotions_one_per_slot_upd')


@pytest.fixture
def db(tmp_db_conn):
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'source' not in cols:
        tmp_db_conn.executescript(MIG_176.read_text())
    tmp_db_conn.executescript(MIG_177.read_text())   # drop-first shape, safe to re-run
    tmp_db_conn.commit()
    return tmp_db_conn


_pid = [900000]


def _mk_product(conn, name='guard'):
    _pid[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, 'ตัว', 100, 60, 1)", (f'{name} #{_pid[0]}',))
    conn.commit()
    return cur.lastrowid


def _add(conn, pid, name, ptype, *, discount_value=None, bundle_buy=None, bundle_free=None,
         gift_desc=None, gift_qty=None, date_start=None, date_end=None, is_active=1):
    cur = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, bundle_buy, "
        "bundle_free, gift_desc, gift_qty, date_start, date_end, is_active) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, name, ptype, discount_value, bundle_buy, bundle_free, gift_desc, gift_qty,
         date_start, date_end, is_active))
    conn.commit()
    return cur.lastrowid


class TestTriggerInstalled:
    def test_both_triggers_exist_and_the_update_one_has_no_OF_column_list(self, db):
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'promotions_one_per_slot%'")}
        assert names == set(TRIGGERS)
        upd = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", ('promotions_one_per_slot_upd',)).fetchone()[0]
        # `UPDATE OF a, b` fires only when a listed column is in the SET list, so a
        # filtered trigger would miss an UPDATE of product_id / date_start / date_end.
        assert 'BEFORE UPDATE ON promotions' in upd
        assert 'UPDATE OF' not in upd

    def test_trigger_body_embeds_promo_slot_sql_verbatim_so_it_cannot_drift(self, db):
        body = ''.join(r[0] for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE name IN (?,?)", TRIGGERS))
        for alias in ('p', 'NEW'):
            price_expr, qty_expr = promo_models.promo_slot_sql(alias)
            assert price_expr in body, f'price expr for {alias!r} drifted from promo_slot_sql'
            assert qty_expr in body, f'qty expr for {alias!r} drifted from promo_slot_sql'

    def test_trigger_body_never_mentions_source(self, db):
        """mig 176's rollback does ALTER TABLE promotions DROP COLUMN source —
        a trigger referencing that column would make the rollback fail."""
        body = ''.join(r[0] for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE name IN (?,?)", TRIGGERS))
        assert 'source' not in body

    def test_rollback_removes_both_triggers(self, db):
        db.executescript(ROLLBACK_177.read_text())
        left = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN (?,?)", TRIGGERS).fetchone()[0]
        assert left == 0
        # control: with the guard gone, the stacking INSERT the next test refuses now succeeds
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        _add(db, pid, 'B', 'fixed', discount_value=50, date_start='2026-08-15')
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id = ?",
                          (pid,)).fetchone()[0] == 2


class TestInsertGuard:
    def test_second_overlapping_price_promo_is_refused(self, db):
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match='one current price promo'):
            _add(db, pid, 'B', 'fixed', discount_value=50, date_start='2026-08-15')
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0] == 1

    def test_bundle_next_to_percent_is_allowed_different_slots(self, db):
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        _add(db, pid, 'C', 'bundle', bundle_buy=12, bundle_free=1, date_start='2026-08-01')
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0] == 2

    def test_adjacent_windows_allowed_one_day_overlap_refused(self, db):
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01', date_end='2026-08-31')
        # starts the day AFTER A ends → adjacent, no overlap
        _add(db, pid, 'B', 'fixed', discount_value=50, date_start='2026-09-01')
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0] == 2

        pid2 = _mk_product(db)
        _add(db, pid2, 'A', 'percent', discount_value=10, date_start='2026-08-01', date_end='2026-08-31')
        with pytest.raises(sqlite3.IntegrityError):
            # starts the SAME day A ends → one-day overlap
            _add(db, pid2, 'B', 'fixed', discount_value=50, date_start='2026-08-31')

    def test_inactive_rows_are_invisible_to_the_guard(self, db):
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        _add(db, pid, 'E', 'fixed', discount_value=50, date_start='2026-08-15', is_active=0)
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0] == 2

    def test_open_ended_rows_overlap_everything_in_their_slot(self, db):
        """NULL date_start/date_end must read as 'since forever'/'until forever' —
        the 566 prod rows are exactly this shape."""
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10)          # both dates NULL
        with pytest.raises(sqlite3.IntegrityError):
            _add(db, pid, 'B', 'fixed', discount_value=50, date_start='2030-01-01')

    def test_mixed_row_with_only_a_discount_occupies_the_price_slot_only(self, db):
        """The exact shape of prod pids 445 / 1811: a `mixed` row whose
        discount_value IS NULL is qty-only, so it sits beside a `fixed` row."""
        pid = _mk_product(db)
        _add(db, pid, 'M', 'mixed', bundle_buy=1, bundle_free=1, date_start='2026-08-01')
        _add(db, pid, 'F', 'fixed', discount_value=220, date_start='2026-08-01')
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0] == 2
        # control: a mixed row that DOES carry a discount takes the price slot and collides
        pid2 = _mk_product(db)
        _add(db, pid2, 'M', 'mixed', discount_value=5, bundle_buy=1, bundle_free=1,
             date_start='2026-08-01')
        with pytest.raises(sqlite3.IntegrityError):
            _add(db, pid2, 'F', 'fixed', discount_value=220, date_start='2026-08-01')


class TestUpdateGuard:
    def test_moving_product_id_onto_an_occupied_slot_is_refused(self, db):
        a = _mk_product(db); b = _mk_product(db)
        _add(db, a, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        moving = _add(db, b, 'B', 'fixed', discount_value=50, date_start='2026-08-15')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE promotions SET product_id = ? WHERE id = ?", (a, moving))
        db.rollback()
        assert db.execute("SELECT product_id FROM promotions WHERE id=?", (moving,)).fetchone()[0] == b

    def test_extending_date_end_into_the_next_promos_window_is_refused(self, db):
        pid = _mk_product(db)
        first = _add(db, pid, 'A', 'percent', discount_value=10,
                     date_start='2026-08-01', date_end='2026-08-31')
        _add(db, pid, 'B', 'fixed', discount_value=50, date_start='2026-09-01')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE promotions SET date_end = '2026-09-30' WHERE id = ?", (first,))
        db.rollback()
        assert db.execute("SELECT date_end FROM promotions WHERE id=?", (first,)).fetchone()[0] == '2026-08-31'

    def test_moving_date_start_backwards_into_the_previous_window_is_refused(self, db):
        """date_start is not in any `OF` list precisely because of this shape."""
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01', date_end='2026-08-31')
        second = _add(db, pid, 'B', 'fixed', discount_value=50, date_start='2026-09-01')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE promotions SET date_start = '2026-08-15' WHERE id = ?", (second,))
        db.rollback()

    def test_no_op_self_update_never_raises(self, db):
        """`p.id <> NEW.id` — without it a row would see ITSELF as the occupant
        and every UPDATE on an active promo would abort."""
        pid = _mk_product(db)
        one = _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        db.execute("UPDATE promotions SET is_active = 1 WHERE id = ?", (one,))
        db.execute("UPDATE promotions SET promo_name = 'renamed' WHERE id = ?", (one,))
        db.commit()
        assert db.execute("SELECT promo_name FROM promotions WHERE id=?", (one,)).fetchone()[0] == 'renamed'

    def test_two_slots_on_one_product_survive_every_no_op_update(self, db):
        """Regression guard for the real prod shape (pids 445 / 1811): two active
        promos in DIFFERENT slots must each stay updatable."""
        pid = _mk_product(db)
        m = _add(db, pid, 'M', 'mixed', bundle_buy=1, bundle_free=1, date_start='2026-06-01')
        f = _add(db, pid, 'F', 'fixed', discount_value=220, date_start='2026-06-01')
        for row_id in (m, f):
            db.execute("UPDATE promotions SET is_active = 1 WHERE id = ?", (row_id,))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=? AND is_active=1",
                          (pid,)).fetchone()[0] == 2

    def test_deactivating_never_raises(self, db):
        pid = _mk_product(db)
        a = _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        db.execute("UPDATE promotions SET is_active = 0 WHERE id = ?", (a,))
        db.commit()
        # and now the slot is free
        _add(db, pid, 'B', 'fixed', discount_value=50, date_start='2026-08-15')
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=? AND is_active=1",
                          (pid,)).fetchone()[0] == 1


class TestReplacePromotionUnderTheGuard:
    """2a and 2b must agree: replace_promotion's own overlap predicate has to
    keep the trigger quiet, including the transient mid-transaction states."""

    def test_ordinary_replace_does_not_trip_the_trigger(self, db):
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        ok, msg, new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'B', 'promo_type': 'fixed', 'discount_value': 50},
            today='2026-08-27', conn=db)
        assert ok, msg
        assert db.execute("SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0] == 2

    def test_cancel_conflicts_path_does_not_trip_the_trigger(self, db):
        pid = _mk_product(db)
        # NOTE: A must already be CLOSED at 09-02 — an open-ended A beside a
        # 09-03 Y is a genuine overlap and the trigger refuses to seed it. That
        # is the point: once 177 is installed, the stacked state that BLOCKER 1
        # produced is not even constructible; the only way to hold a scheduled
        # promo is via replace_promotion, which closes its predecessor by date.
        _add(db, pid, 'A', 'percent', discount_value=10,
             date_start='2026-08-01', date_end='2026-09-02')
        _add(db, pid, 'Y', 'fixed', discount_value=50, date_start='2026-09-03')
        ok, msg, new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'Z', 'promo_type': 'fixed', 'discount_value': 40,
                  'date_start': '2026-08-30'},
            today='2026-08-27', conn=db, cancel_conflicts=True)
        assert ok, msg
        rows = db.execute("SELECT promo_name, is_active FROM promotions WHERE product_id=? ORDER BY id",
                          (pid,)).fetchall()
        assert [(r[0], r[1]) for r in rows] == [('A', 1), ('Y', 0), ('Z', 1)]
        # A was re-closed by DATE at Z's start - 1; Y was cancelled, not date-closed
        a_end = db.execute("SELECT date_end FROM promotions WHERE product_id=? AND promo_name='A'",
                           (pid,)).fetchone()[0]
        assert a_end == '2026-08-29'

    def test_future_dated_replace_does_not_trip_the_trigger(self, db):
        """The adjacency case the overlap test exists for: old row kept
        is_active=1 with date_end = new_start-1, right beside the new row."""
        pid = _mk_product(db)
        _add(db, pid, 'A', 'percent', discount_value=10, date_start='2026-08-01')
        ok, msg, _ = promo_models.replace_promotion(
            pid, {'promo_name': 'B', 'promo_type': 'fixed', 'discount_value': 50,
                  'date_start': '2026-09-03'},
            today='2026-08-27', conn=db)
        assert ok, msg
        rows = db.execute("SELECT promo_name, date_start, date_end, is_active FROM promotions "
                          "WHERE product_id=? ORDER BY id", (pid,)).fetchall()
        assert [tuple(r) for r in rows] == [
            ('A', '2026-08-01', '2026-09-02', 1),
            ('B', '2026-09-03', None, 1),
        ]
