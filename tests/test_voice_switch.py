"""声で起動した後に［自分の施設で始める］や施設の切り替えで別の施設へ移っても、声で直せる（2026-11-05）。

前は、声の入口を起動した施設（ひだまりの郷）の名簿で作り、ほかの施設へ移ると入口を外していた。そのため
自分の施設では has_voice が偽になって［話す］が消え、/api/voice も「声の入口が使えません」と返していた。
今は、モデルは1つを使い回し、文字にするたびに、いま開いている施設の名簿の名前を手がかりとして渡す。
本物のモデルは使わない（偽物の声が、渡された名簿を覚える）。
"""
import json
import shutil
import threading
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
KOMOREBI = {"赤坂", "井上", "内田", "遠藤", "太田", "川口", "木下", "工藤"}   # 見本の施設B（こもれびの家）の名簿
PCM = b"\0" * 64


class FakeVoice:
    """声の文字起こしの代わり。渡された名簿を覚え、名簿の最初の人の一言を返す。"""

    def __init__(self):
        self.names = []    # 文字にした時に渡された名簿（呼ばれた順）

    def transcribe(self, samples, names=None):
        self.names.append(None if names is None else list(names))
        return (f"{names[0]}さんは夜勤なし" if names else "夜勤なし"), 0.01


@pytest.fixture
def srv(built_dir, tmp_path):
    root = tmp_path / "places" / "hidamari"
    shutil.copytree(built_dir, root)
    session = Session(Store(root), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    voice = FakeVoice()
    handler = make_handler(session, threading.Lock(), True, voice)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    box = handler.box
    httpd.flow = lambda: NextFlow(box.session.store, box.session.year, box.session.month)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", voice, root
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None):
    h = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def speak(base, text_only=True):
    """画面の［話す］と同じく声を送る。text_only は［変える（話す）］と同じく文字にするだけ。"""
    h = {"Content-Type": "application/octet-stream", **({"X-Text-Only": "1"} if text_only else {})}
    with urllib.request.urlopen(urllib.request.Request(base + "/api/voice", data=PCM, headers=h)) as r:
        return json.loads(r.read())


def create_komorebi(base):
    body = {"text": SAMPLE.read_text(encoding="utf-8"), "year": 2026, "month": 11,
            "name": "こもれびの家（架空）", "answers": {}, "drop_rules": []}
    out = call(base, "/api/setup/create", body)
    assert out["ok"] and len(out["state"]["staff"]) == 8, out
    return out


def test_自分の施設で始めた後も話すが使え_その施設の名簿で文字にする(srv):
    base, voice, _ = srv
    assert call(base, "/api/state")["has_voice"] is True
    made = create_komorebi(base)                             # ［自分の施設で始める］→ 見本を貼って作る
    assert made["state"]["has_voice"] is True                # 画面はこの値で［話す］を出す（前は偽で消えていた）
    assert call(base, "/api/state")["has_voice"] is True
    out = speak(base)
    assert "error" not in out, out                           # 前は「声の入口が使えません。文字で入れてください」
    assert set(voice.names[-1]) == KOMOREBI                  # 切り替えた施設の名簿だけ（ひだまりの郷の名前を混ぜない）
    assert out["text"].split("さん")[0] in KOMOREBI

    # 文字にするだけでなく、一言として送った時も、切り替えた施設（12月・8人）の表へ入る
    out = speak(base, text_only=False)
    assert set(voice.names[-1]) == KOMOREBI and out["text"].split("さん")[0] in KOMOREBI
    assert out["state"]["month"] == 12 and len(out["state"]["staff"]) == 8


def test_施設を行き来しても開いている施設の名簿で文字にする(srv):
    base, voice, root = srv
    hidamari = [s.name for s in Store(root).staff()]
    out = speak(base)
    assert out["text"] == f"{hidamari[0]}さんは夜勤なし"
    assert voice.names[-1] == hidamari                       # 起動した施設では、その名簿で文字にする
    kid = create_komorebi(base)["facility"]["id"]

    back = call(base, "/api/facilities/open", {"id": "hidamari"})   # ひだまりの郷へ戻る（動画の後の審査員の手順3）
    assert back["state"]["has_voice"] is True
    speak(base)
    assert voice.names[-1] == hidamari                       # 戻った後は、起動した時と同じ名簿

    again = call(base, "/api/facilities/open", {"id": kid})  # もう一度こもれびの家へ
    assert again["state"]["has_voice"] is True
    speak(base)
    assert set(voice.names[-1]) == KOMOREBI


def test_話して足した勤務の名前も手がかりに渡す(srv):
    base, voice, _ = srv
    call(base, "/api/say", {"text": "8時から17時の日8を足して"})
    speak(base)
    assert "日8" in voice.names[-1]                          # 渡さないと「日8」が「非8」になった（2026-11-06 AI のテストプレイ）


def test_一言や答えや戻すの後の返事にも声の印が入り_スペースで続けて話せる(srv):
    """画面は返事の state で S を置き換え、S.has_voice が偽ならスペースで話す入口を閉じる。
    前は /api/state の返事にしか has_voice が無く、一度話す・伝える・答えると、その後スペースで話せなかった（作者 2026-11-07）。"""
    base, _, _ = srv
    said = call(base, "/api/say", {"text": "岡田さん、15と16は法事で休み"})          # 文字で［伝える］
    assert said["state"]["has_voice"] is True
    spoken = speak(base, text_only=False)                                            # 声で一言（スペースを押して離す）
    assert spoken["state"]["has_voice"] is True
    asked = call(base, "/api/say", {"text": "遅番の次の日に早番はきついから、やめてあげて"})
    assert asked["reply"]["kind"] == "ask_period" and asked["state"]["has_voice"] is True
    answered = call(base, "/api/answer", {"uid": asked["reply"]["uid"], "text": "毎月"})   # ［毎月］を押す
    assert answered["state"]["has_voice"] is True
    undone = call(base, "/api/undo", {})                                             # ［ひとつ戻す］
    assert undone["state"]["has_voice"] is True


def test_一言をEnterや伝えるで送ったら入力欄の選択を外し_スペースですぐ話せる():
    """欄が選ばれたままだと pttOff が止め、スペースが欄に空白として入る（作者 2026-11-07）。送る処理の中で blur する。"""
    html = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html").read_text(encoding="utf-8")
    body = html.split('document.getElementById("form").addEventListener("submit"', 1)[1].split("});", 1)[0]
    assert "document.activeElement.blur()" in body
    assert body.index("activeElement.blur()") < body.index("sendAnswer(text, inp)") < body.index('post("say"')
