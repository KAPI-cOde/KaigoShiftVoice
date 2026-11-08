"""起動の時に --base-date で決めた引き受けを数える基準日は、施設を開き直したり切り替えたりしても変わらない。
審査員が --base-date 2026-11-30 で起動し、取り込んだ施設（こもれびの家）からひだまりの郷へ戻ると、11月の引き受けの記録が
今日までの分だけに減っていた（まっさらな取得の確かめ、2026-11-08）。施設と職員は作り話（ひだまりの郷）。"""
import json
import shutil
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from conftest import FIXED_NOW, REPLIES
from shiftvoice.reader import RecordedReader
from shiftvoice.server import make_handler
from shiftvoice.session import Session
from shiftvoice.store import Store


def test_施設を開き直しても_起動の時の基準日で引き受けを数える(built_dir, tmp_path):
    root = tmp_path / "places" / "hidamari"
    shutil.copytree(built_dir, root)
    session = Session(Store(root), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW,
                      base_date="2026-11-30")
    session.open()
    before = session.state()["accepted"]
    assert before, "見本の11月には引き受けの記録がある（基準日が11月30日なら数えられる）"
    handler = make_handler(session, threading.Lock(), True, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/facilities/open",
                                     data=json.dumps({"id": "hidamari"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
    new = handler.box.session
    assert new is not session
    assert new.base_date == "2026-11-30"
    if (new.year, new.month) == (2026, 11):
        assert new.state()["accepted"] == before
