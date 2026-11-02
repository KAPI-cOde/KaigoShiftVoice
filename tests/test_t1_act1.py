"""T1 の1幕。台本の一言を順に入れ、聞き返しは遅番と菊池さんの2回だけ、表が最後まで決まりを守る。"""
import json

from shiftvoice.build import month_rules
from shiftvoice.rules import check_table

ACT1 = [
    ("岡田さん、15と16は法事で休み", None),
    ("加藤さん、21日は外の研修", None),
    ("上野さんの研修、13日に変わった", None),
    ("遅番の次の日に早番はきついから、やめてあげて", "毎月"),
    ("菊池さん、早番が多すぎる。月5回くらいまで", "今月だけ"),  # 撮影の12月の見直しを3枚にするため（spec.md 画面8）
    ("田村さん、23日の祝日は日勤だと助かるって", None),
    ("小島さん、29日に試験だから、それまで夜勤3回まで", None),
    ("杉山さん、12月から夜勤もできるって", None),
]


def test_t1_act1(sess):
    asked = []
    log = []
    for text, ans in ACT1:
        r = sess.say(text)
        if r.kind == "ask_period":
            asked.append(text)
            r = sess.answer(r.uid, ans)
        assert r.kind == "applied", (text, r.message)
        assert r.warning == "", (text, r.warning)
        log.append((text[:12], len(r.changes), round(r.seconds, 2)))
    assert asked == [ACT1[3][0], ACT1[4][0]]
    print(json.dumps(log, ensure_ascii=False))
    st = sess.store
    m = st.month(2026, 11)
    t = m.table
    assert t["岡田"][14] == "公" and t["岡田"][15] == "公"
    assert t["加藤"][20] == "研" and t["上野"][12] == "研" and t["上野"][11] != "研"
    assert t["田村"][22] == "日"
    assert t["菊池"].count("早") <= 5
    assert t["小島"][:29].count("夜") <= 3
    assert t["青木"][0] == "日" and t["小島"][0] == "公"   # 2幕の前提（11月1日）
    rules = month_rules(st, 2026, 11, with_premise=False)
    broken = check_table(rules, st.facility(), st.staff_on(2026, 11), m, t)
    hard = [b for b in broken if b["flex"] in ("fixed", "wish", "operational")]
    assert hard == []
    print(f"相談できるルールの外れ {len([b for b in broken if b['flex'] == 'negotiable'])} 件")
    cards = [c for c in sess.state()["cards"] if c["scope"] == "rule" and c["meta"].get("said")]
    assert len(cards) == 2   # 毎月のルールとして増えるのは遅番・小島さんの2件（菊池さんは今月だけ）
