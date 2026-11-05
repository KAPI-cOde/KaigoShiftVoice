"""ルールを押した時の中身（ruledetail.py）。① 数で言う1行・② 今の表の様子・③ 上限や下限に届いているマス。

作者 2026-11-06「連勤の上限を押すと全部のマスが黄色に変わり、連勤いくつの上限で作っているのか分からない」。
数はどれも、ここで表から数え直した値と比べる（rules.py の数え方を使わずに数える）。API は呼ばない。"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from shiftvoice.model import Facility, Month, Rule, Staff
from shiftvoice.onboard import infer, parse_paste
from shiftvoice.ruledetail import rule_detail
from shiftvoice.server import make_handler

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"
WORK = set("早日遅夜明研")


def _cells(d):
    return {(n, day) for n, day in d["cells"]}


def _runs(row, tail=()):
    """勤務の日が続く並び (長さ, 始めの日, 終わりの日)。日は1から、前の月の末は0以下。"""
    seq = list(tail) + list(row)
    off = len(tail)
    out, start = [], None
    for i, c in enumerate(seq + ["公"]):
        if c in WORK:
            start = i if start is None else start
        elif start is not None:
            out.append((i - start, start - off + 1, i - off))
            start = None
    return [r for r in out if r[2] >= 1]


def test_連勤の上限は5日と_ちょうどの人と_その並びだけ光る(sess):
    m = sess.store.month(2026, 11)
    t = m.table
    want_cells, who, longest = set(), set(), 0
    for n, row in t.items():
        for ln, a, b in _runs(row, m.prev_tail.get(n, [])):
            longest = max(longest, ln)
            if ln >= 5:
                who.add(n)
                want_cells |= {(n, d) for d in range(max(a, 1), b + 1)}
    d = sess.rule_detail("r010")
    assert d["line"] == "連勤は5日まで（明けも勤務の日に数えます）"
    assert d["now"][0] == f"今の表では、いちばん長い連勤は{longest}日"
    assert d["now"][1].startswith(f"上限ちょうどは{len(who)}人")
    assert _cells(d) == want_cells
    assert len(want_cells) < sum(len(r) for r in t.values()) // 4     # 全部のマスは光らない
    assert ("菊池", 1) in want_cells and "前の月の末から続く日も数えています" in d["now"]   # 10月の末から続く5日


def test_夜勤の回数は上限ちょうどと下限ちょうどの人の夜勤だけ(sess):
    t = sess.store.month(2026, 11).table
    can = [s.name for s in sess.store.staff_on(2026, 11) if "夜" in s.can]
    n = {k: t[k].count("夜") for k in can}
    d = sess.rule_detail("r006")
    assert d["line"] == f"夜勤は月3〜5回（夜勤に入れる{len(can)}人）"
    assert d["now"][0] == f"今の表では、最多{max(n.values())}回・最少{min(n.values())}回"
    top = [k for k in can if n[k] == 5]
    low = [k for k in can if n[k] == 3]
    assert f"上限ちょうどは{len(top)}人" in d["now"][1] and f"下限ちょうどは{len(low)}人" in d["now"][2]
    assert _cells(d) == {(k, i + 1) for k in top + low for i, c in enumerate(t[k]) if c == "夜"}
    assert "杉山" not in {c[0] for c in d["cells"]}


def test_早番の人数はユニットごとに数え_早番のマスだけ(sess):
    t = sess.store.month(2026, 11).table
    unit = {s.name: s.unit for s in sess.store.staff()}
    per = {(u, d): sum(1 for k, row in t.items() if unit[k] == u and row[d - 1] == "早")
           for u in ("東", "西") for d in range(1, 31)}
    assert set(per.values()) == {1}
    d = sess.rule_detail("r001")
    assert d["line"] == "早番は東・西それぞれ毎日1人"
    assert d["now"] == ["今の表では、東も西も、30日とも1人ちょうど"]
    assert _cells(d) == {(k, i + 1) for k, row in t.items() for i, c in enumerate(row) if c == "早"}


def test_日勤の人数は下限ちょうどの日だけ光る(sess):
    t = sess.store.month(2026, 11).table
    per = {d: [k for k, row in t.items() if row[d - 1] in ("日", "研")] for d in range(1, 31)}
    two = [d for d, ks in per.items() if len(ks) == 2]
    d = sess.rule_detail("r004")
    assert d["now"][0].startswith(f"今の表では、ちょうど2人の日が{len(two)}日、3人以上の日が{30 - len(two)}日")
    assert _cells(d) == {(k, day) for day in two for k in per[day]}


def test_公休の数は全員9日ちょうど(sess):
    t = sess.store.month(2026, 11).table
    assert {row.count("公") for row in t.values()} == {9}
    d = sess.rule_detail("r005")
    assert d["line"] == "公休は月9日"
    assert d["now"][0] == f"今の表では、{len(t)}人とも決まった数ちょうどで、余りはありません"
    assert len(d["cells"]) == 9 * len(t)


def test_遅番の次の日に早番を置かないは並びの数で言い_守れていれば光らない(sess):
    m = sess.store.month(2026, 11)
    before = sum(1 for row in m.table.values() for a, b in zip(row, row[1:]) if a == "遅" and b == "早")
    assert before > 0                                   # 足す前の表には当てはまる並びがある
    r = sess.say("遅番の次の日に早番はきついから、やめてあげて")
    r = sess.answer(r.uid, "毎月")
    rid = r.rule_ids[0]
    t = sess.store.month(2026, 11).table
    pairs = sum(1 for row in t.values() for a, b in zip(row, row[1:]) if a == "遅" and b == "早")
    d = sess.rule_detail(rid)
    assert d["line"] == "遅番の次の日に早番を置かない"
    assert d["now"][0] == f"今の表で、遅番の次の日が早番の所は{pairs}か所" and pairs == 0
    assert d["cells"] == [] and d["glow"] is None


def test_遅番の並びが残る表では_その並びだけ光る(sess):
    """表を直さずにルールだけ当てた時（ここでは直す前の表で数える）。"""
    from shiftvoice.build import month_rules
    m = sess.store.month(2026, 11)
    rule = Rule(id="x1", ver=1, name="遅番の次の日に早番を置かない", kind="sequence", flex="negotiable",
                params={"if": ["遅"], "next_not_in": ["早"]})
    rules = month_rules(sess.store, 2026, 11, with_premise=False) + [rule]
    pairs = {(k, d) for k, row in m.table.items() for d in range(1, 30) if row[d - 1] == "遅" and row[d] == "早"}
    # 10月の末日が遅番で11月1日が早番の人も数える（表を作る時も月をまたいで見る）。光るのは11月1日だけ
    cross = {k for k, row in m.table.items() if (m.prev_tail.get(k) or ["公"])[-1] == "遅" and row[0] == "早"}
    assert cross == {"岡田"}
    want = {(k, x) for k, d in pairs for x in (d, d + 1)} | {(k, 1) for k in cross}
    d = rule_detail("x1", rules, sess.store.facility(), sess.store.staff_on(2026, 11), m, m.table)
    assert _cells(d) == want
    assert d["now"][0].startswith(f"今の表で、遅番の次の日が早番の所は{len(pairs) + len(cross)}か所")


def test_一人の回数と日のルール(sess):
    r = sess.say("小島さん、29日に試験だから、それまで夜勤3回まで")
    t = sess.store.month(2026, 11).table
    d = sess.rule_detail(r.rule_ids[0])
    k = t["小島"][:29].count("夜")
    assert d["line"] == "小島さんの夜勤は11月1〜29日の累計で3回まで"
    assert d["now"][0].startswith(f"今の表では{k}回")
    r = sess.say("岡田さん、15と16は法事で休み")
    d = sess.rule_detail(r.rule_ids[0])
    assert d["line"] == "岡田さんの15・16日は公休" and _cells(d) == {("岡田", 15), ("岡田", 16)}


def test_当番と知らないルールは光らせない(sess):
    assert sess.rule_detail("無い")["none"] is True


def test_画面はルールを押すと中身を取りに行く():
    h = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html").read_text(encoding="utf-8")
    assert '"/api/rule?id="' in h and "rd.glow" in h
    assert "S.staff.map(function (s) { return s.name; })" not in h.split("function ruleCells")[1][:400]


def test_サーバーの口(sess):
    handler = make_handler(sess, threading.Lock(), True, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/rule?id=r010") as res:
            d = json.loads(res.read())
        assert d["id"] == "r010" and d["line"].startswith("連勤は5日まで") and d["cells"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- 取り込みの施設（こもれびの家の見本）

def _komorebi():
    pr = infer(parse_paste(SAMPLE.read_text(encoding="utf-8")), 2026, 11)
    fac = Facility.from_dict({"id": "k", "name": "こもれびの家（架空）", "units": pr.units,
                              "shift_types": pr.shift_types, "settings": {}})
    staff = [Staff.from_dict(s) for s in pr.staff]
    rules = [Rule.from_dict(r) for r in pr.rules]
    month = Month(year=2026, month=11, holidays=[3, 23], prev_tail={}, table=pr.table)
    by = {r.name: r.id for r in rules}
    return fac, staff, rules, month, pr.table, by


def test_取り込みの施設_夜勤の回数と公休の数():
    fac, staff, rules, month, t, by = _komorebi()
    can = [s.name for s in staff if "夜" in s.can]
    n = {k: t[k].count("夜") for k in can}
    d = rule_detail(by["夜勤の回数（夜勤に入れる人）"], rules, fac, staff, month, t)
    lo, hi = min(v for v in n.values() if v), max(n.values())
    assert d["line"] == f"夜勤は月{lo}〜{hi}回（夜勤に入れる{len(can)}人）"
    assert d["now"][0] == f"今の表では、最多{max(n.values())}回・最少{min(n.values())}回"
    edge = [k for k in can if n[k] in (lo, hi)]
    assert _cells(d) == {(k, i + 1) for k in edge for i, c in enumerate(t[k]) if c == "夜"}
    d = rule_detail(by["公休の数"], rules, fac, staff, month, t)
    assert d["line"] == "公休は月9日" and {row.count("公") for row in t.values()} == {9}
    assert d["now"][0] == f"今の表では、{len(t)}人とも決まった数ちょうどで、余りはありません"


def test_取り込みの施設_連勤と日勤の上限():
    fac, staff, rules, month, t, by = _komorebi()
    rs = {k: _runs(row) for k, row in t.items()}
    longest = max(ln for v in rs.values() for ln, _, _ in v)
    d = rule_detail(by["連勤の上限"], rules, fac, staff, month, t)
    assert d["line"] == f"連勤は{longest}日まで（明けも勤務の日に数えます）"   # 取り込みは貼った月のいちばん長い連勤を上限にする
    assert _cells(d) == {(k, x) for k, v in rs.items() for ln, a, b in v if ln >= longest for x in range(a, b + 1)}
    hi_rule = next(r for r in rules if r.name == "日勤の人数（上限）")
    n = hi_rule.params["n"]
    per = {day: [k for k, row in t.items() if row[day - 1] == "日"] for day in range(1, 31)}
    full = [day for day, ks in per.items() if len(ks) == n]
    d = rule_detail(hi_rule.id, rules, fac, staff, month, t)
    assert d["line"] == f"日勤は毎日{n}人まで"
    assert d["now"][0].startswith(f"今の表では、上限の{n}人ちょうどの日が{len(full)}日")
    assert _cells(d) == {(k, day) for day in full for k in per[day]}


def test_回数の幅を月の日数に合わせて広げた時は_広げた数で言う():
    """取り込みで見つけた夜勤の回数（30日の月に6人が5回ずつ）は、31日の月に上限を6回へ広げて作る（fit_count）。"""
    fac = Facility.from_dict({"id": "t", "name": "試験", "units": ["全体"], "settings": {},
                              "shift_types": [{"symbol": "夜", "name": "夜勤", "kind": "work"},
                                              {"symbol": "公", "name": "公休", "kind": "rest"}]})
    staff = [Staff(name=n, unit="全体", can=["夜"]) for n in ("赤坂", "井上", "内田", "遠藤", "太田", "川口")]
    rule = Rule(id="r1", ver=1, name="夜勤の回数", kind="count", flex="operational",
                params={"shifts": ["夜"], "min": 5, "max": 5, "capable_only": True, "seen": {"total": 30, "days": 30}})
    t = {s.name: (["夜"] * 6 + ["公"] * 25) if i == 0 else (["夜"] * 5 + ["公"] * 26) for i, s in enumerate(staff)}
    month = Month(year=2026, month=12, holidays=[], prev_tail={}, table=t)
    d = rule_detail("r1", [rule], fac, staff, month, t)
    assert d["line"] == "夜勤は月5〜6回（夜勤に入れる6人）"
    assert d["now"][0] == "12月は31日あるので、5回から広げています"
    assert "上限ちょうどは1人（赤坂さん）" in d["now"]
