"""detect_express_report must classify EVERY header exactly as it did before.

Card 10 moves the report-type vocabulary out of `import_router` into the
`report_types` registry and turns the detector's if-chain into an ordered walk.
That is a refactor on a money path: the type decides which importer a file goes
to, and `credit_notes_ap` vs `credit_notes_ar` is the difference between
recording a return TO a supplier and a return FROM a customer.

The house rule for a refactor claiming to preserve behaviour is to diff the
OUTPUT, not the logic — so this loads the pre-change `import_router` straight
out of git and runs both detectors over the same corpus.

This is not theoretical. The first draft of the registry gave credit_notes_ap
`title_markers=('ใบลดหนี้',)` with no qualifier, so the ordered walk would have
sent EVERY credit note — customer returns included — to the AP importer. It was
caught by re-reading the original branch, and this test is what stops the next
one silently.

The baseline is `origin/main`. When card 10 merges, that becomes the new
behaviour and this file has done its job: keep it (it re-pins on every later
change to the detector) but expect the diff to be empty forever after.
"""
import importlib.util
import itertools
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_REF = 'origin/main'
MODULE_PATH = 'inventory_app/import_router.py'


@pytest.fixture(scope='module')
def old_detect(tmp_path_factory):
    """`detect_express_report` as it exists on origin/main."""
    try:
        blob = subprocess.run(
            ['git', 'show', f'{BASELINE_REF}:{MODULE_PATH}'],
            cwd=REPO, capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f'cannot read {BASELINE_REF}:{MODULE_PATH} ({exc})')

    path = tmp_path_factory.mktemp('baseline') / 'import_router_baseline.py'
    path.write_bytes(blob)

    spec = importlib.util.spec_from_file_location('import_router_baseline', path)
    mod = importlib.util.module_from_spec(spec)
    # The baseline imports express_registers at module level; pytest.ini already
    # puts inventory_app on the path, so this resolves to the current copy —
    # which is fine, the detector does not touch it.
    sys.modules['import_router_baseline'] = mod
    spec.loader.exec_module(mod)
    return mod.detect_express_report


# Real Express title lines, as printed in the first 8 lines of an export.
TITLES = [
    'รายงานประวัติการขาย',
    'รายงานการขาย',
    'รายงานประวัติการซื้อ',
    'รายงานการซื้อ',
    'รายงานการรับชำระหนี้',
    'รายงานการจ่ายชำระหนี้',
    'รายงานลูกหนี้คงค้าง',
    'รายงานเจ้าหนี้คงค้าง',
    'รายงานใบลดหนี้',
    'รายงานใบลดหนี้ รับคืนสินค้า',
    'รายงานใบลดหนี้ ส่งคืนสินค้า',
    # The one that must STAY unknown — a different layout parse_weekly cannot read.
    'รายงานขายเงินเชื่อ เรียงตามเลขที่',
    'รายงานสินค้าคงเหลือ',
    'รายงานภาษีขาย',
    '',
    'ไม่มีอะไรตรงเลย',
]

# Adversarial pairings: two markers in one header, which is exactly where an
# ordered walk and an if-chain can disagree.
COMBOS = [f'{a}\n{b}' for a, b in itertools.permutations(TITLES[:11], 2)]

HEADERS = [
    f'บริษัท บุญสวัสดิ์ นำชัย จำกัด\n{t}\nณ วันที่ 31/07/2569\n\n'
    for t in TITLES + COMBOS
]


def _write(tmp_path, text, name='report.txt'):
    p = tmp_path / name
    p.write_text(text, encoding='cp874')
    return str(p)


def test_corpus_is_big_enough_to_mean_something():
    """Count first: an empty corpus makes the comparison below vacuously true."""
    assert len(HEADERS) > 100, f'corpus is only {len(HEADERS)} headers'


def test_new_detector_matches_the_baseline_on_every_header(old_detect, tmp_path):
    import import_router

    disagreements = []
    for i, head in enumerate(HEADERS):
        path = _write(tmp_path, head, f'r{i}.txt')
        was = old_detect(path)
        now = import_router.detect_express_report(path)
        if was != now:
            disagreements.append((head.splitlines()[1:3], was, now))

    assert not disagreements, (
        f'{len(disagreements)} of {len(HEADERS)} headers classify differently:\n' +
        '\n'.join(f'  {t}: was {w!r}, now {n!r}' for t, w, n in disagreements[:20]))


def test_the_corpus_actually_exercises_every_type(old_detect, tmp_path):
    """CONTROL. If the corpus only ever produced 'unknown', the comparison
    above would pass while testing nothing. Every live type must appear."""
    seen = set()
    for i, head in enumerate(HEADERS):
        seen.add(old_detect(_write(tmp_path, head, f'c{i}.txt')))

    expected = {'sales', 'purchase', 'payments_in', 'payments_out',
                'credit_notes_ar', 'credit_notes_ap',
                'ar_snapshot', 'ap_snapshot', 'unknown'}
    assert seen == expected, f'corpus never produced: {sorted(expected - seen)}'


def test_credit_note_side_is_decided_by_the_qualifier(tmp_path):
    """The specific bug the first registry draft would have shipped, pinned
    directly rather than only through the differential."""
    import import_router

    ap = _write(tmp_path, 'บริษัท\nรายงานใบลดหนี้ ส่งคืนสินค้า\n\n', 'ap.txt')
    ar = _write(tmp_path, 'บริษัท\nรายงานใบลดหนี้ รับคืนสินค้า\n\n', 'ar.txt')
    bare = _write(tmp_path, 'บริษัท\nรายงานใบลดหนี้\n\n', 'bare.txt')

    assert import_router.detect_express_report(ap) == 'credit_notes_ap'
    assert import_router.detect_express_report(ar) == 'credit_notes_ar'
    # Unqualified falls to the AR side — the original's documented default.
    assert import_router.detect_express_report(bare) == 'credit_notes_ar'
