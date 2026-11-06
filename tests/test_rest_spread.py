"""31日の月に公休の数を守りきれない時、1人に寄せず、誰がいくつ外れたかを言う（2026-11-06 通しの試し）。

見本は作り話の3ユニット16人の11月の表。公休9日のまま12月（31日）へ進むと、毎日の人数より人が余り、段3へ下がる。
直す前は、外す人を少なくするために久保さんだけ公休19日の表が「できました」と出て、誰が外れたかも画面に出なかった。"""
import copy
from pathlib import Path

from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.store import Store

PASTE = Path(__file__).parent / "fixtures" / "pastes" / "three_units.tsv"
ANSWERS = {"日8": {"name": "日8の勤務", "kind": "work"}}


def made(tmp_path):
    prop = infer(parse_paste(PASTE.read_text(encoding="utf-8")), 2026, 11, ANSWERS)
    c = create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=12, answers=ANSWERS)
    return c, Store(tmp_path / "f").month(2026, 12).table


def test_rest_is_spread_thin_and_named(tmp_path):
    c, table = made(tmp_path)
    assert c.next_table_made
    rests = {n: row.count("公") for n, row in table.items()}
    assert max(rests.values()) - min(rests.values()) <= 2, rests
    assert min(rests.values()) >= 8
    # 誰がいくつ外れたかを、画面に出る知らせで言う
    off = [n for n, v in rests.items() if v != 9]
    assert "外れたのは" in c.stage_note and "公休の数（9）" in c.stage_note
    assert f"{off[0]}さん{rests[off[0]]}" in c.stage_note


def test_broken_text_says_people_and_places():
    from shiftvoice.build import broken_text
    b = [{"flex": "operational", "name": "公休の数", "key": n, "value": 10, "lo": 9, "hi": 9} for n in "ABCDEFGH"]
    b.append({"flex": "negotiable", "name": "相談", "key": "A", "value": 1, "lo": 0, "hi": 0})
    b.append({"flex": "wish", "name": "早番の人数", "key": "3日", "value": 1, "lo": 2, "hi": None})
    t = broken_text(b, set("ABCDEFGH"))
    assert t == "外れたのは、公休の数（9）がAさん10・Bさん10・Cさん10・Dさん10・Eさん10・Fさん10・ほか2人、早番の人数が1か所"
    assert broken_text(b[8:9], set("A")) == ""


def test_rest_example_says_per_month():
    """「久保さん、公休は9日」は本物の Haiku が9日の休みと読んだ。勧める言い方には「月」を入れる"""
    prop = infer(parse_paste(_long_rest()), 2026, 11, ANSWERS)
    assert prop.rest_notes and all("公休は月" in t for t in prop.rest_notes)


def _long_rest() -> str:
    lines = PASTE.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        c = ln.split("\t")
        if c[0] == "久保":   # 月の途中から入った人の形。前半を全部休みにする
            c[1:16] = ["公"] * 15
        out.append("\t".join(c))
    return "\n".join(out) + "\n"


def test_wish_on_the_morning_after_a_night_does_not_drop_the_others(nov):
    """前の月の末が夜勤の人の1日の希望休は叶わない。その日だけ外して知らせ、同じ人のほかの日と、ほかの人の希望は守る"""
    from shiftvoice.build import solve_staged
    from shiftvoice.model import Rule
    fac, staff, m, rules = nov
    m = copy.deepcopy(m)
    rs = [r for r in rules if not r.meta.get("premise")]
    a, b = staff[0].name, staff[1].name
    m.prev_tail = {**m.prev_tail, a: ["公", "公", "公", "公", "夜"], b: ["公", "公", "公", "公", "公"]}
    wishes = [Rule(id="w1", ver=1, name=f"{a}さん 1・9日 希望休", kind="cell", flex="wish", params={"in": ["公"]}, who=[a], when=[1, 9]),
              Rule(id="w2", ver=1, name=f"{b}さん 1日 希望休", kind="cell", flex="wish", params={"in": ["公"]}, who=[b], when=[1])]
    r = solve_staged(fac, staff, rs + wishes, m)
    assert r.ok and r.result.stage in (1, 3)
    assert f"{a}さんの1日は前の月の夜勤の明け" in r.note
    assert r.result.table[a][0] == "明" and r.result.table[a][8] == "公"
    assert r.result.table[b][0] == "公"


def test_next_month_does_not_say_rules_are_kept_when_one_was_dropped(tmp_path):
    from shiftvoice.nextflow import NextFlow
    made(tmp_path)
    out = NextFlow(Store(tmp_path / "f"), 2026, 11).build()
    assert out["note"] and "外れたのは" in out["note"]
    assert "そのまま" not in out["sub"]


def test_one_cell_word_stays_small_in_a_month_that_dropped_a_rule(tmp_path):
    """公休を守りきれなかった月でも、1マスの一言は狭い範囲で直す（直す前は54マス動いた）"""
    from shiftvoice.build import month_rules
    from shiftvoice.model import Rule
    from shiftvoice.resolve import resolve
    made(tmp_path)
    store = Store(tmp_path / "f")
    m = store.month(2026, 12)
    who = "青木"
    want = "遅" if m.table[who][11] != "遅" else "早"
    new = Rule(id="n1", ver=1, name=f"{who}さん 12日", kind="cell", flex="wish", params={"in": [want]}, who=[who], when=[12],
               scope="once")
    rules = month_rules(store, 2026, 12, with_premise=False) + [new]
    r = resolve(store.facility(), store.staff_on(2026, 12), rules, m, m.table, [new])
    assert r.ok and r.table[who][11] == want
    assert r.window is not None, r.tries
    assert len(r.changes) <= 16, r.changes


def test_fixed_cell_on_a_wish_day_only_drops_that_day(nov):
    """希望休の日に勤務を頼んだ時は、その日の希望だけ外す。同じ人のほかの日とほかの人の希望は守る"""
    from shiftvoice.build import solve_staged
    from shiftvoice.model import Rule
    fac, staff, m, rules = nov
    rs = [r for r in rules if not r.meta.get("premise")]
    a, b = staff[0].name, staff[1].name
    wish = dict(kind="cell", flex="wish", params={"in": ["公"]})
    new = [Rule(id="w1", ver=1, name=f"{a}さん 8・10日 希望休", who=[a], when=[8, 10], **wish),
           Rule(id="w2", ver=1, name=f"{b}さん 10日 希望休", who=[b], when=[10], **wish),
           Rule(id="c1", ver=1, name=f"{a}さん 10日 早番", kind="cell", flex="fixed", params={"in": ["早"]}, who=[a], when=[10])]
    r = solve_staged(fac, staff, rs + new, m)
    assert r.ok and r.result.stage in (1, 3)
    assert f"{a}さんの10日は" in r.note
    t = r.result.table
    assert t[a][9] == "早" and t[a][7] == "公" and t[b][9] == "公"
