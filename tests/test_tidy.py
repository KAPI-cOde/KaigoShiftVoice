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
    assert "t8" not in rm and [x["id"] for x in base["idle"]] == ["t8"]   # 夜勤に入れない人の「夜勤なし」は注記だけ
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
        assert v["ai"] == "on" and counts["remove"] == 2 and counts["merge"] >= 3 and counts["conflict"] == 0
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


# ---- 壊す側の読み（Astra）の指摘

def _given_name(st):
    """名簿に「姓 名」の人を足し、名前の文に名だけと名簿にいない人の名前を書いたルールを入れる。"""
    from shiftvoice.model import Staff
    staff = st.staff() + [Staff(name="山本 尾島", unit="東", can=["早", "日", "遅", "夜"])]
    st.save_staff(staff)
    m = st.month(2026, 11)
    m.table["山本 尾島"] = ["公"] * m.days
    st.save_month(m)
    led = st.ledger()
    led.append("add", rule={"id": "t30", "name": "尾島は夜勤なし（菊地の希望）", "kind": "count", "flex": "wish",
                            "params": {"shifts": ["夜"], "max": 0}, "who": ["山本 尾島"], "when": None, "scope": "rule",
                            "meta": {"reason": "菊地の希望"}}, op_id="t-t30", frm="2026-10-01")
    st.save_ledger(led)


def test_名前の文は送らず_名だけや名簿にいない人の名前も送らない(st):
    _given_name(st)
    ai = RecordedTidier({})
    view(st, 2026, 11, ai)
    assert len(ai.sent) == 1
    sent = json.dumps(ai.sent[0].payload, ensure_ascii=False)
    for w in ("尾島", "山本", "菊地", "森田", "青木", "斉藤"):
        assert w not in sent, w
    t30 = next(r for r in ai.sent[0].payload["rules"] if r["id"] == "t30")
    assert "夜" in t30["text"] and "職員" in t30["text"]      # 中身から組み立てた文で送る


def test_送る文に名前の2字以上が残れば送らない(st):
    _given_name(st)
    st.add_shift_type({"symbol": "特", "name": "尾島当番", "kind": "work", "start": "09:00", "hours": 8.0})
    fake = _Fake({"lines": {}, "reasons": {}, "ai": {"remove": [], "merge": [], "conflict": []}})
    v = view(st, 2026, 11, fake)
    assert fake.sent == [] and v["ai"] == "skipped"


def test_AI_の案では動かせない_本人の希望のルールを片付けない(st):
    fake = _Fake({"lines": {}, "reasons": {},
                  "ai": {"remove": [{"id": "r010", "why": "連勤は自然に守られる"}, {"id": "r012", "why": "要らない"}],
                         "merge": [{"keep": "r001", "drop": ["r002"], "why": "同じ人数"}], "conflict": []}})
    v = view(st, 2026, 11, fake)
    assert not [x for p in v["piles"] for x in p["items"] if x["basis"] == "ai"]


def test_今月入れない勤務だけを理由に確かとは言わない(st):
    base = collect(st, 2026, 11)
    assert "t8" not in _ids(base, "remove") | _ids(base, "merge")     # 杉山さんが夜勤に入れるようになると要る
    assert [x["id"] for x in base["idle"]] == ["t8"]                   # 注記には出す
    fake = _Fake({"lines": {}, "reasons": {}, "ai": {"remove": [{"id": "t8", "why": "夜勤に入れないので要らない"}],
                                                     "merge": [], "conflict": []}})
    v = view(st, 2026, 11, fake)
    assert [x["id"] for x in v["idle"]] == ["t8"]
    assert not [x for p in v["piles"] for x in p["items"] if x["basis"] == "ai"]   # AI の案でも片付ける側に入れない


def test_AI_の案で今月だけのルールを残して毎月のルールを片付けない(st):
    led = st.ledger()
    led.append("add", rule={"id": "t21", "name": "今月は夜勤を続けない", "kind": "max_run", "flex": "fixed",
                            "params": {"shifts": ["夜"], "max": 1}, "who": None, "when": None, "scope": "once",
                            "meta": {}}, op_id="t-t21", frm="2026-11-01", until="2026-11-30")
    st.save_ledger(led)
    fake = _Fake({"lines": {}, "reasons": {},
                  "ai": {"remove": [], "merge": [{"keep": "t21", "drop": ["t7"], "why": "同じ"}], "conflict": []}})
    v = view(st, 2026, 11, fake)
    assert not [x for p in v["piles"] for x in p["items"] if x["basis"] == "ai"]


def test_知らない番号や名前の入った説明は捨てる(st):
    base = collect(st, 2026, 11)
    keys = {x["drop"][0]: x["key"] for x in base["found"]["merge"]}
    fake = _Fake({"lines": {"merge": "職員99さんのルールです"},
                  "reasons": {keys["t1"]: "r999 に含まれています", keys["t2"]: "齋藤さんの希望と同じです"},
                  "ai": {"remove": [{"id": "t7", "why": "職員99さんがいないので要らない"}], "merge": [], "conflict": []}})
    v = view(st, 2026, 11, fake)
    merge = next(p for p in v["piles"] if p["pile"] == "merge")
    assert "職員99" not in merge["line"]
    whys = [x["why"] for x in merge["items"]]
    assert not any("r999" in w or "齋藤" in w for w in whys)
    assert not [x for p in v["piles"] for x in p["items"] if x["basis"] == "ai"]


def test_AI_のまとめる案は同じ種類なら計算で確かめる(st):
    """t3（青木さんの夜勤6回まで）は全員の5回より広く、外すと青木さんが5回までに戻る。t9 は明けの翌日は公休に守られる"""
    fake = _Fake({"lines": {}, "reasons": {},
                  "ai": {"remove": [], "merge": [{"keep": "r006", "drop": ["t3"], "why": "r006 で5回までなので守られる"},
                                                 {"keep": "r009", "drop": ["t9"], "why": "r009 で公になる"}],
                         "conflict": []}})
    v = view(st, 2026, 11, fake)
    got = [(x["keep"], x["drop"]) for p in v["piles"] for x in p["items"] if x["basis"] == "ai"]
    assert got == [(["r009"], ["t9"])]
