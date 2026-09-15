"""Wiring tests for issue #533 — the marketplace import routes must call
cashbook_payout_mirror.mirror_platform(conn, platform) after a successful
reconcile, and a mirror failure must surface as a warning WITHOUT turning
the import itself into a failure (the mirror is idempotent, so the next
import repairs it).

mirror_platform's own behavior (insert/delete/idempotent/duplicate/manual-
row-survival) is covered by test_cashbook_payout_mirror.py — this file only
proves the two call sites (`/marketplace/balance-import`,
`/marketplace/upload`) actually invoke it at the right point.
"""
import io
import os

import pandas as pd
import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')


def _balance_xlsx():
    """Minimal real Seller Balance workbook accepted by parse_balance —
    an adjustment row (no withdrawal, so reconcile_payouts closes zero
    cycles); good enough to exercise the reconcile -> mirror call, not to
    produce a real payout."""
    columns = ['วันที่', 'ประเภทการทำธุรกรรม', 'คำอธิบาย', 'รหัสคำสั่งซื้อ',
               'จำนวนเงิน', 'ยอดเงินหลังทำธุรกรรมเสร็จสิ้น']
    body = pd.DataFrame([
        ['2099-09-05 09:00', 'รายการปรับปรุง', 'MIRROR-WIRING-TEST', '-', '1.00', '1.00'],
    ], columns=columns)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        pd.DataFrame([['Seller Balance']]).to_excel(
            w, sheet_name='Transaction Report', index=False, header=False)
        body.to_excel(w, sheet_name='Transaction Report', index=False, startrow=1)
    buf.seek(0)
    return buf


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _post_balance_import(client):
    return client.post(
        '/marketplace/balance-import',
        data={'balance_file': (_balance_xlsx(), 'balance.xlsx')},
        content_type='multipart/form-data', follow_redirects=True)


def _post_upload(client):
    return client.post(
        '/marketplace/upload',
        data={'files': [(_balance_xlsx(), 'my_balance_transaction_report.xlsx')]},
        content_type='multipart/form-data', follow_redirects=True)


# ── /marketplace/balance-import ─────────────────────────────────────────────

def test_balance_import_calls_mirror_after_reconcile(tmp_db, monkeypatch):
    import blueprints.marketplace as marketplace_bp

    calls = []

    def fake_mirror(conn, platform):
        calls.append(platform)
        return {'inserted': 0, 'deleted': 0, 'unchanged': 0}

    monkeypatch.setattr(marketplace_bp.cashbook_payout_mirror, 'mirror_platform', fake_mirror)
    resp = _post_balance_import(_client())
    assert resp.status_code == 200
    assert calls == ['shopee']


def test_balance_import_mirror_failure_warns_but_import_still_succeeds(tmp_db, monkeypatch):
    import blueprints.marketplace as marketplace_bp

    def raising_mirror(conn, platform):
        raise marketplace_bp.cashbook_payout_mirror.CashbookPayoutMirrorError(
            "ไม่พบบัญชี SPX (หรือถูกปิดใช้งาน) — ยอดโอนของ Shopee ยังไม่ถูกบันทึกลงบัญชีรับ-จ่าย"
        )

    monkeypatch.setattr(marketplace_bp.cashbook_payout_mirror, 'mirror_platform', raising_mirror)
    resp = _post_balance_import(_client())
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'นำเข้า Balance สำเร็จ' in html, "a mirror failure must not fail the import itself"
    assert 'บันทึกยอดโอนลงบัญชีรับ-จ่ายไม่สำเร็จ' in html
    assert 'ไม่พบบัญชี SPX' in html


def test_balance_import_skipped_conflicts_surfaces_a_visible_warning(tmp_db, monkeypatch):
    """Deploy-then-import ordering hazard (review finding): the mirror
    itself defers rather than double-books when an un-linked manual row
    already matches a payout, but that deferral must not be silent — Put
    needs to know to run the conversion script."""
    import blueprints.marketplace as marketplace_bp

    def conflicted_mirror(conn, platform):
        return {'inserted': 3, 'deleted': 0, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 2}

    monkeypatch.setattr(marketplace_bp.cashbook_payout_mirror, 'mirror_platform', conflicted_mirror)
    resp = _post_balance_import(_client())
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'นำเข้า Balance สำเร็จ' in html, "a deferral must not fail the import itself"
    assert 'ยังไม่ลงบัญชีรับ-จ่าย' in html
    assert 'convert_legacy_cashbook_payout_rows.py' in html


# ── /marketplace/upload ──────────────────────────────────────────────────────

def test_upload_calls_mirror_after_reconcile(tmp_db, monkeypatch):
    import blueprints.marketplace as marketplace_bp

    calls = []

    def fake_mirror(conn, platform):
        calls.append(platform)
        return {'inserted': 0, 'deleted': 0, 'unchanged': 0}

    monkeypatch.setattr(marketplace_bp.cashbook_payout_mirror, 'mirror_platform', fake_mirror)
    resp = _post_upload(_client())
    assert resp.status_code == 200
    assert calls == ['shopee']


def test_upload_mirror_failure_warns_but_batch_still_reports_success(tmp_db, monkeypatch):
    import blueprints.marketplace as marketplace_bp

    def raising_mirror(conn, platform):
        raise RuntimeError("db locked (test)")

    monkeypatch.setattr(marketplace_bp.cashbook_payout_mirror, 'mirror_platform', raising_mirror)
    resp = _post_upload(_client())
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'alert-success' in html, "the file DID import — mirror failure must not hide that"
    assert 'บันทึกยอดโอนลงบัญชีรับ-จ่ายไม่สำเร็จ' in html
    assert 'db locked (test)' in html
