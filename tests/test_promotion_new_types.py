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
        """Insert one of each promo type + 2 tiers for the given product."""
        conn = sqlite3.connect(tmp_db)
        conn.execute("PRAGMA foreign_keys = ON")
        # Clear any existing promos/tiers for this product (test isolation)
        conn.execute("DELETE FROM promotions WHERE product_id = ?", (pid,))
        conn.execute("DELETE FROM product_price_tiers WHERE product_id = ?", (pid,))

        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
            "VALUES (?, 'render-pct', 'percent', 10)", (pid,))
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
            "VALUES (?, 'render-fixed', 'fixed', 75)", (pid,))
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, bundle_buy, bundle_free, bundle_unit) "
            "VALUES (?, 'render-bundle', 'bundle', 12, 1, 'ดอก')", (pid,))
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, gift_desc, gift_qty) "
            "VALUES (?, 'render-gift', 'gift', 'ดจ.สแตนเลส', '20 ดอก')", (pid,))
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, bundle_condition) "
            "VALUES (?, 'render-mixed-ยกลัง', 'mixed', 5, 'ยกลัง')", (pid,))
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price) "
            "VALUES (?, '1 โหล', 230)", (pid,))
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price, note) "
            "VALUES (?, '1 ลัง', 2400, 'bulk discount')", (pid,))
        conn.commit()
        conn.close()

    def test_detail_page_renders_all_promo_types(self, admin_client, tmp_db):
        pid = _first_active_product_id(tmp_db)
        self._seed(tmp_db, pid)
        r = admin_client.get(f'/products/{pid}')
        assert r.status_code == 200
        body = r.data.decode('utf-8')
        # Promo name + type label + value rendering
        assert 'render-pct' in body and 'ลด 10' in body
        assert 'render-fixed' in body and 'ราคาตายตัว' in body
        assert 'render-bundle' in body and 'ซื้อ 12 แถม 1' in body
        assert 'render-gift' in body and 'ดจ.สแตนเลส' in body
        assert 'render-mixed-ยกลัง' in body and 'ต้องซื้อยกลัง' in body

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
