"""Phase 2a — models.promotions.replace_promotion: close-old-by-date +
open-new, never by flipping is_active early. See task-2-brief.md 2a.

Every test forces its own state on a throwaway product inside `tmp_db_conn`
(clones the live dev DB WITH data — force fixture state, never inherit it).
`today` is pinned to a literal ISO date per test, never wall-clock.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from models import promotions as promo_models

MIG_176 = Path(__file__).resolve().parents[1] / 'data/migrations/176_promo_source_and_dates.sql'

TODAY = '2026-08-27'


def _d(n):
    """TODAY + n days, ISO string."""
    return (date.fromisoformat(TODAY) + timedelta(days=n)).isoformat()


@pytest.fixture
def db(tmp_db_conn):
    """tmp_db_conn, guaranteed to have migration 176 applied (promotions.source
    + date_start/date_end columns usable) — same ruling as test_price_lookup.py."""
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'source' not in cols:
        tmp_db_conn.executescript(MIG_176.read_text())
        tmp_db_conn.commit()
    return tmp_db_conn


_pid_counter = [800000]


def _mk_product(conn, name, *, unit_type='ตัว', base=100.0, cost=60.0, active=1):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?,?,?,?,?)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, active),
    )
    conn.commit()
    return cur.lastrowid


def _mk_promo(conn, pid, *, promo_type, discount_value=None, bundle_buy=None,
              bundle_free=None, gift_desc=None, gift_qty=None,
              date_start=None, date_end=None, is_active=1, source=None,
              promo_name='seed'):
    cur = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
        "bundle_buy, bundle_free, gift_desc, gift_qty, date_start, date_end, "
        "is_active, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, promo_name, promo_type, discount_value, bundle_buy, bundle_free,
         gift_desc, gift_qty, date_start, date_end, is_active, source),
    )
    conn.commit()
    return cur.lastrowid


def _promo_rows(conn, pid):
    return conn.execute(
        "SELECT * FROM promotions WHERE product_id = ? ORDER BY id", (pid,)
    ).fetchall()


# ── replace_promotion: close-old + open-new ─────────────────────────────────

class TestReplacePromotion:
    def test_replace_percent_with_fixed_today_closes_old_price_slot(self, db):
        pid = _mk_product(db, 'p')
        old_id = _mk_promo(db, pid, promo_type='percent', discount_value=10,
                            date_start=_d(-30), source='manual')

        ok, msg, new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'new fixed', 'promo_type': 'fixed', 'discount_value': 50},
            today=TODAY, conn=db,
        )
        assert ok, msg

        rows = _promo_rows(db, pid)
        assert len(rows) == 2
        old = next(r for r in rows if r['id'] == old_id)
        new = next(r for r in rows if r['id'] == new_id)
        # Old row: date_end moved to the day before new_start, is_active UNTOUCHED
        assert old['date_end'] == _d(-1)
        assert old['is_active'] == 1
        # New row: current today, source='manual', date_start=today (no date_start posted)
        assert new['promo_type'] == 'fixed'
        assert new['date_start'] == TODAY
        assert new['source'] == 'manual'
        assert new['is_active'] == 1

        # Exactly ONE current price-slot row today
        current_price_ids = [
            r['id'] for r in rows
            if promo_models.is_current(r, TODAY)
            and promo_models.promo_slots_for(
                db, r['promo_type'], r['discount_value'], r['bundle_buy'], r['gift_desc'])[0]
        ]
        assert current_price_ids == [new_id]

    def test_qty_slot_untouched_control(self, db):
        """Replacing a price-slot promo must not touch an unrelated qty-slot promo."""
        pid = _mk_product(db, 'p')
        _mk_promo(db, pid, promo_type='percent', discount_value=10, date_start=_d(-30))
        qty_id = _mk_promo(db, pid, promo_type='bundle', bundle_buy=12, bundle_free=1,
                           date_start=_d(-30))

        ok, msg, _new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'new fixed', 'promo_type': 'fixed', 'discount_value': 50},
            today=TODAY, conn=db,
        )
        assert ok, msg

        qty_row = db.execute("SELECT * FROM promotions WHERE id = ?", (qty_id,)).fetchone()
        assert qty_row['date_end'] is None
        assert qty_row['is_active'] == 1

    def test_future_date_start_keeps_old_current_until_day_before(self, db):
        pid = _mk_product(db, 'p')
        old_id = _mk_promo(db, pid, promo_type='percent', discount_value=10, date_start=_d(-30))

        future_start = _d(7)
        ok, msg, new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'future fixed', 'promo_type': 'fixed', 'discount_value': 50,
                  'date_start': future_start},
            today=TODAY, conn=db,
        )
        assert ok, msg

        old = db.execute("SELECT * FROM promotions WHERE id = ?", (old_id,)).fetchone()
        new = db.execute("SELECT * FROM promotions WHERE id = ?", (new_id,)).fetchone()
        assert old['date_end'] == _d(6)   # future_start - 1 day
        assert old['is_active'] == 1
        assert new['date_start'] == future_start

        # today .. today+6: OLD is current, NEW is not
        for offset in range(0, 7):
            day = _d(offset)
            assert promo_models.is_current(old, day) is True, day
            assert promo_models.is_current(new, day) is False, day
        # today+7 onward: NEW is current, OLD is not
        assert promo_models.is_current(old, future_start) is False
        assert promo_models.is_current(new, future_start) is True

    def test_backdated_new_start_refused_nothing_written(self, db):
        pid = _mk_product(db, 'p')
        old_id = _mk_promo(db, pid, promo_type='percent', discount_value=10, date_start=_d(-30))
        before = _promo_rows(db, pid)

        ok, msg, new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'backdated', 'promo_type': 'fixed', 'discount_value': 50,
                  'date_start': _d(-1)},
            today=TODAY, conn=db,
        )
        assert ok is False
        assert new_id is None

        after = _promo_rows(db, pid)
        assert len(after) == len(before) == 1
        old = db.execute("SELECT * FROM promotions WHERE id = ?", (old_id,)).fetchone()
        assert old['date_end'] is None   # untouched
        assert old['is_active'] == 1

    def test_mixed_row_occupying_both_slots_closes_both_current_occupants(self, db):
        pid = _mk_product(db, 'p')
        price_id = _mk_promo(db, pid, promo_type='percent', discount_value=10, date_start=_d(-30))
        qty_id = _mk_promo(db, pid, promo_type='bundle', bundle_buy=12, bundle_free=1,
                           date_start=_d(-30))

        ok, msg, new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'mixed replace', 'promo_type': 'mixed',
                  'discount_value': 5, 'bundle_buy': 24, 'bundle_free': 2},
            today=TODAY, conn=db,
        )
        assert ok, msg
        price_row = db.execute("SELECT * FROM promotions WHERE id = ?", (price_id,)).fetchone()
        qty_row = db.execute("SELECT * FROM promotions WHERE id = ?", (qty_id,)).fetchone()
        assert price_row['date_end'] == _d(-1)
        assert qty_row['date_end'] == _d(-1)
        new = db.execute("SELECT * FROM promotions WHERE id = ?", (new_id,)).fetchone()
        assert new['promo_type'] == 'mixed'

    def test_no_existing_current_promo_just_inserts(self, db):
        """First-ever promo for a product: nothing to close, plain insert."""
        pid = _mk_product(db, 'p')
        ok, msg, new_id = promo_models.replace_promotion(
            pid, {'promo_name': 'first', 'promo_type': 'percent', 'discount_value': 10},
            today=TODAY, conn=db,
        )
        assert ok, msg
        rows = _promo_rows(db, pid)
        assert len(rows) == 1
        assert rows[0]['id'] == new_id


# ── deactivate_promotion stamps date_end when NULL ──────────────────────────

class TestDeactivateStampsDateEnd:
    def test_deactivate_stamps_date_end_when_null(self, db):
        pid = _mk_product(db, 'p')
        promo_id = _mk_promo(db, pid, promo_type='percent', discount_value=10, date_start=_d(-30))
        promo_models.deactivate_promotion(promo_id, today=TODAY, conn=db)
        row = db.execute("SELECT * FROM promotions WHERE id = ?", (promo_id,)).fetchone()
        assert row['is_active'] == 0
        assert row['date_end'] == TODAY

    def test_deactivate_does_not_override_existing_date_end(self, db):
        pid = _mk_product(db, 'p')
        promo_id = _mk_promo(db, pid, promo_type='percent', discount_value=10,
                             date_start=_d(-30), date_end=_d(10))
        promo_models.deactivate_promotion(promo_id, today=TODAY, conn=db)
        row = db.execute("SELECT * FROM promotions WHERE id = ?", (promo_id,)).fetchone()
        assert row['is_active'] == 0
        assert row['date_end'] == _d(10)   # untouched, already set


# ── is_current predicate — the shared vocabulary ────────────────────────────

class TestIsCurrent:
    def test_inactive_never_current(self):
        row = {'is_active': 0, 'date_start': None, 'date_end': None}
        assert promo_models.is_current(row, TODAY) is False

    def test_future_date_start_not_current(self):
        row = {'is_active': 1, 'date_start': _d(1), 'date_end': None}
        assert promo_models.is_current(row, TODAY) is False

    def test_past_date_end_not_current(self):
        row = {'is_active': 1, 'date_start': None, 'date_end': _d(-1)}
        assert promo_models.is_current(row, TODAY) is False

    def test_open_ended_within_window_is_current(self):
        row = {'is_active': 1, 'date_start': _d(-1), 'date_end': None}
        assert promo_models.is_current(row, TODAY) is True

    def test_no_dates_at_all_is_current(self):
        row = {'is_active': 1, 'date_start': None, 'date_end': None}
        assert promo_models.is_current(row, TODAY) is True


# ── route-level: POST /products/<id>/promotions/new now REPLACES ───────────

@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _pid_with_no_promos(tmp_db):
    """A real product with its promotions cleared (force fixture state —
    the live catalog import stamps most products with an open-ended promo,
    see verification-discipline.md)."""
    conn = sqlite3.connect(tmp_db)
    pid = conn.execute("SELECT id FROM products WHERE is_active = 1 LIMIT 1").fetchone()[0]
    conn.execute("DELETE FROM promotions WHERE product_id = ?", (pid,))
    conn.commit()
    conn.close()
    return pid


class TestPromotionNewRouteReplaces:
    def test_backdated_date_start_redirects_and_writes_nothing(self, admin_client, tmp_db):
        pid = _pid_with_no_promos(tmp_db)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        r = admin_client.post(
            f'/products/{pid}/promotions/new',
            data={'promo_name': 'backdated route test', 'promo_type': 'percent',
                  'discount_value': '10', 'date_start': yesterday},
            follow_redirects=False,
        )
        assert r.status_code == 302  # 302 on refusal too — flash + redirect, not re-render
        conn = sqlite3.connect(tmp_db)
        found = conn.execute(
            "SELECT 1 FROM promotions WHERE product_id = ? AND promo_name = 'backdated route test'",
            (pid,)).fetchone()
        conn.close()
        assert found is None  # nothing written

    def test_new_promo_closes_the_old_one_in_the_same_slot(self, admin_client, tmp_db):
        pid = _pid_with_no_promos(tmp_db)
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
            "date_start, is_active) VALUES (?, 'old percent', 'percent', 10, '2020-01-01', 1)",
            (pid,))
        old_id = conn.execute(
            "SELECT id FROM promotions WHERE product_id = ? AND promo_name = 'old percent'",
            (pid,)).fetchone()[0]
        conn.commit()
        conn.close()

        r = admin_client.post(
            f'/products/{pid}/promotions/new',
            data={'promo_name': 'new fixed route test', 'promo_type': 'fixed',
                  'discount_value': '50'},
            follow_redirects=False,
        )
        assert r.status_code == 302

        conn = sqlite3.connect(tmp_db)
        old_row = conn.execute(
            "SELECT date_end, is_active FROM promotions WHERE id = ?", (old_id,)).fetchone()
        new_row = conn.execute(
            "SELECT promo_type, source, date_start FROM promotions "
            "WHERE product_id = ? AND promo_name = 'new fixed route test'", (pid,)).fetchone()
        conn.close()
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert old_row == (yesterday, 1)
        assert new_row == ('fixed', 'manual', today)
