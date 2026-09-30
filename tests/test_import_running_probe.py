"""`import_running()` (#692): a probe of the Express import flock that takes
nothing an import or another probe needs."""
import fcntl
import os

import pytest


@pytest.fixture
def lock_path(empty_db):
    return os.path.join(os.path.dirname(empty_db), 'express_import.lock')


def test_two_probes_at_once_both_see_no_import(lock_path):
    from blueprints import bsn
    other_probe = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(other_probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
    try:
        assert bsn.import_running() is False
    finally:
        os.close(other_probe)


def test_the_probe_sees_a_running_import_and_leaves_the_lock_free(lock_path):
    from blueprints import bsn
    held = bsn._acquire_import_lock()
    assert held is not None
    try:
        assert bsn.import_running() is True
    finally:
        bsn._release_import_lock(held)

    assert bsn.import_running() is False
    again = bsn._acquire_import_lock()
    assert again is not None
    bsn._release_import_lock(again)
