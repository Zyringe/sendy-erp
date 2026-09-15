"""Bootstrap's `.modal-dialog-scrollable` only scrolls when `.modal-body` is a DIRECT flex
child of `.modal-content`. The IV picker (#ivPickModal) wrapped a `<form>` between them, so
the body grew to its full content height and `.modal-content{overflow:hidden}` cut the
footer off: with enough candidate IVs the บันทึก button sat below the screen and no amount
of scrolling reached it (2026-09-15, /marketplace/settlement "เลือก").

pytest cannot see layout. These pin the structure Bootstrap's CSS needs; the behavioural
proof is a headless-browser run that clicks บันทึก like a person (see the PR).
"""
import glob
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest
from lxml import html as lxml_html

TEMPLATES = os.path.join(os.path.dirname(__file__), '..', 'inventory_app', 'templates')


def _cls(name):
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def _scrollable_templates():
    found = []
    for path in glob.glob(os.path.join(TEMPLATES, '**', '*.html'), recursive=True):
        with open(path, encoding='utf-8') as f:
            if 'modal-dialog-scrollable' in f.read():
                found.append(os.path.relpath(path, TEMPLATES))
    return sorted(found)


SCROLLABLE = _scrollable_templates()


def test_sweep_sees_the_scrollable_modals():
    # Control: an empty sweep would pass every parametrized case below vacuously.
    assert 'marketplace/_iv_picker_modal.html' in SCROLLABLE
    assert len(SCROLLABLE) >= 5


@pytest.mark.parametrize('rel', SCROLLABLE)
def test_scrollable_modal_body_is_a_direct_child_of_modal_content(rel):
    with open(os.path.join(TEMPLATES, rel), encoding='utf-8') as f:
        tree = lxml_html.fromstring(f.read())
    dialogs = tree.xpath(f"//*[{_cls('modal-dialog-scrollable')}]")
    assert dialogs, 'the file names the class, so the parse must find the dialog'
    for dialog in dialogs:
        bodies = dialog.xpath(f".//*[{_cls('modal-body')}]")
        assert bodies, 'a scrollable dialog with no .modal-body'
        for body in bodies:
            parent = body.getparent()
            assert 'modal-content' in (parent.get('class') or '').split(), (
                f"{rel}: .modal-body sits inside <{parent.tag} class={parent.get('class')!r}>, "
                "not directly inside .modal-content — the scrollable modal will not scroll and "
                "the footer gets cut off. Put the class on the wrapper itself "
                '(e.g. <form class="modal-content">).')


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


@pytest.mark.parametrize('url', ['/marketplace/settlement?platform=shopee',
                                 '/marketplace/review?platform=shopee'])
def test_iv_picker_form_is_the_modal_content_and_still_submits(tmp_db, url):
    resp = _client().get(url)
    assert resp.status_code == 200
    doc = lxml_html.fromstring(resp.get_data(as_text=True))
    modals = doc.xpath("//div[@id='ivPickModal']")
    assert len(modals) == 1
    content = modals[0].xpath(f".//*[{_cls('modal-dialog-scrollable')}]/*")
    assert len(content) == 1
    form = content[0]
    assert (form.tag, form.get('id')) == ('form', 'ivPickForm')
    assert 'modal-content' in form.get('class', '').split()
    assert form.get('method') == 'post'
    assert len(form.xpath("./input[@name='csrf_token']")) == 1
    assert len(form.xpath(f"./*[{_cls('modal-body')}]//input[@name='doc_base_manual']")) == 1
    assert len(form.xpath(f"./*[{_cls('modal-footer')}]/button[@type='submit']")) == 1
