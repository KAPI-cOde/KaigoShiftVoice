"""全部のルールで作れなかった日の、理由1つと穴を埋める提案1つ（作者 2026-11-07「こういう理由でできなかったけれど、
こうしてもいいですか」「この人に早番を頼むなら解決する？」）。
作り話の2ユニット10人の施設（tests/fixtures/pastes/excel_nights_no_ake.tsv を取り込んだ12月。U1 は5人で、早番・遅番・夜勤を
毎日1人ずつ）。12日に U1 の2人が希望休を出し、もう1人が「早番と遅番と夜勤は入れない」で抜けると、残る2人で3つの勤務を
埋められず組めない。1人の応援か、入れない人のルールをその日だけ外せば組める。U1 の3人が希望休だと、1人では埋まらない。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from shiftvoice.build import impossible_wishes, month_rules
from shiftvoice.choke import hole_offers
from shiftvoice.model import Rule
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.rules import Term, check_table, expand
from shiftvoice.session import Session
from shiftvoice.store import Store

PASTE = (Path(__file__).parent / "fixtures" / "pastes" / "excel_nights_no_ake.tsv").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    out = tmp_path_factory.mktemp("hole") / "f"
    create_facility(infer(parse_paste(PASTE), 2026, 11, {}), out, name="試験", next_year=2026, next_month=12)
    return out


def _wishes(staff, n=3, day=12):
    u1 = [s.name for s in staff if s.unit == "U1"][:n]
    return [Rule(f"w{i}", 1, f"{x}さん {day}日 休み", "cell", "wish", {"in": ["公"]}, who=[x], when=[day], scope="once")
            for i, x in enumerate(u1)], u1


def test_その日だけ外したマスは回数にも並びにも数えない(made):
    st = Store(made)
    fac, staff, m = st.facility(), st.staff_on(2026, 12), st.month(2026, 12)
    who = staff[0].name
    ban = Rule("b1", 1, f"{who}さん 早番は入れない", "count", "operational", {"shifts": ["早"], "max": 0}, who=[who],
               scope="once", meta={"except_cells": [f"{who}/2026-12-03/早"]})
    [inst] = expand(ban, fac, staff, m)
    assert not any(t.day == 3 for t in inst.terms) and any(t.day == 4 for t in inst.terms)
    seq = Rule("s1", 1, "遅番の次の日に早番を置かない", "sequence", "operational", {"if": ["遅"], "next_not_in": ["早"]},
               meta={"except_cells": [f"{who}/2026-12-03/早"]})
    keys = {i.key for i in expand(seq, fac, staff, m)}
    assert f"{who}/2" not in keys      # 2日の遅番から3日の早番は認める
    assert f"{who}/3" in keys          # 3日の遅番から4日への並びは守る（3日の項は遅番で、外した早番ではない）
    assert f"{staff[1].name}/2" in keys   # ほかの人は変わらない
    other_month = Rule("b2", 1, "x", "count", "operational", {"shifts": ["早"], "max": 0}, who=[who], scope="once",
                       meta={"except_cells": [f"{who}/2027-01-03/早"]})
    [inst2] = expand(other_month, fac, staff, m)
    assert any(t.day == 3 for t in inst2.terms)   # 別の月の日付は、この月の3日を外さない


def _ban(staff):
    third = [s.name for s in staff if s.unit == "U1"][2]
    return Rule("b1", 1, f"{third}さん 早番と遅番と夜勤は入れない", "count", "operational",
                {"shifts": ["早", "遅", "夜"], "max": 0}, who=[third], scope="once"), third


def test_穴を埋める候補に希望休の人を入れず外さずに済む応援を先に出し外す案にはルールを持たせる(made):
    st = Store(made)
    fac, staff, m = st.facility(), st.staff_on(2026, 12), st.month(2026, 12)
    add, who = _wishes(staff, n=2)
    ban, third = _ban(staff)
    rules, _ = impossible_wishes(fac, month_rules(st, 2026, 12, with_premise=False) + add + [ban], m)
    offs = hole_offers(fac, staff, rules, m, 12, limit=10)
    assert offs and not any(o["staff"] in who for o in offs)
    top = offs[0]
    assert top["except"] == [] and top["unit"] == "U1" and top["hole_unit"] == "U1"
    assert {s.unit for s in staff if s.name == top["staff"]} == {"U2"}
    mine = [o for o in offs if o["staff"] == third]
    assert mine and [r.id for r, _i in mine[0]["except"]] == ["b1"] and mine[0]["unit"] is None


def test_1人では埋まらない日には案を出さない(made):
    """U1 の3人が希望休。U2 から1人が応援に入ると、U2 の次の日の窓が組めなくなる（真ん中の窓だけで確かめていた時は、
    この案を出して、受けた後も同じ日が詰まっていた）"""
    st = Store(made)
    fac, staff, m = st.facility(), st.staff_on(2026, 12), st.month(2026, 12)
    add, _ = _wishes(staff, n=3)
    rules, _ = impossible_wishes(fac, month_rules(st, 2026, 12, with_premise=False) + add, m)
    assert hole_offers(fac, staff, rules, m, 12) == []


def _copy(made, tmp_path, n, ban=True):
    dst = tmp_path / "f"
    shutil.copytree(made, dst)
    store = Store(dst)
    led = store.ledger()
    staff = store.staff_on(2026, 12)
    add, who = _wishes(staff, n=n)
    if ban:
        add.append(_ban(staff)[0])
    for i, r in enumerate(add):
        led.append("add", rule={**r.to_dict(), "id": f"r9{i:02d}"}, frm="2026-12-01", until="2026-12-31")
    store.save_ledger(led)
    return store, who


@pytest.fixture()
def blocked(made, tmp_path, monkeypatch):
    """12日に U1 の2人が希望休、もう1人が入れない施設の写し（帳面に入れる）"""
    monkeypatch.setenv("SHIFTVOICE_PATIENCE", "0")
    return _copy(made, tmp_path, 2)


def _snap(store):
    m = store.month(2026, 12)
    return (json.dumps(m.table), dict(m.support), sorted(a.id for a in store.acceptances()), len(m.history),
            json.dumps({k: v.to_dict() for k, v in store.ledger().rules_at().items()}, sort_keys=True))


def test_作り直しで作れない日を理由1つと提案1つで言い受けると引き受けを記録し戻すと全部戻る(blocked):
    store, who = blocked
    r = Session(store, 2026, 12, None).rebuild(tries=1)
    assert r.kind == "applied"
    h = r.extra["hole"]
    assert h["message"].startswith("12日（土）は、") and "希望休が重なり" in h["message"]
    assert "に入れる人がいません。" in h["message"] and "頼めますか" in h["message"]
    assert h["message"].endswith("引き受けた分は翌月に返します")
    assert h["message"] in r.message and h["message"] in r.warning
    assert len(h["others"]) <= 2
    assert not any(x in h["message"] for x in ("：", ":", "札", "道具"))
    after_rebuild = _snap(store)
    offer = next(x for x in store.pending(2026, 12) if x["id"] == h["uid"])["offer"]["hole"][0]

    r2 = Session(store, 2026, 12, None).accept(h["uid"])
    assert r2.kind == "applied" and r2.message.startswith(f"{offer['staff']}さんの引き受けとして記録し")
    m = store.month(2026, 12)
    assert m.table[offer["staff"]][11] == offer["symbol"]
    if offer["unit"]:
        assert m.support[f"{offer['staff']}/12"] == offer["unit"]
    acc = [a for a in store.acceptances() if a.id.startswith("a-hole-")]
    assert [(a.staff, a.date) for a in acc] == [(offer["staff"], "2026-12-12")]
    assert all(m.table[w][11] == "公" for w in who)   # 希望休は守ったまま
    added = [x for x in store.ledger().rules_at().values() if x.meta.get("source") == "穴を埋める提案"]
    assert len(added) == 1 and added[0].scope == "once" and added[0].meta["until"] == "2026-12-31"

    Session(store, 2026, 12, None).undo()
    assert _snap(store) == after_rebuild


def test_外す案を受けるとそのルールはその1マスだけ外れと数えない(blocked):
    store, _ = blocked
    r = Session(store, 2026, 12, None).rebuild(tries=1)
    uid = r.extra["hole"]["uid"]
    items = next(x for x in store.pending(2026, 12) if x["id"] == uid)["offer"]["hole"]
    pick = next((i for i, x in enumerate(items) if x["except"]), None)
    if pick is None:
        pytest.skip("外す案が上位3件に入らなかった")
    c = items[pick]
    Session(store, 2026, 12, None).accept(uid, pick=pick)
    m = store.month(2026, 12)
    assert m.table[c["staff"]][11] == c["symbol"]
    rules = month_rules(store, 2026, 12)
    bad = [b for b in check_table(rules, store.facility(), store.staff_on(2026, 12), m, m.table)
           if b["rule"] in c["except"] and b["key"].startswith(c["staff"])]
    assert bad == []
    cur = store.ledger().rules_at()
    assert all(f"{c['staff']}/2026-12-12/{c['symbol']}" in cur[x].meta["except_cells"] for x in c["except"])


def test_1人では埋まらない日は理由と次の手だけを言い押す案を出さない(made, tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFTVOICE_PATIENCE", "0")
    store, _ = _copy(made, tmp_path, 3, ban=False)
    r = Session(store, 2026, 12, None).rebuild(tries=1)
    h = r.extra["hole"]
    assert h["uid"] is None and h["others"] == []
    assert h["message"].startswith("12日（土）は、") and "1人に頼むだけでは埋まらない" in h["message"]
    assert h["message"].endswith("希望休を本人と相談してください") and h["message"] in r.message
    assert not any((x.get("offer") or {}).get("hole") for x in store.pending(2026, 12))


def test_案を作った後に表が変わったら古い案では直さない(blocked):
    store, _ = blocked
    r = Session(store, 2026, 12, None).rebuild(tries=1)
    uid = r.extra["hole"]["uid"]
    m = store.month(2026, 12)
    name = next(iter(m.table))
    m.table[name][20] = "公" if m.table[name][20] != "公" else "日"
    store.save_month(m)
    before = _snap(store)
    r2 = Session(store, 2026, 12, None).accept(uid)
    assert r2.kind == "ask" and "この案は使えません" in r2.message
    assert _snap(store) == before
    assert not any(x["id"] == uid for x in store.pending(2026, 12))
