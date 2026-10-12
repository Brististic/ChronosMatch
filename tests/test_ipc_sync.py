import os
import pytest
from src.ipc_sync import MmapProcessLock

TEST_LOCK_FILE = "test_chronos_sync.lock"


@pytest.fixture
def clean_lock():
    if os.path.exists(TEST_LOCK_FILE):
        os.remove(TEST_LOCK_FILE)
    lock = MmapProcessLock(TEST_LOCK_FILE, spin_timeout_sec=0.1, create=True)
    yield lock
    lock.close()
    if os.path.exists(TEST_LOCK_FILE):
        os.remove(TEST_LOCK_FILE)


def test_lock_acquire_and_release(clean_lock):
    lock = clean_lock
    assert lock.acquire() is True
    # Locked state verify
    is_locked, pid = lock._read_state()
    assert is_locked == 1
    assert pid == os.getpid()

    lock.release()
    is_locked, pid = lock._read_state()
    assert is_locked == 0
    assert pid == 0


def test_lock_context_manager(clean_lock):
    lock = clean_lock
    with lock:
        is_locked, _ = lock._read_state()
        assert is_locked == 1

    is_locked, _ = lock._read_state()
    assert is_locked == 0


def test_lock_timeout_on_contention():
    if os.path.exists(TEST_LOCK_FILE):
        os.remove(TEST_LOCK_FILE)

    lock1 = MmapProcessLock(TEST_LOCK_FILE, spin_timeout_sec=0.05, create=True)
    assert lock1.acquire() is True

    # Second instance attempting to acquire while locked must timeout
    lock2 = MmapProcessLock(TEST_LOCK_FILE, spin_timeout_sec=0.05, create=False)
    assert lock2.acquire() is False

    lock1.release()
    # Now lock2 can acquire
    assert lock2.acquire() is True
    lock2.release()

    lock1.close()
    lock2.close()
    if os.path.exists(TEST_LOCK_FILE):
        os.remove(TEST_LOCK_FILE)