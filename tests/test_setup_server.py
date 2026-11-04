"""自分の施設で始める（貼る → 読み取る → 作る）と施設の切り替えを、本物の HTTP で確かめる。Claude は呼ばない。"""
import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice.nextflow import NextFlow
from shiftvoice.reader import RecordedReader
from shiftvoice.server import make_handler
from shiftvoice.session import Session
from shiftvoice.store import Store

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"


@pytest.fixture(scope="module")
def srv(built_dir, tmp_path_factory):
    root = tmp_path_factory.mktemp("places") / "hidamari"
    shutil.copytree(built_dir, root)
    session = Session(Store(root), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    handler = make_handler(session, threading.Lock(), True, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    box = handler.box
    httpd.flow = lambda: NextFlow(box.session.store, box.session.year, box.session.month)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}", port, root
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None, headers=None):
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def paste_body(**kw):
    d = {"text": SAMPLE.read_text(encoding="utf-8"), "year": 2026, "month": 11}
    d.update(kw)
    return d


def test_見本とプレビュー(srv):
    base, _, _ = srv
    code, sample = call(base, "/api/setup/sample")
    assert code == 200 and sample["name"] == "こもれびの家（架空）" and (sample["year"], sample["month"]) == (2026, 11)
    code, pv = call(base, "/api/setup/preview", paste_body(text=sample["text"]))
    assert code == 200 and len(pv["staff"]) == 8 and pv["next_month"] == 12
    assert [s["symbol"] for s in pv["shift_types"]][:3] == ["早", "日", "遅"]
    assert pv["rules"] and all(r["id"] and r["name"] and r["evidence"] for r in pv["rules"])
    assert pv["unknown_symbols"] == [] and pv["skipped"] == 1 and pv["blank_as_rest"] > 0


def test_壊れた貼り付けは400で一文(srv):
    base, _, _ = srv
    code, out = call(base, "/api/setup/preview", {"text": "赤坂\t公\t公", "year": 2026, "month": 11})
    assert code == 400 and "error" in out and "\n" not in out["error"]
    code, out = call(base, "/api/setup/preview", paste_body(month="x"))
    assert code == 400 and out["error"]


def test_作る_もう一度は開くだけ_デモへ戻る(srv):
    base, _, demo_root = srv
    code, pv = call(base, "/api/setup/preview", paste_body())
    drop = [pv["rules"][0]["id"]]
    code, out = call(base, "/api/setup/create", paste_body(name="こもれびの家（架空）", answers={}, drop_rules=drop))
    assert code == 200 and out["ok"] and not out["existing"]
    assert isinstance(out["diffs"], list) and out["from_month"] == 11
    assert all({"kind", "text"} <= set(d) <= {"kind", "text", "say", "why"} for d in out["diffs"])   # say＝戻す一言の例、why＝違う理由
    st = out["state"]
    assert (st["year"], st["month"]) == (2026, 12) and len(st["staff"]) == 8
    assert len(st["table"]) == 8 and st["days"] == 31
    assert len([c for c in st["cards"] if c["scope"] == "rule"]) == len(pv["rules"]) - 1
    assert st["absence_sim"] is None and st["has_voice"] is False
    new_root = demo_root.parent / out["facility"]["id"]
    assert (new_root / "facility.json").exists()
    # state も新しい施設を見ている
    code, st2 = call(base, "/api/state")
    assert code == 200 and st2["month"] == 12 and len(st2["staff"]) == 8
    # もう一度作ろうとすると、作り直さずに開く
    mtime = (new_root / "ledger.json").stat().st_mtime_ns
    code, again = call(base, "/api/setup/create", paste_body(name="こもれびの家（架空）", answers={}, drop_rules=[]))
    assert "もう作ってあるので、前に作った施設を開きました" in again["message"] and "今回貼った表は使っていません" in again["message"]
    assert "別の名前にしてください" in again["message"] and again["message"].startswith("『こもれびの家（架空）』")
    assert code == 200 and again["existing"] is True and again["diffs"] is None and (new_root / "ledger.json").stat().st_mtime_ns == mtime
    assert len([c for c in again["state"]["cards"] if c["scope"] == "rule"]) == len(pv["rules"]) - 1
    # 一覧とデモへの切り替え
    code, fl = call(base, "/api/facilities")
    ids = {f["id"]: f for f in fl["facilities"]}
    assert set(ids) == {"hidamari", out["facility"]["id"]} and ids[out["facility"]["id"]]["current"]
    code, back = call(base, "/api/facilities/open", {"id": "hidamari"})
    assert code == 200 and back["state"]["month"] == 11 and len(back["state"]["staff"]) > 8
    assert back["state"]["absence_sim"] is not None
    code, bad = call(base, "/api/facilities/open", {"id": "../etc"})
    assert code == 400


def test_Originが違うPOSTは断る(srv):
    base, port, _ = srv
    code, _ = call(base, "/api/setup/preview", paste_body(), {"Origin": "http://evil.example"})
    assert code == 403
    code, _ = call(base, "/api/facilities/open", {"id": "hidamari"}, {"Origin": "http://evil.example"})
    assert code == 403
    code, _ = call(base, "/api/setup/sample", None, {"Host": "evil.example"})
    assert code == 403
    code, _ = call(base, "/api/facilities", None, {"Host": "evil.example"})
    assert code == 403
    req = urllib.request.Request(base + "/api/setup/preview", data=b"x", headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_翌月の表が作れなければ置き場を残さない(srv, monkeypatch):
    """作りかけの置き場が残ると、同じ名前で作り直した時に「開く」側へ入って開けなくなるため。"""
    import shiftvoice.server as server
    base, _, demo_root = srv
    real = server.create_facility

    def fail(prop, root, **kw):
        c = real(prop, root, **kw)
        c.next_table_made = False
        c.stage_note = "この時間では見つかりませんでした"
        return c

    monkeypatch.setattr(server, "create_facility", fail)
    code, out = call(base, "/api/setup/create", paste_body(name="作れない施設（架空）", answers={}))
    assert code == 400 and "翌月の表が作れませんでした" in out["error"]
    left = [d.name for d in demo_root.parent.iterdir() if d.name not in ("hidamari",)]
    assert not any(n.endswith(".making") for n in left)
    code, fl = call(base, "/api/facilities")
    assert all("作れない" not in f["name"] for f in fl["facilities"])


def test_プレビューは答えを当てて読み方を返す(srv):
    base, _, _ = srv
    text = (Path(__file__).resolve().parent / "fixtures" / "pastes" / "03.tsv").read_text(encoding="utf-8")
    code, pv = call(base, "/api/setup/preview", {"text": text, "year": 2026, "month": 11})
    assert code == 200 and sorted(pv["unknown_symbols"]) == ["A", "B", "C", "N"]
    assert pv["same_choices"][0] == {"symbol": "早", "name": "早番"} and not any(r["name"] == "夜勤の人数" for r in pv["rules"])
    answers = {"A": {"same_as": "早"}, "B": {"same_as": "日"}, "C": {"same_as": "遅"}, "N": {"same_as": "夜"}}
    code, pv2 = call(base, "/api/setup/preview", {"text": text, "year": 2026, "month": 11, "answers": answers})
    assert code == 200 and any(r["name"] == "夜勤の人数" for r in pv2["rules"])
    assert "希→公休" in pv2["reading"] and "N→夜勤" in pv2["reading"]
    code, bad = call(base, "/api/setup/preview", {"text": text, "year": 2026, "month": 11, "answers": {"A": {"same_as": "ぬ"}}})
    assert code == 400 and "知らない勤務" in bad["error"]


def test_問題がある貼り付けは知らせて作らない(srv):
    base, _, root = srv
    text = (Path(__file__).resolve().parent / "fixtures" / "pastes" / "14.tsv").read_text(encoding="utf-8")
    body = {"text": text, "year": 2027, "month": 2, "name": "宿直の家（架空）"}
    code, pv = call(base, "/api/setup/preview", body)
    assert code == 200 and pv["suggestions"] == {"宿": "night"} and len(pv["problems"]) == 1 and "宿" in pv["problems"][0]
    code, bad = call(base, "/api/setup/create", dict(body, answers={}))
    assert code == 400 and bad["problems"] and "宿" in bad["error"]
    code, ok = call(base, "/api/setup/preview", dict(body, answers={"宿": {"name": "宿直", "kind": "work", "next_is_ake": True}}))
    assert code == 200 and ok["problems"] == [] and any(r["name"] == "宿直の翌日は明け" for r in ok["rules"])
    code, fl = call(base, "/api/facilities")
    assert all("宿直" not in f["name"] for f in fl["facilities"])


def test_作れない貼り付けは外せばできるルールを知らせて置き場を残さない(srv):
    base, _, demo_root = srv
    text = (Path(__file__).resolve().parent / "fixtures" / "pastes" / "31.tsv").read_text(encoding="utf-8")
    code, out = call(base, "/api/setup/create", {"text": text, "year": 2026, "month": 11, "name": "作れない家（架空）"})
    assert code == 400 and "外すと作れます" in out["error"] and "連勤の上限" in out["error"]
    assert not any(d.name.endswith(".making") for d in demo_root.parent.iterdir())


def test_プレビューは途中入職の人の知らせと空欄の名前を返す(srv):
    base, _, _ = srv
    from pathlib import Path as P
    text = (P(__file__).parent / "fixtures" / "pastes" / "33.tsv").read_text(encoding="utf-8")
    code, pv = call(base, "/api/setup/preview", paste_body(text=text))
    assert code == 200 and len(pv["rest_notes"]) == 1 and pv["rest_notes"][0].startswith("木下さん 公休")
    assert any(r.startswith("空欄") and "木下さん" in r for r in pv["reading"])
