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
