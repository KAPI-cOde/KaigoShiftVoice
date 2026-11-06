"""31日の月に公休の数を守りきれない時、1人に寄せず、誰がいくつ外れたかを言う（2026-11-06 通しの試し）。

見本は作り話の3ユニット16人の11月の表。直す前は、外す人を少なくするために久保さんだけ公休19日の表が「できました」と
出て、誰が外れたかも画面に出なかった。

公休を守りきれない月は、作成者が「東の早番は1人、日勤は1人まで（どちらも動かせない）」と答えた形で作る（made の tight）。
1日の勤務の枠が最大11（早3・日1・遅3・夜2・明け2）になり、毎日5人以上が休む。31日で155日 > 16人×9日なので、
どう組んでも公休9日を守りきれない。
貼ったままの見本（tight でない）は、前は「人が余って守れない」と思っていたが、段3の計算が1人の作業者では悪い表から
抜け出せなかっただけだった（2026-11-06。作業者を回すと公休は全員9日、遅番の次の日の早番は0回）。"""
import copy
from pathlib import Path

from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.store import Store

PASTE = Path(__file__).parent / "fixtures" / "pastes" / "three_units.tsv"
ANSWERS = {"日8": {"name": "日8の勤務", "kind": "work"}}


def tighten(rules: list[dict]) -> None:
    """東の早番は1人（下限と上限）、日勤の上限1人を、動かせないルールにする。相談できるままだと、段3では外して
    余った人を勤務に回せるので、公休の外れが起きない"""
    for r in rules:
        if r["name"] in ("早番の人数（東の下限）", "早番の人数（東の上限）"):
            r["params"] = {**r["params"], "op": "==", "n": 1}
            r["flex"] = "fixed"
        elif r["name"] == "日勤の人数（東の上限）":
            r["flex"] = "fixed"


def made(tmp_path, tight=True):
    prop = infer(parse_paste(PASTE.read_text(encoding="utf-8")), 2026, 11, ANSWERS)
    if tight:
        tighten(prop.rules)
    c = create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=12, answers=ANSWERS)
    return c, Store(tmp_path / "f").month(2026, 12).table


def test_pasted_three_units_keep_rest_and_avoid_late_then_early(tmp_path):
    """貼ったままの3ユニット16人の12月は、公休9日を全員守れる。直す前は段3の計算が1人の作業者で、公休8〜11日・
    運用の都合の外れ17件・遅番の次の日の早番23回の表を「できました」と出していた（2026-11-06。時刻を6時45分・
    13時15分にした時に見つかった）"""
    from shiftvoice.build import month_rules
    from shiftvoice.interval import count_short
    from shiftvoice.rules import check_table
    c, table = made(tmp_path, tight=False)
    assert c.next_table_made
    assert {row.count("公") for row in table.values()} == {9}
    store = Store(tmp_path / "f")
    fac, m = store.facility(), store.month(2026, 12)
    assert count_short(table, fac.shift_types, 14, m.prev_tail)[0] == 0
    off = [b for b in check_table(month_rules(store, 2026, 12, with_premise=False), fac, store.staff_on(2026, 12), m, table)
           if b["flex"] == "operational"]
    assert len(off) <= 2, off


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


def small(wish_days: int, **settings):
    """職員16人が毎日10人勤務する30日の月（公休の平均は11日強）。公休9日のルールは守れず、職員00さんが1日からの希望休を持つ"""
    from shiftvoice.model import Facility, Month, Rule, ShiftType, Staff
    fac = Facility("x", "x", ["東"], [ShiftType("日", "日勤", "work"), ShiftType("公", "公休", "rest")],
                   {"solve": {"max_deterministic_time": 1}, **settings})
    staff = [Staff(f"職員{i:02}", "東", ["日"]) for i in range(16)]
    rules = [Rule("rest", 1, "公休の数", "count", "operational", {"shifts": ["公"], "min": 9, "max": 9}),
             Rule("cov", 1, "毎日日勤10人", "coverage", "fixed", {"shifts": ["日"], "scope": "all", "op": "==", "n": 10}),
             Rule("wish", 1, "職員00さん 希望休", "cell", "wish", {"in": ["公"]}, who=["職員00"],
                  when=list(range(1, wish_days + 1)))]
    return fac, staff, rules, Month(2026, 11, [], {})


def test_one_long_wish_does_not_lift_the_limit_for_everyone():
    """公休9日に14日の希望休が1件。直す前は上限が無制限まで外れ、公休28日の人が出た（Astra 2026-11-06 の指摘2）"""
    from shiftvoice.build import solve_staged
    r = solve_staged(*small(14))
    assert r.ok and r.result.stage == 3
    rests = {n: row.count("公") for n, row in r.result.table.items()}
    assert rests.pop("職員00") == 14
    assert max(rests.values()) <= 12, rests   # ほかの人は、希望の分まで広げず3日のずれまで


def test_forced_dev_counts_only_what_the_pinned_cells_force():
    from shiftvoice.rules import expand
    from shiftvoice.solver import forced_dev, pinned_cells
    fac, staff, rules, m = small(14)
    pins = pinned_cells(rules, {"fixed", "wish"})
    dev = {i.key: forced_dev(i, pins) for i in expand(rules[0], fac, staff, m)}
    assert dev["職員00"] == 5 and dev["職員01"] == 0
    assert forced_dev(next(i for i in expand(rules[0], fac, staff, m) if i.key == "職員00"), pinned_cells(rules, {"fixed"})) == 0


def one_rest_more(tmp_path):
    """公休が外れている人に、勤務の日の休みを足す一言の材料"""
    from shiftvoice.build import month_rules
    from shiftvoice.model import Rule
    made(tmp_path)
    store = Store(tmp_path / "f")
    m = store.month(2026, 12)
    who = next(n for n, row in m.table.items() if row.count("公") > 9)
    day = next(d for d in range(5, 28) if m.table[who][d - 1] in ("早", "遅"))
    new = Rule(id="n1", ver=1, name=f"{who}さん {day}日 休み", kind="cell", flex="wish", params={"in": ["公"]},
               who=[who], when=[day], scope="once")
    return store, m, who, day, month_rules(store, 2026, 12, with_premise=False) + [new], new


def test_rest_count_already_off_is_not_made_worse(tmp_path):
    """公休11日の人に別の日の休みを足しても、別の休みを振り替えて増やさない（直す前は12日になり知らせも無かった。指摘1）"""
    from shiftvoice.resolve import resolve
    store, m, who, day, rules, new = one_rest_more(tmp_path)
    r = resolve(store.facility(), store.staff_on(2026, 12), rules, m, m.table, [new])
    assert r.ok and r.table[who][day - 1] == "公"
    assert r.table[who].count("公") == m.table[who].count("公")
    assert r.window is not None and len(r.changes) <= 16, r.changes
    assert not r.staged.note


def test_rest_count_says_so_when_it_has_to_get_worse():
    """どの休みも振り替えられない時は、離れたことを人と数で言う（指摘1）"""
    from shiftvoice.model import Facility, Month, Rule, ShiftType, Staff
    from shiftvoice.resolve import resolve
    fac = Facility("x", "x", ["東"], [ShiftType("日", "日勤", "work"), ShiftType("公", "公休", "rest")], {})
    staff = [Staff("青木", "東", ["日"]), Staff("石田", "東", ["日"])]
    base = {"青木": ["公"] * 10 + ["日"] * 20, "石田": ["公"] * 9 + ["日"] * 21}
    rules = [Rule("rest", 1, "公休の数", "count", "operational", {"shifts": ["公"], "min": 9, "max": 9}),
             Rule("w", 1, "青木さん 希望休", "cell", "wish", {"in": ["公"]}, who=["青木"], when=list(range(1, 11)))]
    new = Rule("n1", 1, "青木さん 20日 休み", "cell", "wish", {"in": ["公"]}, who=["青木"], when=[20], scope="once")
    r = resolve(fac, staff, rules + [new], Month(2026, 11, [], {}), base, [new])
    assert r.ok and r.table["青木"].count("公") == 11
    assert r.staged.note == "青木さんの公休の数が10から11になり、ルールの9からさらに離れました"


def test_rest_count_getting_worse_reaches_the_reply(sess):
    """振り替えられずに離れた時は、一言の返事の知らせ（画面の赤い文）に出る（指摘1）"""
    import json
    from shiftvoice.names import Masker
    from shiftvoice.reader import RecordedReader
    sess.open()
    who = "岡田"
    row = sess.store.month(2026, 11).table[who]
    rest = [d for d in range(1, 31) if row[d - 1] == "公"]
    work = next(d for d in range(30, 0, -1) if row[d - 1] in ("早", "日", "遅"))
    led = sess.store.ledger()   # 貼って作った施設と同じ運用の都合にし、全員の公休が1日外れている形にする
    led.append("change", rule_id="r005", rule={"flex": "operational", "params": {"shifts": ["公"], "min": 8, "max": 8}})
    led.append("add", rule={"id": "w1", "ver": 1, "name": "岡田さん 希望休", "kind": "cell", "flex": "wish", "params": {"in": ["公"]},
                            "who": [who], "when": rest, "scope": "rule"})
    sess.store.save_ledger(led)
    text = f"{who}さん、{work}日は休みで"
    masked, _ = Masker(sess.store.staff(), sess.store.facility()).mask(text)
    item = {"type": "day_off", "days": [work], "reason": None, "staff": masked.split("さん")[0]}
    sess.reader = RecordedReader({masked: json.dumps({"items": [item]}, ensure_ascii=False)})
    r = sess.say(text)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "once")
    assert r.kind == "applied", r.message
    assert f"{who}さんの公休の数が{len(rest)}から{len(rest) + 1}になり、ルールの8からさらに離れました" in r.warning, r
