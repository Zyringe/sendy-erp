"""Reject-flow coverage for /mapping tab 2 staged SKU suggestions."""
import json
import os
import sqlite3

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')


def _seed_user_and_suggestion(db_path, *, code='REJECT001', status='pending'):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (username,password_hash,display_name,role,is_active) "
        "VALUES ('rejector','x','Rejector','manager',1) "
        "ON CONFLICT(username) DO NOTHING"
    )
    uid = conn.execute(
        "SELECT id FROM users WHERE username='rejector'"
    ).fetchone()[0]
    sid = conn.execute(
        "INSERT INTO pending_product_suggestions "
        "(bsn_code,bsn_name,suggested_name,suggested_by_user_id,status) "
        "VALUES (?,?,?,?,?)",
        (code, f'raw {code}', f'new {code}', uid, status),
    ).lastrowid
    conn.commit()
    conn.close()
    return uid, sid


def test_reject_deletes_pending_row_and_writes_recoverable_audit(empty_db):
    """Removing the DELETE or any audit field must make this fail."""
    _uid, sid = _seed_user_and_suggestion(empty_db)
    import models

    models.reject_pending_suggestion(sid, 'rejector', 'ข้อมูลซ้ำ')

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    assert conn.execute(
        "SELECT 1 FROM pending_product_suggestions WHERE id=?", (sid,)
    ).fetchone() is None
    audit = conn.execute(
        "SELECT * FROM audit_log WHERE table_name='pending_product_suggestions' "
        "AND row_id=? AND action='DELETE'", (sid,)
    ).fetchone()
    conn.close()

    assert audit is not None
    payload = json.loads(audit['changed_fields'])
    assert payload['id'] == sid
    assert payload['bsn_code'] == 'REJECT001'
    assert payload['suggested_name'] == 'new REJECT001'
    assert audit['user'] == 'rejector'
    assert audit['change_source'] == 'manual'
    assert audit['change_reason'] == 'ข้อมูลซ้ำ'


@pytest.mark.parametrize('reason', ['', '   ', None, 'x' * 501])
def test_reject_refuses_invalid_reason_without_mutating(empty_db, reason):
    """Dropping trim/required/length validation must make one case fail."""
    _uid, sid = _seed_user_and_suggestion(empty_db)
    import models

    with pytest.raises(models.SuggestionRejectValidationError):
        models.reject_pending_suggestion(sid, 'rejector', reason)

    conn = sqlite3.connect(empty_db)
    assert conn.execute(
        "SELECT status FROM pending_product_suggestions WHERE id=?", (sid,)
    ).fetchone()[0] == 'pending'
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='pending_product_suggestions' "
        "AND row_id=?", (sid,)
    ).fetchone()[0] == 0
    conn.close()


def test_reject_refuses_non_pending_row_without_audit(empty_db):
    """Widening the guarded SELECT beyond pending must make this fail."""
    _uid, sid = _seed_user_and_suggestion(empty_db, status='approved')
    import models

    with pytest.raises(models.SuggestionRejectConflictError):
        models.reject_pending_suggestion(sid, 'rejector', 'ไม่ใช้แล้ว')

    conn = sqlite3.connect(empty_db)
    assert conn.execute(
        "SELECT status FROM pending_product_suggestions WHERE id=?", (sid,)
    ).fetchone()[0] == 'approved'
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='pending_product_suggestions' "
        "AND row_id=?", (sid,)
    ).fetchone()[0] == 0
    conn.close()


class _DeleteMissConnection:
    """Same real connection, but changes status at the DELETE seam once."""
    def __init__(self, conn):
        self._conn = conn
        self.seam_calls = 0

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith('DELETE FROM PENDING_PRODUCT_SUGGESTIONS'):
            self.seam_calls += 1
            self._conn.execute(
                "UPDATE pending_product_suggestions SET status='approved' WHERE id=?",
                (params[0],),
            )
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_reject_delete_rowcount_miss_rolls_back_audit(empty_db, monkeypatch):
    """Removing the DELETE rowcount guard would commit a false audit event."""
    _uid, sid = _seed_user_and_suggestion(empty_db)
    import models

    raw = sqlite3.connect(empty_db)
    raw.row_factory = sqlite3.Row
    wrapped = _DeleteMissConnection(raw)
    monkeypatch.setattr(models.suggestions, 'get_connection', lambda: wrapped)

    with pytest.raises(models.SuggestionRejectConflictError):
        models.reject_pending_suggestion(sid, 'rejector', 'ซ้ำ')

    assert wrapped.seam_calls == 1, 'the DELETE seam never fired'
    conn = sqlite3.connect(empty_db)
    assert conn.execute(
        "SELECT status FROM pending_product_suggestions WHERE id=?", (sid,)
    ).fetchone()[0] == 'pending'
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='pending_product_suggestions' "
        "AND row_id=?", (sid,)
    ).fetchone()[0] == 0
    conn.close()


def _client_for_role(tmp_db, role, *, code):
    _uid, sid = _seed_user_and_suggestion(tmp_db, code=code)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = f'{role}-user'
        sess['display_name'] = role.title()
        sess['role'] = role
    return client, sid


@pytest.mark.parametrize('role', ['admin', 'manager'])
def test_reject_route_allows_admin_and_manager(tmp_db, role):
    client, sid = _client_for_role(tmp_db, role, code=f'ALLOW-{role}')

    response = client.post(
        f'/mapping/suggestions/{sid}/reject', json={'reason': 'ไม่สร้างรายการนี้'}
    )

    assert response.status_code == 200
    assert response.get_json() == {'ok': True}


@pytest.mark.parametrize('role', ['staff', 'shareholder'])
def test_reject_route_returns_json_403_for_unauthorized_roles(tmp_db, role):
    client, sid = _client_for_role(tmp_db, role, code=f'DENY-{role}')

    response = client.post(
        f'/mapping/suggestions/{sid}/reject', json={'reason': 'ไม่สร้าง'}
    )

    assert response.status_code == 403
    assert response.is_json
    assert response.get_json()['ok'] is False


def test_reject_route_maps_validation_and_conflict_statuses(tmp_db):
    client, sid = _client_for_role(tmp_db, 'manager', code='ROUTE-STATUS')

    invalid = client.post(f'/mapping/suggestions/{sid}/reject', json={'reason': '  '})
    assert invalid.status_code == 400
    assert invalid.is_json

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "UPDATE pending_product_suggestions SET status='approved' WHERE id=?", (sid,)
    )
    conn.commit()
    conn.close()
    conflict = client.post(
        f'/mapping/suggestions/{sid}/reject', json={'reason': 'ไม่สร้าง'}
    )
    assert conflict.status_code == 409
    assert conflict.is_json


def test_reject_route_returns_sanitized_json_for_unexpected_error(tmp_db, monkeypatch):
    """Letting an unexpected exception fall through would return HTML in prod."""
    client, sid = _client_for_role(tmp_db, 'manager', code='ROUTE-ERROR')
    import models

    def _fail(*_args, **_kwargs):
        raise sqlite3.OperationalError('secret database detail')

    monkeypatch.setattr(models, 'reject_pending_suggestion', _fail)
    response = client.post(
        f'/mapping/suggestions/{sid}/reject', json={'reason': 'ไม่สร้าง'}
    )

    assert response.status_code == 500
    assert response.is_json
    assert response.get_json() == {
        'ok': False,
        'error': 'เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่',
    }
    assert 'secret database detail' not in response.get_data(as_text=True)


def test_pending_suggestions_tag_bill_evidence(empty_db):
    """Removing any of the three evidence sources must make its case fail."""
    import models
    rows = []
    for code in ('NO-BILL', 'HAS-SALE', 'HAS-PURCHASE', 'HAS-CREDIT'):
        _seed_user_and_suggestion(empty_db, code=code)
    conn = sqlite3.connect(empty_db)
    conn.execute(
        "INSERT INTO sales_transactions (date_iso,doc_no,bsn_code) "
        "VALUES ('2026-08-29','S1','HAS-SALE')"
    )
    conn.execute(
        "INSERT INTO purchase_transactions (date_iso,doc_no,bsn_code) "
        "VALUES ('2026-08-29','P1','HAS-PURCHASE')"
    )
    conn.execute(
        "INSERT INTO credit_note_imports (doc_no,doc_base,date_iso,bsn_code) "
        "VALUES ('SR-TEST','SR-TEST','2026-08-29','HAS-CREDIT')"
    )
    conn.commit()
    conn.close()

    rows = {r['bsn_code']: r['has_bills'] for r in models.get_pending_suggestions()}
    assert rows == {
        'NO-BILL': 0,
        'HAS-SALE': 1,
        'HAS-PURCHASE': 1,
        'HAS-CREDIT': 1,
    }


def test_reject_controls_are_role_gated_and_residue_is_flagged(tmp_db):
    manager, sid = _client_for_role(tmp_db, 'manager', code='UI-RESIDUE')
    manager_html = manager.get('/mapping?tab=suggestions').get_data(as_text=True)
    assert f'id="sug-reject-reason-{sid}"' in manager_html
    assert 'required' in manager_html
    assert 'maxlength="500"' in manager_html
    assert 'ไม่มีบิลเหลือแล้ว' in manager_html

    staff = manager.application.test_client()
    with staff.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'staff-user'
        sess['role'] = 'staff'
    staff_html = staff.get('/mapping?tab=suggestions').get_data(as_text=True)
    assert f'id="sug-reject-reason-{sid}"' not in staff_html
