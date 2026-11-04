"""公休移動（2026-11-04 作者と同僚の答え）。

休みの電話で待機の人が公休の日に入る時、その人の公休は減らさず、電話の日より後の自分の勤務の日へ移す。
動かすのは休む人と待機の人の2人だけ。移す日の候補は、負担の少ない順に3つまで出し、1つ目を「おすすめ」にする。
今月の中に移せる日が無い時だけ、今までの形（公休が1日減る）にして、そう言う。
"""
import copy
import dataclasses

from shiftvoice.build import month_rules
from shiftvoice.resolve import changed_cells
from shiftvoice.rules import check_table
from shiftvoice.standby import ASK_WIDER, fix_absence

from test_act2 import CALL, LATER_CALLS, STANDBY, act2  # noqa: F401（act2 は試験の下ごしらえ）

NO_MOVE = "今月の中で休みを移せる日がありませんでした。公休は8日になります"


def rest(t, n):
    return t[n].count("公")


def called(s):
    """待機当番を足して配った後の表（休みの電話の前）。"""
    s.say(STANDBY)
    s.distribute()
    return copy.deepcopy(s.store.month(2026, 11).table)


def offer_of(s, uid):
    return next(p for p in s.store.pending(2026, 11) if p["id"] == uid)["offer"]


def cells(changes):
    return sorted((c["staff"], c["day"], c["before"], c["after"]) for c in changes)


def _short(fac, a, b):
    """次の勤務までの間が14時間未満の並び（勤務の種類の始まりと長さから。試験の側で別に数える）。"""
    sa, sb = fac.shift(a), fac.shift(b)
    if sa.start is None or sa.hours is None or sb.start is None:
        return False
    h = lambda x: int(x[:2]) + int(x[3:]) / 60   # noqa: E731
    return 24 + h(sb.start) - (h(sa.start) + sa.hours) < 14


def burden(fac, m, t, n):
    """（短い並びの数, いちばん長い連勤）。前の月の末から続けて数え、連勤はこの月にかかる物だけ。"""
    tail = m.prev_tail.get(n, [])
    row = tail[-1:] + t[n]
    short = sum(_short(fac, a, b) for a, b in zip(row, row[1:]))
    work = set(fac.workday_symbols())
    best = run = 0
    for i, c in enumerate(tail + t[n]):
        run = run + 1 if c in work else 0
        if i >= len(tail):
            best = max(best, run)
    return short, best


def test_move_keeps_rest_days(act2):
    """小島さんの公休は9日のまま。休みは11月1日より後の、小島さんの勤務の日へ移る。青木さんも9日のまま（4マス）。"""
    s = act2
    t0 = called(s)
    r = s.say(CALL)
    assert r.kind == "standby_offer" and r.extra["duty"] == "小島", r.message
    mv = r.extra["moves"]
    assert mv and r.extra["pick"] == 0 and mv[0]["label"] == "おすすめ"
    d = mv[0]["day"]
    assert 1 < d <= 30 and t0["小島"][d - 1] != "公"
    assert r.summary == f"小島さん 公休9日のまま（休みは{d}日へ移す）"
    assert r.message == "その日の待機の小島さんに頼む案です（4マス）"
    assert len(r.changes) == 4 and {c["staff"] for c in r.changes} == {"青木", "小島"}
    a = s.accept(r.uid)
    assert a.kind == "applied" and a.summary == "11月の引き受け 0回 → 1回"
    t = s.store.month(2026, 11).table
    assert t["青木"][0] == "公" and t["小島"][0] == "日" and t["小島"][d - 1] == "公"
    assert rest(t, "小島") == rest(t0, "小島") == 9 and rest(t, "青木") == rest(t0, "青木") == 9
    assert s.store.month(2026, 11).rest_adjust == {}
    rec = [x for x in s.store.acceptances() if x.staff == "小島"]
    assert len(rec) == 1 and rec[0].date == "2026-11-01" and rec[0].moved_to == f"2026-11-{d:02d}"
    # 動かせないルール・本人の希望・運用の都合の外れは増えない
    st = s.store
    rules = month_rules(st, 2026, 11, with_premise=False)
    m = st.month(2026, 11)

    def hard(tb):
        return {(b["rule"], b["key"]) for b in check_table(rules, st.facility(), st.staff_on(2026, 11), m, tb)
                if b["flex"] != "negotiable"}
    assert hard(t) <= hard(t0)


def test_moves_in_order_of_burden(act2):
    """候補は、相談できるルールの外れを増やさない → 休む人の公休も元のまま → 変わるマスと関わる人が少ない →
    関わる人の短い並び（次の勤務までの間が14時間未満）を増やさない → 連勤を延ばさない → 電話の日に近い、の順。3つまで。"""
    s = act2
    t0 = called(s)
    r = s.say(CALL)
    o = offer_of(s, r.uid)
    st = s.store
    fac, m0 = st.facility(), st.month(2026, 11)
    rules = month_rules(st, 2026, 11, with_premise=False)
    staff = st.staff_on(2026, 11)
    neg0 = {(b["rule"], b["key"]) for b in check_table(rules, fac, staff, m0, t0) if b["flex"] == "negotiable"}
    mv = o["moves"]
    assert 1 <= len(mv) <= 3 and len(r.extra["moves"]) == len(mv)
    keys = []
    for x, shown in zip(mv, r.extra["moves"]):
        t = x["table"]
        assert cells(changed_cells(t0, t)) == cells(x["changes"]) == cells(shown["changes"])
        names = sorted({c["staff"] for c in x["changes"]})
        assert names == ["小島", "青木"]                  # 動かすのは休む人と待機の人だけ
        assert rest(t, "小島") == 9 and x["day"] > 1 and t["小島"][x["day"] - 1] == "公"
        m = dataclasses.replace(m0, support=x["support"], rest_adjust=x["rest_adjust"])
        br = check_table(rules, fac, staff, m, t)
        assert not [b for b in br if b["flex"] != "negotiable"
                    and b not in check_table(rules, fac, staff, m0, t0)], x["line"]
        neg = len({(b["rule"], b["key"]) for b in br if b["flex"] == "negotiable"} - neg0)
        b0 = {n: burden(fac, m0, t0, n) for n in names}
        b1 = {n: burden(fac, m0, t, n) for n in names}
        short = sum(max(0, b1[n][0] - b0[n][0]) for n in names)
        run = sum(max(0, b1[n][1] - b0[n][1]) for n in names)
        keys.append((neg, rest(t, "青木") != rest(t0, "青木"), len(x["changes"]), len(names), short, run, x["day"] - 1))
        # 1行の説明は、移す日・2人・マスの数・短い並び
        assert shown["line"].startswith(f"{x['day']}日に小島さんが休み"), shown["line"]
        assert f"（{len(x['changes'])}マス）" in shown["line"]
        assert ("短い並びは増えない" in shown["line"]) == (short == 0), shown["line"]
    print("候補", [(x["day"], x["line"]) for x in r.extra["moves"]], keys)
    assert keys == sorted(keys), keys
    assert [x["label"] for x in r.extra["moves"]] == ["おすすめ"] + [""] * (len(mv) - 1)
    assert len({x["day"] for x in mv}) == len(mv)   # 同じ日を2回出さない


def test_pick_another_day(act2):
    """［休みを移す日を変える］で2つ目を選んで了承すると、その案で直る。"""
    s = act2
    t0 = called(s)
    r = s.say(CALL)
    mv = r.extra["moves"]
    assert len(mv) >= 2, mv
    assert s.accept(r.uid, pick=9).kind == "error"          # 無い候補は選べない（表は変えない）
    assert s.store.month(2026, 11).table == t0
    a = s.accept(r.uid, pick=1)
    assert a.kind == "applied" and f"休みは{mv[1]['day']}日へ" in a.message
    t = s.store.month(2026, 11).table
    assert cells(changed_cells(t0, t)) == cells(mv[1]["changes"])
    assert rest(t, "小島") == 9 and t["小島"][mv[1]["day"] - 1] == "公"
    rec = [x for x in s.store.acceptances() if x.staff == "小島"]
    assert len(rec) == 1 and rec[0].moved_to == f"2026-11-{mv[1]['day']:02d}"


def test_undo_restores_all(act2):
    """［ひとつ戻す］で、4マス・公休の足し引き・応援・引き受けの記録が全部元に戻る。"""
    s = act2
    t0 = called(s)
    m0 = s.store.month(2026, 11)
    ra0, sp0 = dict(m0.rest_adjust), dict(m0.support)
    acc0 = [a.to_dict() for a in s.store.acceptances()]
    r = s.say(CALL)
    d = r.extra["moves"][0]["day"]
    s.accept(r.uid)
    m1 = s.store.month(2026, 11)
    assert m1.table["小島"][d - 1] == "公" and len(changed_cells(t0, m1.table)) == 4
    assert m1.support.get("小島/1") == "東"     # 小島さんは1日だけ東へ応援
    assert len(s.store.acceptances()) == len(acc0) + 1
    u = s.undo()
    assert u.kind == "applied"
    assert sorted((c["staff"], c["day"]) for c in u.changes) == sorted((c["staff"], c["day"]) for c in r.changes)
    m2 = s.store.month(2026, 11)
    assert m2.table == t0 and m2.rest_adjust == ra0 and m2.support == sp0
    assert [a.to_dict() for a in s.store.acceptances()] == acc0
    assert s.state()["accepted"].get("小島", 0) == 0


def test_refused_changes_nothing(act2):
    """［断られた］は今と同じ。表・公休・応援・記録を変えず、案Eの聞き返しへ。案Eでも頼まれた人の公休は移す。"""
    s = act2
    t0 = called(s)
    m0 = s.store.month(2026, 11)
    n = len(s.store.acceptances())
    r = s.say(CALL)
    x = s.refuse(r.uid)
    assert x.kind == "ask" and x.message == ASK_WIDER and x.extra.get("wider")
    m = s.store.month(2026, 11)
    assert m.table == t0 and m.rest_adjust == m0.rest_adjust and m.support == m0.support
    assert len(s.store.acceptances()) == n
    assert s.accept(r.uid).kind == "error"
    o = s.wider(r.uid)
    assert o.kind == "standby_offer" and o.extra["moves"], o.message
    h = o.extra["duty"]
    assert o.summary == f"{h}さん 公休9日のまま（休みは{o.extra['moves'][0]['day']}日へ移す）"
    s.accept(r.uid)
    t = s.store.month(2026, 11).table
    assert rest(t, h) == rest(t0, h) == 9 and t["青木"][0] == "公" and t[h][0] != "公"


def test_no_day_to_move_says_so(act2, monkeypatch):
    """今月の中に移せる日が無い時は、今までの形（公休が1日減る）にし、そう言う。"""
    import shiftvoice.standby as sb
    s = act2
    t0 = called(s)
    monkeypatch.setattr(sb, "rest_moves", lambda *a, **k: [])
    r = s.say(CALL)
    assert r.kind == "standby_offer" and r.extra["moves"] == [] and r.extra["no_move"] == NO_MOVE
    assert r.summary == "公休　青木さん 9日 → 10日　小島さん 9日 → 8日"
    assert len(r.changes) == 2
    a = s.accept(r.uid)
    assert a.kind == "applied"
    t = s.store.month(2026, 11).table
    assert rest(t, "小島") == 8 and rest(t0, "小島") == 9
    assert s.store.month(2026, 11).rest_adjust == {"青木": 1, "小島": -1}
    assert [x.moved_to for x in s.store.acceptances() if x.staff == "小島"] == [None]


def test_no_day_after_month_end(act2):
    """月の末日（30日）の電話は、電話の日より後に移せる日が無い。fix_absence は今までの形を返す。"""
    s = act2
    t0 = called(s)
    st = s.store
    m = st.month(2026, 11)
    rules = month_rules(st, 2026, 11, with_premise=False)
    fac, staff = st.facility(), st.staff_on(2026, 11)
    req = next(n for n in t0 if t0[n][29] in ("日", "早", "遅"))
    for h in [n for n in t0 if t0[n][29] == "公" and t0[n][28] not in ("夜", "明")]:
        r = fix_absence(fac, staff, rules, m, t0, req, 30, [h])
        if r["status"] == "ok":
            break
    assert r["status"] == "ok" and r["moves"] == [] and r["moved_to"] is None
    assert r["rest"][h] == (9, 8) and r["rest"][req] == (9, 10)


def test_three_calls_kojima_moves(act2):
    """撮影の流れ。3回とも小島さんが了承して引き受けは3件。移す日は、後の電話の日（14日・22日）とも、
    前提の固定（撮影用の11月の元の表の決め打ち）ともぶつからない。"""
    s = act2
    t0 = called(s)
    out = []
    for text, day in [(CALL, 1)] + list(zip(LATER_CALLS, (14, 22))):
        r = s.say(text)
        assert r.kind == "standby_offer" and r.extra["duty"] == "小島", (text, r.message)
        assert r.extra["moves"], text
        mv = r.extra["moves"][0]
        out.append((day, mv["day"], len(r.changes), mv["line"]))
        assert s.accept(r.uid).kind == "applied"
    print("3回の電話", out)
    t = s.store.month(2026, 11).table
    rec = sorted((a.date, a.moved_to) for a in s.store.acceptances() if a.staff == "小島")
    assert [d for d, _ in rec] == ["2026-11-01", "2026-11-14", "2026-11-22"]
    moved = [int(x[-2:]) for _, x in rec]
    assert not set(moved) & {1, 14, 22} and all(t["小島"][d - 1] == "公" for d in moved)
    assert rest(t, "小島") == 9
    pins = s.store.premise(2026, 11)["pins"]
    calls = {("青木", 1), ("小島", 1), ("小島", 14), ("小島", 22)}
    for p in pins:
        if (p["who"], p["day"]) not in calls:
            assert t[p["who"]][p["day"] - 1] == t0[p["who"]][p["day"] - 1], p


def test_move_then_accept(act2):
    """［休みを移す日を変える］で2つ目を選ぶと、表は変えずに案がその候補になる（光るマスも1行の文も変わる）。
    そのまま［了承をもらって直す］を押すと、その候補で直り、記録の行に移した日が出る。"""
    s = act2
    t0 = called(s)
    r = s.say(CALL)
    mv = r.extra["moves"]
    assert len(mv) >= 2, mv
    assert all("table" not in x for x in mv)                     # 画面へは表の中身を送らない
    x = s.move(r.uid, 1)
    assert x.kind == "standby_offer" and x.extra["pick"] == 1
    assert cells(x.changes) == cells(mv[1]["changes"])
    assert x.summary == mv[1]["summary"] == f"小島さん 公休9日のまま（休みは{mv[1]['day']}日へ移す）"
    assert x.message == mv[1]["message"]
    assert s.store.month(2026, 11).table == t0                   # 選んだだけでは表を変えない
    o = offer_of(s, r.uid)
    assert o["pick"] == 1 and o["moved_to"] == mv[1]["day"]       # 開き直しても選んだ候補のまま
    assert s.move(r.uid, 7).kind == "error" and offer_of(s, r.uid)["pick"] == 1
    assert s.move(r.uid, "x").kind == "error"
    a = s.accept(r.uid)
    assert a.kind == "applied" and f"休みは{mv[1]['day']}日へ" in a.message
    t = s.store.month(2026, 11).table
    assert cells(changed_cells(t0, t)) == cells(mv[1]["changes"])
    assert a.extra["records"] == [f"11/1 引き受け（休みは{mv[1]['day']}日へ）"]
    assert s.move(r.uid, 0).kind == "error"                      # 直した後の案は選び直せない


def test_card_parts(act2):
    """案の下の1行（parts）は、休む人がどうなるかから言う。負担が増える区切りだけ warn。"""
    s = act2
    called(s)
    r = s.say(CALL)
    for x in r.extra["moves"]:
        texts = [p["text"] for p in x["parts"]]
        assert texts and not texts[0].startswith(f"{x['day']}日に"), texts
        assert any(t.startswith("青木さんは代わりに") or t.startswith("青木さんの公休は") for t in texts), texts
        assert x["warn"] == any(p["warn"] for p in x["parts"])
        assert all(not p["warn"] for p in x["parts"] if p["text"] == "短い並びは増えない")


def test_move_over_http(act2):
    """本物の HTTP で、休みの電話 → /api/move で候補を選ぶ → /api/accept（pick つき）で直る。"""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from shiftvoice.server import make_handler
    s = act2
    t0 = called(s)
    handler = make_handler(s, threading.Lock(), True, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read())
    try:
        r = post("/api/say", {"text": CALL})["reply"]
        assert r["kind"] == "standby_offer" and len(r["extra"]["moves"]) >= 2
        m = post("/api/move", {"uid": r["uid"], "pick": 1})
        assert m["reply"]["kind"] == "standby_offer" and m["reply"]["extra"]["pick"] == 1
        assert m["state"]["table"] == t0
        p = next(x for x in m["state"]["pending"] if x["id"] == r["uid"])
        assert p["offer"]["pick"] == 1
        a = post("/api/accept", {"uid": r["uid"], "pick": 1})
        d = r["extra"]["moves"][1]["day"]
        assert a["reply"]["kind"] == "applied" and a["reply"]["extra"]["records"] == [f"11/1 引き受け（休みは{d}日へ）"]
        assert a["state"]["table"]["小島"][d - 1] == "公" and a["state"]["table"]["小島"][0] == "日"
    finally:
        httpd.shutdown()
        httpd.server_close()
