"""［全部のルールで一から作り直す］。根本のルールを足した後は、一言の狭い直しではなく、全部のルールで白紙から作り直す
（作者 2026-11-07「まずゼロから根本的に回す。だいぶいい感じになったら、少しの変更を加え始める」）。［ひとつ戻す］で戻せる。"""
from shiftvoice.rules import check_table


def _hard(sess):
    """表の上の帯と同じく、撮影用の前提（p-*）を除いて数える（month_rules の既定は前提込み）。"""
    m = sess.store.month(2026, 11)
    rules = __import__("shiftvoice.build", fromlist=["month_rules"]).month_rules(sess.store, 2026, 11, with_premise=False)
    return [b for b in check_table(rules, sess.store.facility(), sess.store.staff(), m, m.table)
            if b["flex"] in ("fixed", "wish", "operational")]


def test_rebuild_makes_a_table_that_keeps_all_rules_and_can_be_undone(sess):
    sess.open()
    m = sess.store.month(2026, 11)
    broken = {n: list(r) for n, r in m.table.items()}
    name = next(iter(broken))
    broken[name] = ["夜"] * len(broken[name])   # ルールを大きく外した表にする
    m.table = broken
    sess.store.save_month(m)
    assert _hard(sess)
    r = sess.rebuild(tries=1)
    assert r.kind == "applied", r.message
    assert "一から作り直しました" in r.message and r.changes
    assert not _hard(sess)
    u = sess.undo()
    assert sess.store.month(2026, 11).table == broken, u.message


def test_rebuild_is_refused_after_distribution(sess):
    sess.open()
    sess.distribute()
    before = sess.store.month(2026, 11).table
    r = sess.rebuild(tries=1)
    assert r.kind == "ask" and "配った後" in r.message
    assert sess.store.month(2026, 11).table == before


def test_public_server_does_not_take_rebuild():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "server.py").read_text(encoding="utf-8")
    assert '"/api/rebuild" and public is None' in src
    pub = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "public.py").read_text(encoding="utf-8")
    assert "/api/rebuild" not in pub


def test_rebuild_reassigns_standby_and_undo_puts_it_back(sess):
    """当番（待機）がある月で作り直すと、新しい表で当番を割り当て直す。前は表だけを変え、出勤になった人が待機に残った
    （Astra 2026-11-07）。［ひとつ戻す］では前の割り当てに戻る。"""
    import copy
    from conftest import REPLIES
    from shiftvoice.reader import RecordedBuilder
    from shiftvoice.standby import eligible
    sess.builder = RecordedBuilder.from_file(REPLIES.parent / "builds.json")
    sess.open()
    r = sess.say("毎月、土日は休みの人から待機1人。急な休みの電話はその人にだけ。引き受けた回数は、翌月も覚えておいて")
    assert r.kind == "applied", r.message
    m = sess.store.month(2026, 11)
    lid, lay = next((k, v) for k, v in m.layers.items() if isinstance(v, dict) and "def" in v)
    was = copy.deepcopy(lay["assign"])
    led = sess.store.ledger()   # 当番の人を、その日に日勤にするルールを足してから作り直す（白紙から作ると当番の日が出勤になる）
    for i, (day, names) in enumerate(list(was.items())[:1]):
        led.append("add", rule={"id": f"t-work{i}", "ver": 1, "name": f"{names[0]}さん {day}日は日勤", "kind": "cell",
                                "flex": "operational", "params": {"in": ["日"]}, "who": [names[0]], "when": [int(day)],
                                "scope": "once", "meta": {}}, op_id=f"t-work{i}", frm="2026-11-01", until="2026-11-30")
    sess.store.save_ledger(led)
    r = sess.rebuild(tries=1)
    assert r.kind == "applied", r.message
    m = sess.store.month(2026, 11)
    for day, names in m.layers[lid]["assign"].items():
        for n in names:
            assert eligible(m.layers[lid]["def"], m, m.table, n, int(day)), (day, n, m.table[n][int(day) - 1])
    sess.undo()
    assert sess.store.month(2026, 11).layers[lid]["assign"] == was


def test_rebuild_message_is_two_sentences_with_before_and_after_counts(sess):
    """本文は結果と、残った外れの1つの2文まで（作者 2026-11-07「こういう理由でできなかったけれど、こうしてもいいですか、
    くらいが人間に判断できる限度」）。外れの全部は extra の broken でたたんで見せる。"""
    import re
    sess.open()
    m = sess.store.month(2026, 11)
    broken = {n: list(r) for n, r in m.table.items()}
    broken[next(iter(broken))] = ["夜"] * len(next(iter(broken.values())))
    m.table = broken
    sess.store.save_month(m)
    was = len({b["rule"] for b in _hard(sess)})
    r = sess.rebuild(tries=1)
    assert r.kind == "applied", r.message
    assert r.message.count("。") == 2 and len(r.message) < 120, r.message
    mm = re.search(r"外れていたルールは(\d+)件から(\d+)件になりました", r.message)
    assert mm and int(mm.group(1)) == was and was > 0, r.message
    assert "ひとつ戻す" in r.message
    assert "変わったマス" not in r.message and "回作って" not in r.message
    left = int(mm.group(2))
    if left:   # 外れが残った時だけ赤い知らせ。無くなった時は見出しの横に黒で出す
        assert r.warning == r.message and not r.extra["note"]
        assert r.extra["broken"]["count"] == left and r.extra["broken"]["items"] and not r.extra["broken"]["rebuild"]
    else:
        assert r.warning == "" and r.extra["note"] == r.message
        assert "broken" not in (r.extra or {})


def test_rebuild_after_readme_phrases_counts_like_the_band_and_keeps_the_table(sess):
    """README の3つの一言（遅番の次の日の早番・遅番の3連続・平均化）を毎月で入れた後に作り直す。返事の「外れていた
    ルールはN件」は表の上の帯の「ルールの外れ」と同じ数で、遅番の次の日の早番は0のまま、早番・遅番の回数の幅も
    押す前より広がらない。"""
    import re
    from test_even_out import EVEN, SCENE, _late_then_early, _spread
    sess.open()
    for text, ans in SCENE + [(EVEN, "毎月")]:
        r = sess.answer(sess.say(text).uid, ans)
        assert r.kind == "applied", (text, r.message)
    staff = sess.store.staff_on(2026, 11)
    band = sum(1 for c in sess.state()["cards"] if c["broken"] and c["flex"] != "negotiable")
    m = sess.store.month(2026, 11)
    wide = {sym: _spread(m.table, staff, sym) for sym in ("早", "遅")}
    assert _late_then_early(m.table) == 0
    r = sess.rebuild()   # 画面の黒いボタンと同じ回し方（種を変えて3回）
    assert r.kind == "applied", r.message
    if band:
        mm = re.search(r"外れていたルールは(\d+)件", r.message)
        assert mm and int(mm.group(1)) == band, (band, r.message)
    else:   # 押す前に外れが無い時は「0件から0件になりました」「なくなり」と言わない
        assert "0件のまま" in r.message and "外れていた" not in r.message and "なくなり" not in r.message, r.message
    m = sess.store.month(2026, 11)
    assert _late_then_early(m.table) == 0
    for sym in ("早", "遅"):
        lo, hi = _spread(m.table, staff, sym)
        assert hi - lo <= wide[sym][1] - wide[sym][0], (sym, wide[sym], (lo, hi))


def test_rebuild_after_readme_phrases_keeps_training_and_rest_so_standby_script_runs(sess):
    """作り直しても、表にある研修（上野さんの12日）と、見本の前提の公休（小島さんの1・14・22日。待機の台本で小島さんが
    当番になる日）は残る。前提の勤務のマスと回数の条件は使わない。直す前は前提を丸ごと外していたので、研修が遅番に、
    小島さんの3日が遅・夜・遅になった（Astra 2026-11-08）。作り直すと青木さんの1日は早番から夜勤に変わりうるので、
    台本の電話（青木さんの1日）が待機の小島さんだけで埋まるかは確かめない。"""
    from test_act2 import STANDBY
    from test_even_out import EVEN, SCENE
    from conftest import REPLIES
    from shiftvoice.reader import RecordedBuilder
    sess.builder = RecordedBuilder.from_file(REPLIES.parent / "builds.json")
    sess.base_date = "2026-11-01"
    sess.open()
    for text, ans in SCENE + [(EVEN, "毎月")]:
        r = sess.answer(sess.say(text).uid, ans)
        assert r.kind == "applied", (text, r.message)
    r = sess.rebuild(tries=1)
    assert r.kind == "applied", r.message
    t = sess.store.month(2026, 11).table
    assert t["上野"][11] == "研"
    assert [t["小島"][d - 1] for d in (1, 14, 22)] == ["公"] * 3
    r = sess.say(STANDBY)
    assert r.kind == "applied", r.message
    assign = sess.state()["layers"]["standby"]["assign"]
    # README の手順4で使うのは1日の電話だけ。14日・22日の決め打ちは昔の台本の終わりの状態を作る時のもので、作り直した後は
    # 前の日の勤務しだいで待機に入れないことがある（休みのマスが残ることは上で見ている）
    assert assign["1"] == ["小島"]


def test_rebuild_does_not_pin_leave_the_app_placed_but_keeps_training(sess):
    """作り直しは、表にある研修は残すが、表の有給は残さない。日を頼まれた有給は帳面のルールにあり、表にあるだけの有給はアプリが
    入れた物なので、作り直しで平等に入れ直す。残すと、有給を4日と0日に偏って入れた表を作り直しても偏りが直らなかった
    （Codex Astra 2026-11-08。2人の小さな表で 4日・0日のまま）。"""
    from shiftvoice.session import rebuild_rules
    sess.open()
    st = sess.store
    m = st.month(2026, 11)
    fac, staff = st.facility(), st.staff_on(2026, 11)
    rules = __import__("shiftvoice.build", fromlist=["month_rules"]).month_rules(st, 2026, 11, with_premise=False)
    name = next(n for n, row in m.table.items() if row.count("日") >= 4)
    days = [d for d, x in enumerate(m.table[name], 1) if x == "日"][:4]
    for d in days:
        m.table[name][d - 1] = "有"
    got = rebuild_rules(fac, staff, rules, m, st.premise(2026, 11))
    carried = [r for r in got if r.meta.get("carry")]
    assert not [r for r in carried if "有" in r.params["in"]], [r.name for r in carried]
    assert [r for r in carried if r.params["in"] == ["研"] and r.who == ["上野"] and r.when == [12]]


def test_rebuild_keeps_leave_in_the_month_pasted_from_excel(sess):
    """Excel から貼った月の有給は、施設で決まっていた予定なので作り直しでも残す。帳面のルールに無いので、残さないと
    勤務の日に変わった（Codex Astra 2026-11-08 の 3×8）。アプリが作った月の有給は、上の試験のとおり残さない。"""
    from dataclasses import replace
    from shiftvoice.session import rebuild_rules
    sess.open()
    st = sess.store
    m = st.month(2026, 11)
    fac, staff = st.facility(), st.staff_on(2026, 11)
    rules = __import__("shiftvoice.build", fromlist=["month_rules"]).month_rules(st, 2026, 11, with_premise=False)
    name = next(n for n, row in m.table.items() if row.count("日") >= 4)
    d = m.table[name].index("日") + 1
    m.table[name][d - 1] = "有"
    pasted = replace(fac, settings={**fac.settings, "pasted_month": [2026, 11]})
    keep = [r for r in rebuild_rules(pasted, staff, rules, m, None) if r.meta.get("carry") and "有" in r.params["in"]]
    assert [(r.who, r.when) for r in keep] == [([name], [d])]
    other = replace(fac, settings={**fac.settings, "pasted_month": [2026, 10]})
    assert not [r for r in rebuild_rules(other, staff, rules, m, None) if r.meta.get("carry") and "有" in r.params["in"]]


def test_作り直しの返事は_Claudeを呼ばないので吹き出しの札をアプリにする(sess):
    """作り直しはアプリの計算だけで、Claude を呼ばない。吹き出しに「Claude」と出ると、Claude が作ったように読める。"""
    from pathlib import Path
    sess.open()
    r = sess.rebuild(tries=1)
    assert r.kind == "applied" and r.extra.get("local") is True
    html = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html").read_text(encoding="utf-8")
    body = html.split("function renderReplyBody(r) {", 1)[1].split("\n}", 1)[0]
    assert 'r.extra && r.extra.local' in body.split('classList.toggle("local"', 1)[1].split(";", 1)[0]


def test_時間のかかる作り直しの待ちには経過の秒を出し_返事で止める():
    """1〜3分の作り直しの間、文だけだと止まったように見えた（12月の表を作る時は秒が出ていた）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html").read_text(encoding="utf-8")
    for start in ("function startRebuild()", "function sendAnswer("):
        body = html.split(start, 1)[1].split("\n}", 1)[0]
        assert "chatWait(" in body and ", true);" in body.split("chatWait(", 1)[1].split("\n", 1)[0], start
    wait = html.split("function chatWait(text, clock) {", 1)[1].split("\n}", 1)[0]
    assert "setInterval" in wait and "stopChatClock()" in wait
    assert "stopChatClock();" in html.split("function renderReplyBody(r) {", 1)[1].split("\n", 2)[1]


def test_見本の施設の作り直しは種の違う回を同時に回し_順番に回した時と同じ表を選ぶ(built_dir, tmp_path, monkeypatch):
    """作り直しのボタンは、見本の11月で3回作って選ぶので、順番だと50秒ほど待った（3つの一言の後）。1人の作業者で決まった
    計算の量だけ探す施設は、同時に回しても各回の表は変わらない。同時に回したこと（直す前は順番）と、選んだ表が順番に回した
    時と同じことを見る。"""
    import concurrent.futures as cf
    import shutil
    from conftest import FIXED_NOW, REPLIES
    from shiftvoice.reader import RecordedReader
    from shiftvoice.session import Session
    from shiftvoice.store import Store

    def fresh(name):
        d = tmp_path / name
        shutil.copytree(built_dir, d)
        s = Session(Store(d), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
        s.open()
        return s

    used = []
    real = cf.ThreadPoolExecutor

    class Counting(real):
        def __init__(self, n, *a, **k):
            used.append(n)
            super().__init__(n, *a, **k)

    monkeypatch.setattr(cf, "ThreadPoolExecutor", Counting)
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    a = fresh("par")
    ra = a.rebuild(tries=3)
    assert ra.kind == "applied" and used == [3], used
    monkeypatch.setattr("os.cpu_count", lambda: 1)   # 1コアの PC では今までどおり順番
    b = fresh("seq")
    rb = b.rebuild(tries=3)
    assert rb.kind == "applied" and used == [3]
    assert a.store.month(2026, 11).table == b.store.month(2026, 11).table
