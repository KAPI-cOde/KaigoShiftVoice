"""3幕の通し（画面なし）。12月のルールの見直し → 返さない案と返す案 → 採る。B8 の文。"""
from shiftvoice.model import Acceptance
from shiftvoice.nextflow import NextFlow

from test_act2 import CALL, STANDBY, act2  # noqa: F401（act2 は試験の下ごしらえ）


def test_act3_flow(act2):
    s = act2
    s.say(STANDBY)
    s.distribute()
    s.accept(s.say(CALL).uid)
    # 小島さんの残り2回（撮影では2幕の後にアプリで操作する。ここでは記録を足して代える）
    st = s.store
    st.save_acceptances(st.acceptances() + [
        Acceptance(id="k2", staff="小島", date="2026-11-14", layer="standby", op_id="op-k2"),
        Acceptance(id="k3", staff="小島", date="2026-11-21", layer="standby", op_id="op-k3")])
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
    assert k["after"] <= k["before"]
    assert fl.adopt("give")["ok"]
    assert st.month(2026, 12).table == b["tables"]["give"]
