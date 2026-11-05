"""Windows の事務用 PC で止まらないための試験（どの OS でも通る）。"""
import subprocess
import sys

import pytest

from shiftvoice import store
from shiftvoice.server import _take_lock, lock_file, pid_alive


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.fixture
def running():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield p
    finally:
        p.kill()
        p.wait()


def test_pid_alive_dead_and_running(running):
    assert pid_alive(_dead_pid()) is False
    assert pid_alive(running.pid) is True


def test_lock_taken_over_when_dead(tmp_path):
    root = tmp_path / "data"
    lock = lock_file(root)
    lock.write_text(f"{_dead_pid()}\nhttp://127.0.0.1:1/", encoding="utf-8")
    got = _take_lock(root, "http://127.0.0.1:2/")
    assert got == lock
    assert lock.read_text(encoding="utf-8").startswith(f"{__import__('os').getpid()}\n")


def test_lock_refused_when_running(tmp_path, running):
    root = tmp_path / "data"
    lock = lock_file(root)
    lock.write_text(f"{running.pid}\nhttp://127.0.0.1:1/", encoding="utf-8")
    assert _take_lock(root, "http://127.0.0.1:2/") is None


def test_lock_taken_over_when_broken(tmp_path):
    root = tmp_path / "data"
    lock = lock_file(root)
    lock.write_text("これは壊れた印", encoding="utf-8")
    assert _take_lock(root, "http://127.0.0.1:2/") == lock
    lock.write_text("abc\nhttp://x/", encoding="utf-8")
    assert _take_lock(root, "http://127.0.0.1:2/") == lock


def test_atomic_write_retries_permission_error(tmp_path, monkeypatch):
    real = store.os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("共有違反")
        return real(a, b)

    monkeypatch.setattr(store.os, "replace", flaky)
    path = tmp_path / "a.json"
    store.atomic_write_json(path, {"x": 1})
    assert store.read_json(path) == {"x": 1}
    assert calls["n"] == 3


def test_atomic_write_gives_up_and_cleans_tmp(tmp_path, monkeypatch):
    def always(a, b):
        raise PermissionError("共有違反")

    monkeypatch.setattr(store.os, "replace", always)
    with pytest.raises(PermissionError):
        store.atomic_write_json(tmp_path / "a.json", {"x": 1})
    assert list(tmp_path.glob("*.tmp")) == []
