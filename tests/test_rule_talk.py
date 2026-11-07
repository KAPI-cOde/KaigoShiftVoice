"""ルールを見る・確かめる・整理する問いに、チャットの一言で答える（ruleask.py。作者 2026-11-08「ルールの管理なども、
すべてチャットのやりとりを通じてできると認知負荷が低くて助かる」）。

問いは Claude へ送らず、表もルールも未反映の発言も変えない。頼み（表やルールを変える一言）や、頼みと混ざった一言は、
今までどおり Claude へ送る。録った返事に無い一言は ReadError になるので、送ったかどうかは reader.sent で見る。"""
import json
import re
import shutil
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from shiftvoice.ruleask import group_cards, rule_question
from shiftvoice.server import NoKeyReader, make_handler
from shiftvoice.session import Session
from shiftvoice.store import Store

from conftest import FIXED_NOW, REPLIES
from test_answer_first import FakeVoice, post
from test_rule_groups import CARDS, ROSTER
from test_tidy import make

BROKEN_SAID = ["外れているルールは？", "守れていないルールある？", "どのルールが守れてないの", "守れてないルール教えて",
               "今の表で守れていないルールを見せて", "ルールは全部守れてる？", "ルール違反ある？", "外れてるルールありますか"]
TIDY_SAID = ["ルールを整理して", "いらないルールある？", "使われていないルールは？", "ルールの整理をお願いします",
             "まとめられるルールある？", "ルールが多すぎる", "消していいルールはある？", "重なっているルールは？"]
LIST_SAID = ["ルールを見せて", "今のルールは？", "ルールの一覧", "ルール一覧", "ルールを全部教えて", "どんなルールがある？",
             "ルールは何件？", "今あるルールは", "ルールの一覧を見せてください"]
# 頼み（表やルールを変える一言）と、頼みと混ざった一言。今までどおり Claude へ送る
REQUESTS = ["遅番の次の日に早番はやめて", "ルールで夜勤は4回まで", "ルールを整理して、それから岡田さん15日休み",
            "守れていないルールを外して", "いらないルールを消して", "夜勤は4回に戻して", "外れているルールは今月だけゆるめて",
            "全部のルールで一から作り直す"]


def snap(s: Session):
    """表・帳面・未反映の発言（問いの前後で同じであること）。"""
    m = s.store.month(s.year, s.month)
    return (json.dumps(m.table, ensure_ascii=False, sort_keys=True), s.store.ledger().rev,
            sorted(s.store.ledger().rules_at()), s.store.pending(s.year, s.month))


def night_worker(s: Session) -> tuple[str, int]:
    m = s.store.month(s.year, s.month)
    name = next(n for n, row in m.table.items() if row.count("夜") >= 2)
    return name, m.table[name].count("夜")


def add_rule(s: Session, rid: str, name: str, kind: str, params: dict, who=None, flex="operational", scope="rule", when=None):
    led = s.store.ledger()
    led.append("add", rule={"id": rid, "name": name, "kind": kind, "flex": flex, "params": params, "who": who,
                            "when": when, "scope": scope, "meta": {"said": name}}, op_id=f"t-{rid}", frm="2026-11-01")
    s.store.save_ledger(led)


def asked(s: Session, text: str, topic: str):
    """問いを言い、Claude へ送らず、何も変えずに答えたことを確かめて返事を返す。"""
    s.open()
    before = snap(s)
    r = s.say(text)
    assert r.kind == "info", (text, r.kind, r.message)
    assert r.extra["topic"] == topic
    assert s.reader.sent == []                                  # Claude へは送っていない
    assert r.sent is None and r.sent_known is True              # 画面は「Claude へは送っていません」と出せる
    assert snap(s) == before                                    # 表・ルール・未反映の発言はそのまま
    assert ":" not in r.message and "：" not in r.message
    return r


# ---- 外れを聞く

@pytest.mark.parametrize("text", BROKEN_SAID)
def test_外れを聞くと_守れていないルールと場所を1件1行で答える(sess, text):
    sess.open()
    who, n = night_worker(sess)
    add_rule(sess, "x1", f"{who}さん 夜勤なし", "count", {"shifts": ["夜"], "max": 0}, who=[who])
    r = asked(sess, text, "broken")
    assert r.message.startswith("今の表で守れていないルールは1件です")
    assert r.extra["lines"] == [f"「{who}さん 夜勤なし」は、{who}さん{n}回で外れています"]
    assert r.extra["ids"] == ["x1"]
    assert {"label": "ルールのページを開く", "open": "rules"} in r.extra["actions"]


def test_日の人数の外れは_ユニットと日と人数で言い_多い所はほかで束ねる(sess):
    add_rule(sess, "x2", "東の日勤は3人", "coverage", {"shifts": ["日"], "scope": "each_unit", "op": "==", "n": 3, "units": ["東"]},
             scope="once", when=[3, 4, 5, 6, 7], flex="fixed")
    r = asked(sess, "外れているルールは？", "broken")
    line = r.extra["lines"][0]
    assert line.startswith("「東の日勤は3人」は5か所で外れています（東の3日（") and line.endswith("ほか2か所）")


def test_外れが無ければ無いと言い_全部の件数を添える(sess):
    sess.open()
    n = len(sess.state()["cards"])
    r = asked(sess, "守れていないルールある？", "broken")
    assert r.message == f"今の表で守れていないルールはありません（全部で{n}件）"
    assert r.extra["lines"] == []


def test_日付の頼みを足しても_チャットの全部の件数は表の上のルールの件数のまま(sess):
    """表の上の［ルール（N件）］とルールのページは、希望休などの日付の頼みを数えない（表のマスに出る）。チャットの
    「全部でN件」も同じ数え方にする。前は日付の頼みまで数え、すぐ横の［ルール（12件）］と「全部で13件」が並んだ。"""
    sess.open()
    before = {t: re.search(r"全部で(\d+)件", asked(sess, q, t).message)[1] for q, t in [("外れているルールは？", "broken"),
                                                                                 ("ルールを見せて", "list")]}
    add_rule(sess, "w1", "岡田さん 15・16日 休み", "cell", {"shift": "公", "avoid": False}, who=["岡田"], flex="wish",
             scope="once", when=[15, 16])
    assert any(c["kind"] == "cell" for c in sess.state()["cards"])
    r = asked(sess, "外れているルールは？", "broken")
    assert re.search(r"全部で(\d+)件", r.message)[1] == before["broken"], r.message
    r = asked(sess, "ルールを見せて", "list")
    assert re.search(r"全部で(\d+)件", r.message)[1] == before["list"], r.message
    assert "ほかに、日付の頼み（希望休など）が1件あり、表のマスに出ています" in r.extra["lines"]


def test_できれば守るルールの外れは_件数に入れず別の行で言う(sess):
    """右の一覧の赤い「外れ」は相談できる強さを数えない。チャットの件数も同じにし、黙らずに別の行で言う。"""
    add_rule(sess, "x3", "連勤は2日まで", "max_run", {"shifts": "work", "max": 2}, flex="negotiable")
    r = asked(sess, "外れているルールは？", "broken")
    assert r.message.startswith("守るルールは、今の表で全部守れています")
    assert r.extra["lines"] == ["ほかに、できれば守るルールが1件外れています（「連勤は2日まで」）"]


# ---- 整理を頼む

@pytest.mark.parametrize("text", TIDY_SAID)
def test_整理を頼むと_整理の画面と同じ見立てを1件1行で答え_何も片付けない(tmp_path, text):
    s = Session(make(tmp_path / "f"), 2026, 11, _reader(), now=lambda: FIXED_NOW)
    r = asked(s, text, "tidy")
    assert r.message.startswith("整理の候補は4件です。まだ何も片付けていません")
    lines = r.extra["lines"]
    assert "「森田さん 夜勤なし」は、名簿にいない森田さんだけのルールです" in lines
    assert "「南の早番は1人」は、当てはまる人や日がありません" in lines
    assert any(x.startswith("「斉藤さん 夜勤は月5回まで」は、") and "含まれていて" in x for x in lines)
    assert r.extra["actions"] == [{"label": "整理の画面を開く", "open": "tidy"}]
    assert {"t4", "t5", "t2"} <= set(r.extra["ids"])


def test_整理の候補が無ければそう言う(sess):
    r = asked(sess, "ルールを整理して", "tidy")
    assert "片付けやまとめの候補は見つかりませんでした" in r.message and r.extra["lines"] == []
    assert r.extra["actions"] == [{"label": "整理の画面を開く", "open": "tidy"}]


# ---- 一覧を頼む

@pytest.mark.parametrize("text", LIST_SAID)
def test_一覧を頼むと_種類ごとの件数を1行ずつ答える(sess, text):
    sess.open()
    who, _ = night_worker(sess)
    add_rule(sess, "x1", f"{who}さん 夜勤なし", "count", {"shifts": ["夜"], "max": 0}, who=[who])
    add_rule(sess, "x2", "東の日勤は3人", "coverage", {"shifts": ["日"], "scope": "each_unit", "op": "==", "n": 3, "units": ["東"]},
             scope="once", when=[3], flex="fixed")
    n = len(sess.state()["cards"])
    r = asked(sess, text, "list")
    assert r.message.startswith(f"ルールは全部で{n}件です")
    lines = r.extra["lines"]
    assert sum(int(re.search(r"(\d+)件", x)[1]) for x in lines) == n          # 種類ごとの件数を足すと全部になる
    assert any(x.startswith("施設のルール（毎月）") for x in lines)
    assert any(x.startswith("人ごとのルール（毎月）") and f"「{who}さん 夜勤なし」" in x for x in lines)
    assert "今月だけのルール1件「東の日勤は3人」" in lines
    assert "全部はルールのページで見られます" in r.message                    # 名前を出さなかった種類がある
    assert r.extra["actions"] == [{"label": "ルールのページを開く", "open": "rules"}]


def test_一覧の分け方は右の一覧と同じ():
    """右の一覧（index.html の groupCards）の試験と同じカードで、同じ分け方になる（今月だけ・施設の守る/できれば・人ごと）。"""
    g = group_cards(CARDS, ROSTER, 2026, 11)
    ids = lambda cs: [c["id"] for c in cs]
    assert sorted(ids(g["only"])) == sorted(["小島 29日まで夜勤3回", "小島 今月は夜勤0"])
    assert ids(g["fac_must"]) == ["早番の人数", "夜勤の回数", "公休の数（取り込み）"]
    assert ids(g["fac_soft"]) == ["早番の2連続を避ける"]
    assert sorted(g["persons"]) == sorted(["小島", "斉藤", "千葉", "赤坂"])
    assert len(g["fac_must"]) + len(g["fac_soft"]) + len(g["people"]) == 10   # 右の一覧の total と同じ
    assert ids(g["dated"]) == ["岡田 15日 休み"]                             # マスの頼みは表に出る


# ---- 頼みは今までどおり

@pytest.mark.parametrize("text", REQUESTS)
def test_頼みや_頼みと混ざった一言は_今までどおり_Claude_へ送る(sess, text):
    assert rule_question(text) is None
    sess.open()
    r = sess.say(text)
    assert r.kind != "info"
    assert len(sess.reader.sent) == 1 and r.sent is not None


def test_混ざった一言は全文を名前を伏せて送る(sess):
    sess.open()
    r = sess.say("ルールを整理して、それから岡田さん15日休み")
    assert r.kind != "info" and "整理" in r.sent and "岡田" not in r.sent and re.search(r"職員\d{2}", r.sent)


# ---- 入口（鍵なし・HTTP・声）

def test_鍵なしでも_ルールの問いには答える(built_dir, tmp_path):
    d = tmp_path / "h"
    shutil.copytree(built_dir, d)
    s = Session(Store(d), 2026, 11, NoKeyReader(), now=lambda: FIXED_NOW)
    s.open()
    for text, topic in (("外れているルールは？", "broken"), ("ルールを整理して", "tidy"), ("ルールを見せて", "list")):
        r = s.say(text)
        assert r.kind == "info" and r.extra["topic"] == topic and not r.extra.get("no_key")


@pytest.fixture
def srv(sess):
    voice = FakeVoice()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(sess, threading.Lock(), True, voice))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", voice
    httpd.shutdown()
    httpd.server_close()


def test_サーバーの返事に_ボタンにできる物が入る(srv, sess):
    base, voice = srv
    out = post(base, "/api/say", {"text": "ルールを見せて"})
    assert out["reply"]["kind"] == "info" and out["reply"]["uid"] is None
    assert out["reply"]["extra"]["actions"] == [{"label": "ルールのページを開く", "open": "rules"}]
    assert out["reply"]["extra"]["lines"] and "cards" in out["state"]
    voice.text = "ルールを整理して"                                      # 声で言っても同じ答え
    out = post(base, "/api/voice", b"\0" * 64, "application/octet-stream")
    assert out["reply"]["kind"] == "info" and out["reply"]["extra"]["actions"][0]["open"] == "tidy"
    assert sess.reader.sent == []


def _reader():
    from shiftvoice.reader import RecordedReader
    return RecordedReader.from_file(REPLIES)
