"""Unified import entry point: one upload box auto-detects ขาย/ซื้อ vs AR/AP and
routes each to the right handler (diff-confirm for transactions, snapshot-replace
preview for outstanding balances)."""
import glob
import io
import os

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')


_AR_HEADER = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย"',
    '"  รายงานลูกหนี้คงค้างแบบละเอียด"',
    '"รหัสลูกค้า  01ก01   ถึง  Zหน้าร้าน                         วันที่ : 29/05/69"',
]
_AP_HEADER = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย"',
    '"  รายงานเจ้าหนี้คงค้างแบบละเอียด"',
    '"รหัสผู้จำหน่าย  AA01   ถึง  ZZ99                            วันที่ : 29/05/69"',
]


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="cp874")
    return str(p)


def test_detect_kind_ar_ap(tmp_path):
    import app
    assert app._detect_express_kind(_write(tmp_path, 'ar.csv', _AR_HEADER)) == 'ar_snapshot'
    assert app._detect_express_kind(_write(tmp_path, 'ap.csv', _AP_HEADER)) == 'ap_snapshot'


def test_detect_kind_sales_purchase(sample_sales_file, sample_purchase_file):
    import app
    assert app._detect_express_kind(sample_sales_file) == 'sales'
    assert app._detect_express_kind(sample_purchase_file) == 'purchase'


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


def _real_ar_file():
    for pat in ('/Volumes/Zyringe_128/Sendai-Boonsawat/Express/BSN/รายงาน*/ลูกหนี้คงค้าง*.csv',):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def test_ar_file_routes_to_snapshot_preview_then_confirms(admin_client, tmp_db):
    """A real AR outstanding file uploaded to the unified /import-weekly lands on
    the snapshot preview (no write), and confirm runs the snapshot import."""
    ar = _real_ar_file()
    if not ar:
        pytest.skip("AR snapshot file not mounted")

    content = open(ar, 'rb').read()
    resp = admin_client.post(
        '/import-weekly',
        data={'weekly_file': (io.BytesIO(content), 'ลูกหนี้คงค้าง_29.5.69.csv', 'text/csv')},
        content_type='multipart/form-data', follow_redirects=False)
    assert resp.status_code == 200, "AR file should render the snapshot preview"
    assert 'ยอดคงค้าง'.encode() in resp.data, "snapshot preview not rendered"
    with admin_client.session_transaction() as sess:
        pend = sess.get('pending_import')
        assert pend and pend['kind'] == 'ar_snapshot'

    resp2 = admin_client.post('/import-weekly/confirm', data={'action': 'confirm'},
                              follow_redirects=False)
    assert resp2.status_code == 302
    assert '/express/ar' in resp2.headers.get('Location', '')
    with admin_client.session_transaction() as sess:
        flashes = [m for (_c, m) in sess.get('_flashes', [])]
        assert sess.get('pending_import') is None
    assert any('สำเร็จ' in m or 'ลูกหนี้' in m for m in flashes), flashes
