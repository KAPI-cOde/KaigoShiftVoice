"""GitHub Codespaces の中で動く時だけ、その Codespace の転送先の名前1つを Host と Origin で受ける。
ほかの時と、ほかの名前は、今までどおり断る（DNS の付け替えと、ほかのサイトからの送信を止める守りは弱めない）。"""
import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice.reader import RecordedReader
from shiftvoice.server import codespaces_host, make_handler
from shiftvoice.session import Session
from shiftvoice.store import Store


@pytest.fixture
def served(built_dir, tmp_path):
    root = tmp_path / "hidamari"
    shutil.copytree(built_dir, root)
    session = Session(Store(root), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, threading.Lock(), True, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def codespace(monkeypatch, name="demo-abc"):
    monkeypatch.setenv("CODESPACES", "true")
    monkeypatch.setenv("CODESPACE_NAME", name)
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")


def test_転送先の名前は_Codespaces_の中だけで作る(monkeypatch):
    monkeypatch.delenv("CODESPACES", raising=False)
    assert codespaces_host(8765) is None
    codespace(monkeypatch)
    assert codespaces_host(8765) == "demo-abc-8765.app.github.dev"
    monkeypatch.setenv("CODESPACE_NAME", "")
    assert codespaces_host(8765) is None


def test_Codespaces_の中ではその転送先の名前を受ける(served, monkeypatch):
    base, port = served
    codespace(monkeypatch)
    host = f"demo-abc-{port}.app.github.dev"
    assert call(base + "/api/state", headers={"Host": host}) == 200
    post = {"Host": host, "Origin": f"https://{host}", "Content-Type": "application/json"}
    assert call(base + "/api/undo", "POST", post, {}) != 403
    # ほかの名前・ほかの番号・http の Origin は断る
    assert call(base + "/api/state", headers={"Host": f"other-{port}.app.github.dev"}) == 403
    assert call(base + "/api/state", headers={"Host": f"demo-abc-{port + 1}.app.github.dev"}) == 403
    assert call(base + "/api/undo", "POST", {**post, "Origin": "https://evil.example"}, {}) == 403
    assert call(base + "/api/undo", "POST", {**post, "Origin": f"http://{host}"}, {}) == 403


def test_Codespaces_の外では今までどおり断る(served, monkeypatch):
    base, port = served
    monkeypatch.delenv("CODESPACES", raising=False)
    assert call(base + "/api/state", headers={"Host": f"demo-abc-{port}.app.github.dev"}) == 403
    assert call(base + "/api/state") == 200
