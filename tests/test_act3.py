"""3幕の通し（画面なし）。12月のルールの見直し → 返さない案と返す案 → 採る。B8 の文。"""
from shiftvoice.nextflow import NextFlow

from test_act2 import CALL, STANDBY, act2, kojima_rest  # noqa: F401（act2 は試験の下ごしらえ）


def test_act3_flow(act2):
    s = act2
    s.say(STANDBY)
    s.distribute()
    s.accept(s.say(CALL).uid)
    # 小島さんの残り2回（2幕の後に、2幕と同じ［了承をもらって直す］で作る）
    kojima_rest(s)
    st = s.store
    assert sorted(a.date for a in st.acceptances() if a.staff == "小島") == ["2026-11-01", "2026-11-14", "2026-11-22"]
    fl = NextFlow(st, 2026, 11)
    v = fl.review()
    assert v["total"] == 3 and v["head"].startswith("12月の表を作る前に　見直すルール 3件中1件目（残り2件）")
    c0 = v["cards"][0]
    assert c0["label"] == "期限が来た" and "小島" in c0["title"]
    assert c0["lines"] == ["11月29日で終わりました。12月には効きません", "片付けても、11月の表と引き受けの記録は残ります"]
    assert [c["label"] for c in v["cards"][1:]] == ["先月増えた", "先月増えた"]   # 遅番→早番・待機当番
    fl.answer(c0["rule_id"], "archive")
    for c in v["cards"][1:]:
        v = fl.answer(c["rule_id"], "keep")
    assert v["done"] and v["summary"][0].endswith("→ 片付けた")
    b = fl.build()
    assert b["status"] == "ok", b
    print(b["main"], "／", b["line2"], f"{b['seconds']}秒")
    assert b["main"].startswith("11月に3回引き受け → 12月の土日祝の夜勤")
    assert b["sub"] == "夜勤の総数は同じ。土日祝の分をほかの人と配分し直す"
    k = next(p for p in b["people"] if p["name"] == "小島")
    assert k["before"] >= 1 and k["after"] < k["before"]   # 台本の山場（返すと小島さんの土日祝の夜勤が減る）
    assert fl.adopt("give")["ok"]
    assert st.month(2026, 12).table == b["tables"]["give"]


def test_astra7_stale_plan_rejected(act2):
    act2.say(STANDBY)
    fl = NextFlow(act2.store, 2026, 11)
    v = fl.review()
    for c in v["cards"]:
        fl.answer(c["rule_id"], "archive" if c["reason"] == "expired" else "keep")
    assert fl.build()["status"] == "ok"
    fl.undo(v["cards"][1]["rule_id"])      # 案を作った後にルールが変わった
    assert "error" in fl.adopt("give")


def _pairs(table, a, b):
    return sum(1 for row in table.values() for x, y in zip(row, row[1:]) if x == a and y == b)


def test_m8_change_by_saying(act2):
    """M8 先月増えた札を［変える（話す）］で直すと、12月の表に効く。待機当番の札には［変える］を出さない。"""
    s = act2
    s.say(STANDBY)
    fl = NextFlow(s.store, 2026, 11)
    v = fl.review()
    seq = next(c for c in v["cards"] if c["reason"] == "added_last_month" and "早番" in c["title"])
    lay = next(c for c in v["cards"] if c["reason"] == "added_last_month" and c is not seq)
    assert [b[0] for b in seq["buttons"]] == ["keep", "change"]
    assert [b[0] for b in lay["buttons"]] == ["keep"]
    # 読めない一言・別の種類の一言では札を変えない
    assert "error" in fl.change(seq["rule_id"], "よろしくね", s.reader)
    assert "error" in fl.change(seq["rule_id"], "職員09さん、早番が多すぎる。月5回くらいまで".replace("職員09", "菊池"), s.reader)
    assert fl.review()["cards"][fl.review()["cursor"]]["answer"] is None
    for c in v["cards"]:
        if c is not seq:
            fl.answer(c["rule_id"], "archive" if c["reason"] == "expired" else "keep")
    said = "遅番の次の日は、早番も日勤もやめて"
    v = fl.change(seq["rule_id"], said, s.reader)
    assert "error" not in v, v
    assert v["done"] and any(t.endswith("→ 変えた（遅番の次の日に早番・日勤を置かない）") for t in v["summary"])
    led = s.store.ledger()
    dec = led.rules_at(between=("2026-12-01", "2026-12-31"))
    nov = led.rules_at(between=("2026-11-01", "2026-11-30"))
    new = [r for r in dec.values() if r.meta.get("changed_from") == seq["rule_id"]]
    assert len(new) == 1 and new[0].params["next_not_in"] == ["早", "日"] and new[0].meta["said"] == said
    assert seq["rule_id"] not in dec                                  # 元の札は12月には効かない
    assert nov[seq["rule_id"]].params["next_not_in"] == ["早"]        # 11月の札は変わらない（指摘A）
    assert new[0].id not in nov
    b = fl.build()
    assert b["status"] == "ok", b
    # 変える前の12月（今月も使う）は 遅番→日勤 が28件（2026-11-02 に測った）。変えると12月の表から消える
    print("遅→日", _pairs(b["tables"]["base"], "遅", "日"), _pairs(b["tables"]["give"], "遅", "日"))
    # 相談できるルールなので、返す案ではほかとの兼ね合いで1〜2件外れることがある（変える前は28件）
    assert _pairs(b["tables"]["base"], "遅", "日") == 0 and _pairs(b["tables"]["give"], "遅", "日") <= 2
    assert _pairs(b["tables"]["give"], "遅", "早") == 0
    # 戻すと元の中身に戻る
    fl.undo(seq["rule_id"])
    dec = s.store.ledger().rules_at(between=("2026-12-01", "2026-12-31"))
    assert dec[seq["rule_id"]].params["next_not_in"] == ["早"] and new[0].id not in dec
