"""全部の希望休を入れると組めない時の、いちばんの詰まり所（choke.chokepoints。作者 2026-11-07）。
作り話の2ユニット10人の施設（tests/fixtures/pastes/excel_nights_no_ake.tsv を取り込んだ12月。U1 は5人で、早番・遅番・夜勤を
毎日1人ずつ）で、12日（土）に U1 の3人が希望休を出すと、残る2人で3つの勤務を埋められず組めない。"""
from __future__ import annotations

from pathlib import Path

import pytest

from shiftvoice.build import impossible_wishes, month_rules, solve_staged
from shiftvoice.choke import chokepoints, explain
from shiftvoice.model import Rule
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.store import Store

PASTE = (Path(__file__).parent / "fixtures" / "pastes" / "excel_nights_no_ake.tsv").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    out = tmp_path_factory.mktemp("choke") / "f"
    create_facility(infer(parse_paste(PASTE), 2026, 11, {}), out, name="試験", next_year=2026, next_month=12)
    return out


def _wishes(staff, n=3, day=12):
    u1 = [s.name for s in staff if s.unit == "U1"][:n]
    return [Rule(f"w{i}", 1, f"{x}さん {day}日 休み", "cell", "wish", {"in": ["公"]}, who=[x], when=[day], scope="once")
            for i, x in enumerate(u1)], u1


def test_重なった希望休の日をいちばんの詰まり所として言う(made):
    st = Store(made)
    fac, staff, m = st.facility(), st.staff_on(2026, 12), st.month(2026, 12)
    add, who = _wishes(staff)
    rules, _ = impossible_wishes(fac, month_rules(st, 2026, 12, with_premise=False) + add, m)
    c = chokepoints(fac, staff, rules, m)
    assert c and c[0]["day"] == 12
    assert sorted(w for w, d in c[0]["wishes"]) == sorted(who) and all(d == 12 for _, d in c[0]["wishes"])
    assert sorted(c[0]["cells"]) == sorted([w, 12] for w in who)
    assert any("U1" in r for r in c[0]["rules"])
    t = explain(c, m)[0]
    assert "いちばんの詰まり所は12日（土）" in t and all(f"{w}さん" in t for w in who)
    assert "早出" in t and t.endswith("希望休を本人と相談します")
    assert not any(x in t for x in ("：", ":", "札", "道具"))


def test_組める月には何も言わない(made):
    """希望休が2人なら組める。月全体の数の合わなさ（この12月は公休9日で勤務が余る）は、日ごとの詰まりとしては言わない"""
    st = Store(made)
    fac, staff, m = st.facility(), st.staff_on(2026, 12), st.month(2026, 12)
    add, _ = _wishes(staff, n=2)
    rules, _ = impossible_wishes(fac, month_rules(st, 2026, 12, with_premise=False) + add, m)
    assert chokepoints(fac, staff, rules, m) == []


def test_一から作る時だけ詰まり所を探す(made):
    st = Store(made)
    fac, staff, m = st.facility(), st.staff_on(2026, 12), st.month(2026, 12)
    add, _ = _wishes(staff)
    rules = month_rules(st, 2026, 12, with_premise=False) + add
    r = solve_staged(fac, staff, rules, m, explain=True)
    assert r.choke and r.choke[0]["day"] == 12
    assert solve_staged(fac, staff, rules, m).choke == []   # 一言の直しなど（explain を渡さない）は探さない


def test_希望休を入れて作り直す画面に詰まり所の文とマスを渡す(made, tmp_path):
    import shutil
    from shiftvoice.nextflow import NextFlow
    dst = tmp_path / "f"
    shutil.copytree(made, dst)
    store = Store(dst)
    staff = store.staff_on(2026, 12)
    led = store.ledger()
    add, who = _wishes(staff)
    for i, r in enumerate(add):
        led.append("add", rule={**r.to_dict(), "id": f"r9{i:02d}"}, frm="2026-12-01", until="2026-12-31")
    store.save_ledger(led)
    out = NextFlow(store, 2026, 11).build()
    assert out["choke"] and out["choke"][0]["day"] == 12
    assert sorted(out["choke"][0]["cells"]) == sorted(f"{w}|12" for w in who)
    assert "いちばんの詰まり所は12日（土）" in out["choke"][0]["text"]
