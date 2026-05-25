"""Tests for the extended /promotions/new flow + display.

Covers the UI integration of mig 086's bundle/gift/mixed/condition promo
types into Sendy: the form POST → create_promotion → DB INSERT path, and
the /products/<id> GET that renders all 5 promo types in the promotion
list + tier prices section.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _first_active_product_id(tmp_db) -> int:
    conn = sqlite3.connect(tmp_db)
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active = 1 LIMIT 1"
    ).fetchone()[0]
    conn.close()
    return pid


# ── models.create_promotion accepts all new fields ──────────────────────────

class TestCreatePromotionExtended:
    def test_create_percent(self, tmp_db):
        import models
        pid = _first_active_product_id(tmp_db)
        promo_id = models.create_promotion({
            'product_id': pid,
            'promo_name': 'test percent',
            'promo_type': 'percent',
            'discount_value': 15,
        })
        assert promo_id > 0
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT promo_type, discount_value, bundle_buy FROM promotions WHERE id = ?",
            (promo_id,)).fetchone()
        conn.close()
        assert row == ('percent', 15.0, None)

    def test_create_bundle(self, tmp_db):
        import models
        pid = _first_active_product_id(tmp_db)
        promo_id = models.create_promotion({
            'product_id': pid,
            'promo_name': 'test bundle',
            'promo_type': 'bundle',
            'bundle_buy': 12,
            'bundle_free': 1,
            'bundle_unit': 'ดอก',
        })
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT promo_type, bundle_buy, bundle_free, bundle_unit, discount_value "
            "FROM promotions WHERE id = ?", (promo_id,)).fetchone()
        conn.close()
        assert row == ('bundle', 12, 1, 'ดอก', None)

    def test_create_gift(self, tmp_db):
        import models
        pid = _first_active_product_id(tmp_db)
        promo_id = models.create_promotion({
            'product_id': pid,
            'promo_name': 'test gift',
            'promo_type': 'gift',
            'gift_desc': 'ดจ.สแตนเลส',
            'gift_qty': '20 ดอก',
        })
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT promo_type, gift_desc, gift_qty FROM promotions WHERE id = ?",
            (promo_id,)).fetchone()
        conn.close()
        assert row == ('gift', 'ดจ.สแตนเลส', '20 ดอก')

    def test_create_mixed_with_condition(self, tmp_db):
        import models
        pid = _first_active_product_id(tmp_db)
        promo_id = models.create_promotion({
            'product_id': pid,
            'promo_name': 'test ยกลัง 5%',
            'promo_type': 'mixed',
            'discount_value': 5,
            'bundle_condition': 'ยกลัง',
        })
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT promo_type, discount_value, bundle_condition FROM promotions WHERE id = ?",
            (promo_id,)).fetchone()
        conn.close()
        assert row == ('mixed', 5.0, 'ยกลัง')

    def test_create_bundle_without_buy_fails_check(self, tmp_db):
        """DB CHECK enforces bundle requires bundle_buy NOT NULL."""
        import models
        pid = _first_active_product_id(tmp_db)
        with pytest.raises(sqlite3.IntegrityError):
            models.create_promotion({
                'product_id': pid,
                'promo_name': 'bad bundle',
                'promo_type': 'bundle',
                # missing bundle_buy + bundle_free
            })


# ── POST /products/<id>/promotions/new with new types ────────────────────────

class TestPromotionNewRoute:
    def test_post_bundle_succeeds(self, admin_client, tmp_db):
        pid = _first_active_product_id(tmp_db)
        r = admin_client.post(
            f'/products/{pid}/promotions/new',
            data={
                'promo_name': 'route bundle test',
                'promo_type': 'bundle',
                'bundle_buy': '12',
                'bundle_free': '1',
                'bundle_unit': 'ตัว',
            },
            follow_redirects=False,
        )
        assert r.status_code == 302  # redirects to product_detail
        conn = sqlite3.connect(tmp_db)
        found = conn.execute(
            "SELECT bundle_buy, bundle_free, bundle_unit FROM promotions "
            "WHERE product_id = ? AND promo_name = 'route bundle test'",
            (pid,)).fetchone()
        conn.close()
        assert found == (12, 1, 'ตัว')

    def test_post_gift_succeeds(self, admin_client, tmp_db):
        pid = _first_active_product_id(tmp_db)
        r = admin_client.post(
            f'/products/{pid}/promotions/new',
            data={
                'promo_name': 'route gift test',
                'promo_type': 'gift',
                'gift_desc': 'ดจ.สแตนเลส',
                'gift_qty': '20 ดอก',
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        conn = sqlite3.connect(tmp_db)
        found = conn.execute(
            "SELECT gift_desc, gift_qty FROM promotions "
            "WHERE product_id = ? AND promo_name = 'route gift test'",
            (pid,)).fetchone()
        conn.close()
        assert found == ('ดจ.สแตนเลส', '20 ดอก')

    def test_post_mixed_with_condition(self, admin_client, tmp_db):
        pid = _first_active_product_id(tmp_db)
        r = admin_client.post(
            f'/products/{pid}/promotions/new',
            data={
                'promo_name': 'route ยกลัง',
                'promo_type': 'mixed',
                'discount_value': '5',
                'bundle_condition': 'ยกลัง',
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        conn = sqlite3.connect(tmp_db)
        found = conn.execute(
            "SELECT discount_value, bundle_condition FROM promotions "
            "WHERE product_id = ? AND promo_name = 'route ยกลัง'",
            (pid,)).fetchone()
        conn.close()
        assert found == (5.0, 'ยกลัง')

    def test_post_bundle_missing_buy_rerenders_form(self, admin_client, tmp_db):
        """Server-side validation catches missing bundle_buy before DB."""
        pid = _first_active_product_id(tmp_db)
        r = admin_client.post(
            f'/products/{pid}/promotions/new',
            data={
                'promo_name': 'bad',
                'promo_type': 'bundle',
                # missing bundle_buy + bundle_free
            },
            follow_redirects=False,
        )
        # Re-renders the form (200), doesn't redirect
        assert r.status_code == 200

    def test_post_percent_over_100_rerenders_form(self, admin_client, tmp_db):
        pid = _first_active_product_id(tmp_db)
        r = admin_client.post(
            f'/products/{pid}/promotions/new',
            data={
                'promo_name': 'bad pct',
                'promo_type': 'percent',
                'discount_value': '150',
            },
            follow_redirects=False,
        )
        assert r.status_code == 200


# ── GET /products/<id> renders all promo types + tiers ──────────────────────

class TestProductDetailRendersAllTypes:
    def _seed(self, tmp_db, pid):
        """Insert one of each promo type + 2 tiers for the given product.

        Each promo gets an explicit, monotonically-increasing created_at so
        `get_active_promotion`'s `ORDER BY created_at DESC LIMIT 1` is
        deterministic (otherwise rapid-fire inserts can tie on the second).
        The 'render-mixed-ยกลัง' row is intentionally LAST (latest timestamp)
        so it becomes the active promo for `test_active_promo_badge_*`.
        """
        conn = sqlite3.connect(tmp_db)
        conn.execute("PRAGMA foreign_keys = ON")
        # Clear any existing promos/tiers for this product (test isolation)
        conn.execute("DELETE FROM promotions WHERE product_id = ?", (pid,))
        conn.execute("DELETE FROM product_price_tiers WHERE product_id = ?", (pid,))

        seeds = [
            # (created_at, sql, params)
            ('2026-01-01 09:00:01',
             "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, created_at) "
             "VALUES (?, 'render-pct', 'percent', 10, ?)"),
            ('2026-01-01 09:00:02',
             "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, created_at) "
             "VALUES (?, 'render-fixed', 'fixed', 75, ?)"),
            ('2026-01-01 09:00:03',
             "INSERT INTO promotions (product_id, promo_name, promo_type, bundle_buy, bundle_free, bundle_unit, created_at) "
             "VALUES (?, 'render-bundle', 'bundle', 12, 1, 'ดอก', ?)"),
            ('2026-01-01 09:00:04',
             "INSERT INTO promotions (product_id, promo_name, promo_type, gift_desc, gift_qty, created_at) "
             "VALUES (?, 'render-gift', 'gift', 'ดจ.สแตนเลส', '20 ดอก', ?)"),
            ('2026-01-01 09:00:05',
             "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, bundle_condition, created_at) "
             "VALUES (?, 'render-mixed-ยกลัง', 'mixed', 5, 'ยกลัง', ?)"),
        ]
        for ts, sql in seeds:
            # Each SQL has placeholders for (pid, ts)
            conn.execute(sql, (pid, ts))
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price) "
            "VALUES (?, '1 โหล', 230)", (pid,))
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price, note) "
            "VALUES (?, '1 ลัง', 2400, 'bulk discount')", (pid,))
        conn.commit()
        conn.close()

    def _split_body(self, body: str):
        """Split rendered detail page into (info_section, promotions_section)
        by the unique `bi-percent` marker on the promotions card-header icon.
        Active-promo badge lives in info_section (top-of-page product info card);
        promotion list lives in promotions_section.
        """
        marker = 'bi-percent'
        parts = body.split(marker)
        assert len(parts) == 2, (
            f"expected exactly one {marker!r} marker, found {len(parts) - 1}")
        return parts[0], parts[1]

    def test_promotion_list_renders_all_5_types(self, admin_client, tmp_db):
        """The promotions table (lower card) must render every type fully."""
        pid = _first_active_product_id(tmp_db)
        self._seed(tmp_db, pid)
        r = admin_client.get(f'/products/{pid}')
        assert r.status_code == 200
        _, promo_list = self._split_body(r.data.decode('utf-8'))

        # Each seeded promo must appear in the list section
        assert 'render-pct' in promo_list and 'ลด 10' in promo_list
        assert 'render-fixed' in promo_list and 'ราคาตายตัว' in promo_list
        assert 'render-bundle' in promo_list and 'ซื้อ 12 แถม 1' in promo_list
        assert 'render-gift' in promo_list and 'ดจ.สแตนเลส' in promo_list
        assert 'render-mixed-ยกลัง' in promo_list and 'ต้องซื้อยกลัง' in promo_list

    def test_active_promo_badge_renders_most_recent(self, admin_client, tmp_db):
        """Active-promo badge (info-card row) must show ONLY the most-recent
        promo (here: render-mixed-ยกลัง, inserted last)."""
        pid = _first_active_product_id(tmp_db)
        self._seed(tmp_db, pid)
        r = admin_client.get(f'/products/{pid}')
        assert r.status_code == 200
        info_section, _ = self._split_body(r.data.decode('utf-8'))

        # Active badge MUST show the latest promo (render-mixed-ยกลัง)
        assert 'render-mixed-ยกลัง' in info_section
        # Its value rendering (5% + condition). discount_value is a Python
        # float so renders as "5.0" — substring "ลด 5.0%" is the literal output.
        assert 'ลด 5.0%' in info_section
        assert 'ต้องซื้อยกลัง' in info_section
        # The OTHER promos must NOT leak into the info section
        # (only the active row is shown there, not the full list)
        assert 'render-bundle' not in info_section
        assert 'render-gift' not in info_section

    def test_active_promo_badge_bundle_with_unit(self, admin_client, tmp_db):
        """Bundle-type active promo renders buy/free/unit correctly."""
        pid = _first_active_product_id(tmp_db)
        # Seed ONLY a bundle promo so it becomes active
        conn = sqlite3.connect(tmp_db)
        conn.execute("DELETE FROM promotions WHERE product_id = ?", (pid,))
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, "
            "                        bundle_buy, bundle_free, bundle_unit) "
            "VALUES (?, 'badge-bundle', 'bundle', 24, 3, 'ใบ')", (pid,))
        conn.commit()
        conn.close()
        r = admin_client.get(f'/products/{pid}')
        info_section, _ = self._split_body(r.data.decode('utf-8'))
        assert 'badge-bundle' in info_section
        assert 'ซื้อ 24 แถม 3' in info_section
        assert 'ใบ' in info_section  # unit displayed

    def test_bundle_tiers_json_rendered_as_thai_not_raw(self, admin_client, tmp_db):
        """F1 fix: bundle_tiers_json must be parsed by the from_json filter
        and rendered as readable Thai, not as literal JSON."""
        pid = _first_active_product_id(tmp_db)
        conn = sqlite3.connect(tmp_db)
        conn.execute("DELETE FROM promotions WHERE product_id = ?", (pid,))
        tiers_json = '[{"buy": 12, "free": 1}, {"buy": 24, "free": 3}, {"buy": 50, "free": 10}]'
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, "
            "                        discount_value, bundle_buy, bundle_free, bundle_tiers_json) "
            "VALUES (?, 'tier-json-test', 'mixed', 10, 12, 1, ?)",
            (pid, tiers_json))
        conn.commit()
        conn.close()
        r = admin_client.get(f'/products/{pid}')
        _, promo_list = self._split_body(r.data.decode('utf-8'))
        # Readable Thai rendering present
        assert 'ซื้อ 12 แถม 1' in promo_list
        assert 'ซื้อ 24 แถม 3' in promo_list
        assert 'ซื้อ 50 แถม 10' in promo_list
        # Raw JSON brackets must NOT appear in the rendered text
        # (Jinja auto-escapes &quot;, so the literal `{"buy"` would appear if rendered raw)
        assert '{&quot;buy&quot;' not in promo_list
        assert '{"buy"' not in promo_list

    def test_detail_page_renders_tier_prices(self, admin_client, tmp_db):
        pid = _first_active_product_id(tmp_db)
        self._seed(tmp_db, pid)
        r = admin_client.get(f'/products/{pid}')
        assert r.status_code == 200
        body = r.data.decode('utf-8')
        assert 'ราคาตามขนาดแพ็ค' in body
        assert '1 โหล' in body
        assert '1 ลัง' in body
        assert 'bulk discount' in body
