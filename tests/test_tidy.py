"""ルールの整理の3つの山（tidy.py）。作り話のひだまりの郷の11月に、含まれているルール・名簿にいない人のルール・当てはまる
場面の無いルールを入れて、計算で確かに見つけるか、言い過ぎないか、Claude へ送る文に名前が無いか、片付けた後に戻せるかを
確かめる。録った Claude の返事は tests/fixtures/tidy.json（この作り話の施設だけ）。"""
import json
import shutil
from pathlib import Path

import pytest

from shiftvoice.build import month_rules
from shiftvoice.model import Rule
from shiftvoice.store import Store
from shiftvoice.tidy import RecordedTidier, collect, contained, finish, view

from conftest import SEED

TIDY = Path(__file__).resolve().parent / "fixtures" / "tidy.json"

# id, 名前, 種類, 中身, 人, 強さ
ROWS = [
    ("t1", "東の早番は1人まで", "coverage", {"shifts": ["早"], "scope": "each_unit", "op": "<=", "n": 1, "units": ["東"]},
     None, "negotiable"),                                   # 早番の人数（各ユニットちょうど1人・動かせない）に含まれる
    ("t2", "斉藤さん 夜勤は月5回まで", "count", {"shifts": ["夜"], "max": 5}, ["斉藤"], "operational"),   # 全員の回数と同じ
    ("t3", "青木さん 夜勤は月6回まで", "count", {"shifts": ["夜"], "max": 6}, ["青木"], "operational"),   # 全員の5回より広い
    ("t4", "森田さん 夜勤なし", "count", {"shifts": ["夜"], "max": 0}, ["森田"], "operational"),         # 名簿にいない
    ("t5", "南の早番は1人", "coverage", {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 1, "units": ["南"]},
     None, "fixed"),                                        # 南というユニットは無い
    ("t6", "森田さん・青木さん 遅番は月8回まで", "count", {"shifts": ["遅"], "max": 8}, ["青木"], "operational"),
    # 夜勤の翌日は明け（動かせない）なので夜勤は続かない。種類が違うので計算では言わず、AI が読んで見立てる
    ("t7", "夜勤は続けて1日まで", "max_run", {"shifts": ["夜"], "max": 1}, None, "operational"),
    ("t8", "杉山さん 夜勤なし", "count", {"shifts": ["夜"], "max": 0}, ["杉山"], "operational"),   # 杉山さんは夜勤に入れない人
    # 「明けの翌日は公休」（動かせない）の方が厳しい。数える勤務が違うので計算では言わず、AI が読んで見立てる
    ("t9", "明けの次の日は休みか有給", "sequence", {"if": ["明"], "next_in": ["公", "有"]}, None, "operational"),
]


def make(dst: Path) -> Store:
    shutil.copytree(SEED, dst)
    st = Store(dst)
    led = st.ledger()
    for rid, name, kind, params, who, flex in ROWS:
        led.append("add", rule={"id": rid, "name": name, "kind": kind, "flex": flex, "params": params, "who": who,
                                "when": None, "scope": "rule", "meta": {"said": name}}, op_id=f"t-{rid}", frm="2026-10-01")
    st.save_ledger(led)
    return st


@pytest.fixture
def st(tmp_path):
    return make(tmp_path / "f")


def _ids(base, pile):
    key = "drop" if pile == "merge" else "ids"
    return {i for x in base["found"][pile] for i in x[key]}


def test_含まれている_名簿にいない_場面が無いを計算で見つける(st):
    base = collect(st, 2026, 11)
    rm = {x["ids"][0]: x for x in base["found"]["remove"]}
    assert rm["t4"]["what"] == "absent" and rm["t4"]["who"] == ["森田"]
    assert rm["t5"]["what"] == "no_scene"
    assert rm["t8"]["what"] == "always"           # 夜勤に入れない人の「夜勤なし」は、どう置いても守られる
    mg = {x["drop"][0]: x for x in base["found"]["merge"]}
    assert mg["t1"]["keep"] == ["r001"] and mg["t1"]["what"] == "contained"
    assert mg["t2"]["keep"] == ["r006"]           # 名指しの数が全員の数と同じなら、外しても斉藤さんは同じ5回まで
    assert [(s["id"], s["who"]) for s in base["stale"]] == [("t6", ["森田"])]   # 今の人に当たるので片付けには入れない
    assert base["found"]["conflict"] == []


def test_含まれていない物を含まれていると言わない(st):
    base = collect(st, 2026, 11)
    said = _ids(base, "remove") | _ids(base, "merge")
    assert "t3" not in said      # 名指しの6回は全員の5回より広い。外すと青木さんが5回までに戻る
    assert "t6" not in said      # 名前の文に名簿にいない人がいても、青木さんには当たっている
    assert "t7" not in said and "t9" not in said    # 種類や数える勤務が違う組は、計算では言わない
    assert not {r for r in said if r.startswith("r")}   # 見本のルールはどれも効いている


def _seed():
    s = Store(SEED)
    return s.facility(), s.staff_on(2026, 11), s.month(2026, 11), month_rules(s, 2026, 11, with_premise=False)


def _cov(rid, name, flex, units=None, scope="rule"):
    p = {"shifts": ["日"], "scope": "each_unit", "op": "<=", "n": 2, **({"units": units} if units else {})}
    return Rule(rid, 1, name, "coverage", flex, p, scope=scope, meta={"from": "2026-10-01", "until": None})


def test_弱いルールには含まれていると言わない():
    fac, staff, m, rules = _seed()
    east = _cov("x1", "東の日勤は2人まで", "operational", ["東"])
    every = _cov("x2", "日勤は各ユニット2人まで", "negotiable")
    assert contained(rules + [east, every], fac, staff, m) == []
    east = _cov("x1", "東の日勤は2人まで", "negotiable", ["東"])
    every = _cov("x2", "日勤は各ユニット2人まで", "operational")
    got = contained(rules + [east, every], fac, staff, m)
    assert [(x["drop"], x["keep"]) for x in got] == [("x1", ["x2"])]


def test_今月だけのルールに守られているだけなら言わない():
    fac, staff, m, rules = _seed()
    east = _cov("x1", "東の日勤は2人まで", "negotiable", ["東"])
    once = _cov("x2", "今月は日勤を各ユニット2人まで", "operational", scope="once")
    assert contained(rules + [east, once], fac, staff, m) == []


def test_同じことを決めた2件は片方だけを片付けの候補にする():
    fac, staff, m, rules = _seed()
    a = _cov("x1", "日勤は各ユニット2人まで", "negotiable")
    b = _cov("x2", "日勤はどのユニットも2人まで", "negotiable")
    got = contained(rules + [a, b], fac, staff, m)
    assert len(got) == 1 and {got[0]["drop"]} | set(got[0]["keep"]) == {"x1", "x2"}


def test_Claude_へ送る文に名前が無く_返事の名前は戻して出す(st):
    ai = RecordedTidier.from_file(TIDY)
    v = view(st, 2026, 11, ai)
    assert v["ai"] == "on" and len(ai.sent) == 1
    sent = json.dumps(ai.sent[0].payload, ensure_ascii=False)
    for name in [s.name for s in st.staff()] + ["森田", "東", "西"]:
        assert name not in sent, name
    assert "職員" in sent and "ユニット1" in sent
    piles = {p["pile"]: p for p in v["piles"]}
    t4 = next(x for x in piles["remove"]["items"] if x["ids"] == ["t4"])
    assert "森田さん" in t4["why"] and "職員" not in t4["why"] and t4["basis"] == "sure"
    ai_items = [x for p in v["piles"] for x in p["items"] if x["basis"] == "ai"]
    assert ai_items, "録った返事に AI の見立てが無い（tests/fixtures/tidy.json を録り直す）"
    assert any(x.get("drop") == ["t9"] and x["keep"] == ["r009"] for x in ai_items)
    for p in v["piles"]:
        assert ":" not in p["line"] and "：" not in p["line"]
        for x in p["items"]:
            assert not any(c in x["why"] for c in (":", "：", "札", "決まり"))


class _Fake:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def ask(self, p):
        self.sent.append(p)
        return self.reply


def test_知らない番号_今月だけ_強い方を片付ける案は捨てる(st):
    led = st.ledger()
    led.append("add", rule={"id": "t20", "name": "今月は夜勤を各ユニット1人", "kind": "coverage", "flex": "fixed",
                            "params": {"shifts": ["夜"], "scope": "each_unit", "op": "==", "n": 1}, "who": None,
                            "when": None, "scope": "once", "meta": {}}, op_id="t-t20", frm="2026-11-01", until="2026-11-30")
    st.save_ledger(led)
    fake = _Fake({"lines": {"remove": "職員01さんのルールを見ました：片付けてよい札です"}, "reasons": {"m99": "知らない項目"},
                  "ai": {"remove": [{"id": "r999", "why": "知らない番号"}, {"id": "t1", "why": "計算と重なる"},
                                    {"id": "t20", "why": "今月だけ"}, {"id": "t7", "why": "r007 があるので続かない"},
                                    {"id": "r006", "why": "早番の2連続を避けるがあるので要らない", "by": ["r011"]}],
                         "merge": [{"keep": "t9", "drop": ["r009"], "why": "強い方を片付ける"},
                                   {"keep": "r001", "drop": ["r001"], "why": "同じ物"}],
                         "conflict": [{"ids": ["r001"], "why": "1件だけ"}, {"ids": ["r001", "nope"], "why": "知らない番号"}]}})
    v = view(st, 2026, 11, fake)
    ai_items = [(p["pile"], x) for p in v["piles"] for x in p["items"] if x["basis"] == "ai"]
    assert [(p, x.get("ids")) for p, x in ai_items] == [("remove", ["t7"])]
    t7 = ai_items[0][1]
    assert "「夜勤の翌日は明け」" in t7["why"] and "r007" not in t7["why"]
    line = next(p["line"] for p in v["piles"] if p["pile"] == "remove")
    assert line.startswith("青木さんのルール") and "札" not in line and "：" not in line


def test_公開版の決まりと_送れない時は計算だけ(st, monkeypatch):
    class Boom:
        def ask(self, p):
            raise RuntimeError("つながらない")
    v = view(st, 2026, 11, Boom())
    assert v["ai"] == "error" and v["piles"][0]["items"]
    monkeypatch.setenv("SHIFTVOICE_PUBLIC", "1")
    fake = _Fake({"lines": {}, "reasons": {}, "ai": {"remove": [], "merge": [], "conflict": []}})
    v = view(st, 2026, 11, fake)
    assert v["ai"] == "off" and fake.sent == []


def test_片付けた後に一覧から戻せる(st):
    from shiftvoice.reader import RecordedReader
    from shiftvoice.review import history
    from shiftvoice.session import Session
    from conftest import FIXED_NOW, REPLIES
    s = Session(st, 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    out = s.cleanup_apply(["t1"], [])
    assert "error" not in out and "t1" not in st.ledger().rules_at()
    assert "t1" in {h["rule_id"] for h in history(st.ledger())}
    assert "t1" not in _ids(collect(st, 2026, 11), "merge")
    r = s.restore("t1")
    assert r.kind == "applied" and "t1" in st.ledger().rules_at()
    assert "t1" in _ids(collect(st, 2026, 11), "merge")


def test_手元のサーバーの口(tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from shiftvoice.reader import RecordedReader
    from shiftvoice.server import make_handler
    from shiftvoice.session import Session
    from conftest import FIXED_NOW, REPLIES
    st = make(tmp_path / "f")
    s = Session(st, 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW,
                tidier=RecordedTidier.from_file(TIDY))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, threading.Lock(), True, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/tidy", timeout=60) as res:
            v = json.loads(res.read())
        counts = {p["pile"]: p["count"] for p in v["piles"]}
        assert v["ai"] == "on" and counts["remove"] == 3 and counts["merge"] >= 3 and counts["conflict"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()


from test_public import make_server, new_user, req  # noqa: E402,F401


def test_公開版では_Claude_を呼ばない(make_server):
    base, app, calls = make_server()
    ck, _st = new_user(base)
    code, v, _ = req(base, "/api/tidy", cookie=ck, timeout=60)
    assert code == 200 and v["ai"] == "off" and v["ai_note"]
    assert calls() == 0
