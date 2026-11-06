"""希望休は原則として守り、どうしても組めない時だけ相談する（作者 2026-11-07）。

- 本人の希望を外す段（段2・段4）でも、希望休（本人が日付を決めて頼んだ休み）は、ほかの本人の希望（曜日の勤務の希望など）
  より後に外す。前は同じ重みで、曜日の希望を残して希望休を外すことがあった
- 希望休を外して作った時は、誰の何日かと、入れると守れなくなるルールを知らせ、本人と相談するよう言う。前は
  「外れたのは、〇〇さん 5日 希望休が1か所」と、ほかの外れと同じ並びで言うだけだった
名前は作り話。API は呼ばない。"""
from __future__ import annotations

from shiftvoice.build import solve_staged
from shiftvoice.model import Facility, Month, Rule, ShiftType, Staff

NOV = Month(2026, 11, [], {})   # 2026年11月1日は日曜


def fac() -> Facility:
    return Facility("x", "x", ["東"], [ShiftType("日", "日勤", "work"), ShiftType("公", "公休", "rest")],
                    {"solve": {"max_deterministic_time": 2}})


STAFF = [Staff(f"職員{i:02d}", "東", ["日"]) for i in range(3)]
COVER = Rule("cov", 1, "毎日の日勤は2人", "coverage", "fixed", {"shifts": ["日"], "scope": "all", "op": "==", "n": 2})


def test_希望休はほかの本人の希望より後に外す():
    """毎日1人だけ休める施設で、11月1日（日）に、職員01さんの希望休と、職員00さんの「日曜は休み」の希望がぶつかる。
    曜日の希望の方を外す（曜日の希望の重みを少し重くしても、希望休が勝つ）"""
    wish = Rule("w1", 1, "職員01さん 1日 休み", "cell", "wish", {"in": ["公"]}, who=["職員01"], when=[1])
    sunday = Rule("sun", 1, "職員00さん 日曜は休み", "weekday", "wish", {"weekdays": ["sun"], "in": ["公"]},
                  who=["職員00"], weight=150000)
    r = solve_staged(fac(), STAFF, [COVER, wish, sunday], NOV)
    assert r.ok and r.result.stage == 2
    assert r.result.table["職員01"][0] == "公"
    assert r.result.table["職員00"][0] == "日"


def test_希望休を外した時は人と日とぶつかったルールを言い_相談をすすめる():
    """2人が同じ5日に希望休。毎日2人の日勤（動かせない）とぶつかり、どちらか1人の希望休しか入らない"""
    a = Rule("w1", 1, "職員01さん 5日 休み", "cell", "wish", {"in": ["公"]}, who=["職員01"], when=[5])
    b = Rule("w2", 1, "職員02さん 5日 休み", "cell", "wish", {"in": ["公"]}, who=["職員02"], when=[5])
    r = solve_staged(fac(), STAFF, [COVER, a, b], NOV)
    assert r.ok and r.result.stage == 2
    lost = [n for n in ("職員01", "職員02") if r.result.table[n][4] != "公"]
    assert len(lost) == 1
    assert "本人と相談" in r.note
    assert f"{lost[0]}さんの5日" in r.note
    assert "「毎日の日勤は2人」" in r.note
    assert "外れたのは" not in r.note   # 希望休は、ほかの外れの並びに重ねて言わない


def test_希望休でない本人の希望は今までどおり外れたのはで言う():
    sunday = Rule("sun", 1, "職員00さん 日曜は休み", "weekday", "wish", {"weekdays": ["sun"], "in": ["公"]}, who=["職員00"])
    sunday2 = Rule("sun2", 1, "職員01さん 日曜は休み", "weekday", "wish", {"weekdays": ["sun"], "in": ["公"]}, who=["職員01"])
    r = solve_staged(fac(), STAFF, [COVER, sunday, sunday2], NOV)
    assert r.ok and r.result.stage == 2
    assert "外れたのは" in r.note and "相談" not in r.note
