"""話して足すルールの読み違い（2026-11-07 夜、実際の施設の表と同じ条件を画面の口で作った通しの試しで見つかった物）。
作り話の2ユニット10人の施設（tests/fixtures/pastes/excel_nights_no_ake.tsv を取り込んだ12月）で、録った形の返事で確かめる。
API は呼ばない。直す前は、どれも黙って違う中身で入った（表を大きく崩した物もある）。"""
import json
from pathlib import Path

import pytest

from shiftvoice.model import Rule
from shiftvoice.names import Masker
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.rules import with_overrides
from shiftvoice.session import Session
from shiftvoice.store import Store

PASTE = (Path(__file__).parent / "fixtures" / "pastes" / "excel_nights_no_ake.tsv").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    out = tmp_path_factory.mktemp("spoken") / "f"
    c = create_facility(infer(parse_paste(PASTE), 2026, 11, {}), out, name="試験", next_year=2026, next_month=12)
    assert c.next_table_made
    return out


def _say(root: Path, tmp_path: Path, text: str, items: list[dict]):
    import shutil
    dst = tmp_path / "f"
    shutil.copytree(root, dst)
    store = Store(dst)
    masker = Masker(store.staff(), store.facility())
    items = [{k: (masker.to_code[v] if k == "staff" and isinstance(v, str) and v in masker.to_code else v)
              for k, v in it.items()} for it in items]
    s = Session(store, 2026, 12, RecordedReader({masker.mask(text)[0]: json.dumps({"items": items}, ensure_ascii=False)}))
    s.open()
    before = set(store.ledger().rules_at())
    r = s.say(text)
    if r.kind == "ask_period":
        r = s.answer(r.uid, "rule")
    if r.kind == "ask_replace":   # 前からある同じ所のルール（連勤の上限など）と別に足す
        r = s.answer(r.uid, "add")
    new = [x for k, x in store.ledger().rules_at().items() if k not in before]
    return r, new


def test_勤務を言った続けないは_その勤務の続く日の上限(made, tmp_path):
    """「全員、早番を3日続けて入れないで」が「全員 連勤は2日まで」になり、124か所破れた"""
    r, new = _say(made, tmp_path, "全員、早番を3日続けて入れないで", [{"type": "consecutive_limit", "staff": None, "max": 2}])
    assert r.kind == "applied", r.message
    assert [(x.kind, x.params, x.name) for x in new] == [("max_run", {"shifts": ["早"], "max": 2}, "全員 早番は続けて2日まで")]


def test_夜勤を2日続けないは夜勤の上限1(made, tmp_path):
    r, new = _say(made, tmp_path, "青木さん、夜勤を2日続けないで", [{"type": "consecutive_limit", "staff": "青木", "max": 1}])
    assert r.kind == "applied", r.message
    assert [(x.params, x.who, x.name) for x in new] == [({"shifts": ["夜"], "max": 1}, ["青木"], "青木さん 夜勤を2日続けない")]


def test_連勤はこれまでどおり(made, tmp_path):
    r, new = _say(made, tmp_path, "全員、連勤は4日まで", [{"type": "consecutive_limit", "staff": None, "max": 4}])
    assert r.kind == "applied" and [x.params for x in new] == [{"shifts": "work", "max": 4}]


def test_有給の日数はちょうど(made, tmp_path):
    """「有給を1日」が下限だけになり、表を作る計算が有給を埋め草に使って8日になった"""
    r, new = _say(made, tmp_path, "青木さん、有給を1日", [{"type": "leave_count", "staff": "青木", "count": 1}])
    assert r.kind == "applied", r.message
    assert [x.params for x in new] == [{"shifts": ["有"], "min": 1, "max": 1}]
    assert Store(tmp_path / "f").month(2026, 12).table["青木"].count("有") == 1


def test_有給の多くてもは上限(made, tmp_path):
    """「有給は多くても月2日まで」が下限2日（日付は任せる）になり、有給が増えた"""
    r, new = _say(made, tmp_path, "青木さん、有給は多くても月2日まで", [{"type": "leave_count", "staff": "青木", "count": 2}])
    assert r.kind == "applied", r.message
    assert [(x.params, x.name) for x in new] == [({"shifts": ["有"], "max": 2}, "青木さん 有給は月2日まで")]


def test_日付を決めた人数は毎日にせず聞き返す(made, tmp_path):
    """「28日は行事なので、早番を5人にして」が毎日の早番5人になった"""
    r, new = _say(made, tmp_path, "28日は行事なので、早番を5人にして", [{"type": "coverage", "shift": "早", "min": 5, "max": None}])
    assert r.kind != "applied" and "日付を決めた人数" in (r.message or "")
    assert new == []


def test_できればは配慮の強さ(made, tmp_path):
    """「日勤はできれば毎日1人」が本人の希望の強さ（運用の都合より強い）で入り、ほかの決まりを押しのけた"""
    r, new = _say(made, tmp_path, "日勤はできれば毎日1人", [{"type": "coverage", "shift": "日", "min": 1, "max": None,
                                                         "strength": "wish"}])
    assert r.kind == "applied", r.message
    assert [x.flex for x in new] == ["negotiable"]


def test_合わせての回数は1本(made, tmp_path):
    """「日勤と遅番は合わせて月8回まで」が日勤8回・遅番8回の2本になった"""
    items = [{"type": "count_limit", "staff": ["青木"], "shift": "日", "min": None, "max": 8},
             {"type": "count_limit", "staff": ["青木"], "shift": "遅", "min": None, "max": 8}]
    r, new = _say(made, tmp_path, "青木さん、日勤と遅番は合わせて月8回まで", items)
    assert r.kind == "applied", r.message
    assert [(x.params, x.name) for x in new] == [({"shifts": ["日", "遅"], "max": 8}, "青木さん 日勤と遅番は合わせて月8回まで")]


def test_いつも施設全体の数を言った人数は施設全体(made, tmp_path):
    """夜勤は施設全体でいつも2人（ユニットでは1人）。「夜勤は毎日ちょうど2人」がユニットごと2人になり、35か所破れた"""
    r, new = _say(made, tmp_path, "夜勤は毎日ちょうど2人", [{"type": "coverage", "shift": "夜", "min": 2, "max": 2}])
    assert r.kind == "applied", r.message
    assert [(x.params["scope"], x.params["n"]) for x in new] == [("all", 2)]


def test_後から言った全ユニット向けの人数に_前の名指しのユニットが譲る():
    """取り込みで作った「U1・U3は夜勤2人まで」（名指し）が、後から言った「どのユニットも夜勤は1人まで」に勝ち、新しいルールが
    U1・U3 に効かなかった。新しい方が厳しい時は両方効き（名指しの2人までと、全ユニットの1人まで）、ゆるい時は名指しが譲る"""
    named = Rule("r009", 1, "夜勤の人数（U1・U3の上限）", "coverage", "negotiable",
                 {"shifts": ["夜"], "scope": "each_unit", "op": "<=", "n": 2, "units": ["U1", "U3"]})
    strict = Rule("n001", 1, "どのユニットも夜勤は1人まで", "coverage", "operational",
                  {"shifts": ["夜"], "scope": "each_unit", "op": "<=", "n": 1})
    eff = {r.id: r for r in with_overrides([named, strict])}
    assert not eff["n001"].meta.get("skip_max") and not eff["r009"].meta.get("skip_max")
    loose = Rule("n002", 1, "どのユニットも夜勤は3人まで", "coverage", "operational",
                 {"shifts": ["夜"], "scope": "each_unit", "op": "<=", "n": 3})
    eff = {r.id: r for r in with_overrides([named, loose])}
    assert eff["r009"].meta.get("skip_max") == ["U1", "U3"] and not eff["n002"].meta.get("skip_max")
    # 名指しの方が新しい時は、これまでどおり名指しが勝つ
    eff = {r.id: r for r in with_overrides([strict, named])}
    assert eff["n001"].meta.get("skip_max") == ["U1", "U3"] and not eff["r009"].meta.get("skip_max")


# ---- Astra（2026-11-07）が上の直しの副作用として見つけた形
def test_1ユニットにつきはユニットごと(made, tmp_path):
    r, new = _say(made, tmp_path, "1ユニットにつき夜勤は毎日2人", [{"type": "coverage", "shift": "夜", "min": 2, "max": 2}])
    assert r.kind == "applied", r.message
    assert [x.params["scope"] for x in new] == ["each_unit"]


def test_期限のまでは有給の上限にしない(made, tmp_path):
    r, new = _say(made, tmp_path, "青木さん、月末までに有給を1日", [{"type": "leave_count", "staff": "青木", "count": 1}])
    assert r.kind == "applied", r.message
    assert [x.params for x in new] == [{"shifts": ["有"], "min": 1, "max": 1}]


def test_別の件のできればで強い回数を弱めない(made, tmp_path):
    items = [{"type": "count_limit", "staff": ["青木"], "shift": "夜", "min": None, "max": 0, "strength": "operational"},
             {"type": "count_limit", "staff": ["青木"], "shift": "早", "min": None, "max": 3, "strength": "negotiable"}]
    r, new = _say(made, tmp_path, "青木さんは夜勤0回、できれば早番も3回まで", items)
    assert r.kind == "applied", r.message
    assert sorted((x.params["shifts"][0], x.flex) for x in new) == [("夜", "operational"), ("早", "negotiable")]


def test_含めての連続は全部の勤務の連勤(made, tmp_path):
    r, new = _say(made, tmp_path, "全員、夜勤も含めて5日連続で働かせないで", [{"type": "consecutive_limit", "staff": None, "max": 4}])
    assert r.kind == "applied", r.message
    assert [x.params for x in new] == [{"shifts": "work", "max": 4}]


def test_1日につきの人数は日付にしない(made, tmp_path):
    r, new = _say(made, tmp_path, "夜勤は1日につき2人", [{"type": "coverage", "shift": "夜", "min": 2, "max": 2}])
    assert r.kind == "applied", r.message
    assert [x.params["n"] for x in new] == [2]


def test_日付と曜日を言った人数も毎週にせず聞き返す(made, tmp_path):
    r, new = _say(made, tmp_path, "28日の日曜は早番5人", [{"type": "coverage", "shift": "早", "min": 5, "max": None,
                                                          "weekdays": ["sun"]}])
    assert r.kind != "applied" and "日付を決めた人数" in (r.message or "") and new == []


def test_合わせての別の意味は1本にしない(made, tmp_path):
    items = [{"type": "count_limit", "staff": ["青木"], "shift": "遅", "min": None, "max": 8},
             {"type": "count_limit", "staff": ["青木"], "shift": "日", "min": None, "max": 8}]
    r, new = _say(made, tmp_path, "青木さん、遅番に合わせて日勤も月8回まで", items)
    assert r.kind == "applied", r.message
    assert sorted(tuple(x.params["shifts"]) for x in new) == [("日",), ("遅",)]


def test_弱い新しい人数で動かせない名指しの人数を外さない():
    named = Rule("r010", 1, "東の夜勤は2人", "coverage", "fixed",
                 {"shifts": ["夜"], "scope": "each_unit", "op": ">=", "n": 2, "units": ["U1"]})
    weak = Rule("n003", 1, "各ユニットできれば夜勤1人", "coverage", "negotiable",
                {"shifts": ["夜"], "scope": "each_unit", "op": "==", "n": 1})
    eff = {r.id: r for r in with_overrides([named, weak])}
    assert not eff["r010"].meta.get("skip_min")


def test_名指しのゆるい連勤の上限は全員の上限の例外(made, tmp_path):
    """「千葉さん、今月だけ連勤6日まで」が「もう守られています」で断られた。希望休と公休の数で6連勤が避けられない人の月は
    作れず、段を下げてほかの人の希望休まで外れた（実際の施設は10月だけ6連勤を許していた）"""
    from shiftvoice.build import month_rules
    from shiftvoice.rules import expand
    r, new = _say(made, tmp_path, "青木さん、今月だけ連勤6日まで", [{"type": "consecutive_limit", "staff": "青木", "max": 6}])
    assert r.kind == "applied", r.message
    assert [(x.params, x.who) for x in new] == [({"shifts": "work", "max": 6}, ["青木"])]
    store = Store(tmp_path / "f")
    rules = month_rules(store, 2026, 12, with_premise=False)
    eff = with_overrides(rules)
    general = next(x for x in eff if x.kind == "max_run" and not x.who and x.params["shifts"] == "work")
    assert general.meta.get("except_who") == ["青木"]
    fac, staff, m = store.facility(), store.staff_on(2026, 12), store.month(2026, 12)
    assert not any(i.terms[0].staff == "青木" for i in expand(general, fac, staff, m))
    # 名指しの方がきびしい時は、全員の上限からは外さない（両方効く）
    tight = Rule("t1", 1, "青木さん 連勤は3日まで", "max_run", "operational", {"shifts": "work", "max": 3}, who=["青木"])
    eff = with_overrides([x for x in rules if x.id != new[0].id] + [tight])
    general = next(x for x in eff if x.kind == "max_run" and not x.who and x.params["shifts"] == "work")
    assert not general.meta.get("except_who")


# ---- Astra（2026-11-07 2回目）
def test_できればの例外では動かせない全員の上限から外さない():
    general = Rule("r025", 1, "連勤の上限", "max_run", "fixed", {"shifts": "work", "max": 5})
    weak = Rule("n9", 1, "青木さん 連勤は6日まで", "max_run", "negotiable", {"shifts": "work", "max": 6}, who=["青木"],
                meta={"exception": True})
    plain = Rule("n8", 1, "青木さん 連勤は6日まで", "max_run", "operational", {"shifts": "work", "max": 6}, who=["青木"],
                 meta={"exception": True})
    assert not next(r for r in with_overrides([general, weak]) if r.id == "r025").meta.get("except_who")
    assert next(r for r in with_overrides([general, plain]) if r.id == "r025").meta.get("except_who") == ["青木"]


def test_何日までの人数も日付として聞き返す(made, tmp_path):
    r, new = _say(made, tmp_path, "今月は28日まで早番3人", [{"type": "coverage", "shift": "早", "min": 3, "max": 3}])
    assert r.kind != "applied" and "日付を決めた人数" in (r.message or "") and new == []
    (tmp_path / "b").mkdir()
    r, new = _say(made, tmp_path / "b", "28日に三人、早番をお願いします", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind != "applied" and new == []


def test_最大や間の言葉があっても勤務の続く日数(made, tmp_path):
    r, new = _say(made, tmp_path, "全員、今月は早番を最大2日連続までにして", [{"type": "consecutive_limit", "staff": None, "max": 2}])
    assert r.kind == "applied", r.message
    assert [x.params for x in new] == [{"shifts": ["早"], "max": 2}]


def test_勤務の名前と連続を言って形が読めない時は聞き返す(made, tmp_path):
    r, new = _say(made, tmp_path, "早番について、3日以上の連続はやめて", [{"type": "consecutive_limit", "staff": None, "max": 2}])
    assert r.kind != "applied" and "どの勤務を続けて" in (r.message or "") and new == []


def test_有給の何日までには期限(made, tmp_path):
    r, new = _say(made, tmp_path, "青木さん、今月は20日までに有給1日", [{"type": "leave_count", "staff": "青木", "count": 1}])
    assert r.kind == "applied", r.message
    assert [x.params for x in new] == [{"shifts": ["有"], "min": 1, "max": 1}]


def test_ただの名指しのゆるい上限は今までどおりもう守られている(made, tmp_path):
    """「今月だけ」「〜てもいい」などの例外の言い方が無い「青木さん、連勤6日まで」は、全員の連勤の上限がもう決めている"""
    from shiftvoice.build import month_rules
    r, new = _say(made, tmp_path, "青木さん、連勤6日まで", [{"type": "consecutive_limit", "staff": "青木", "max": 6}])
    assert "もう守られています" in (r.message or "")
    eff = with_overrides(month_rules(Store(tmp_path / "f"), 2026, 12, with_premise=False))
    general = next(x for x in eff if x.kind == "max_run" and not x.who and x.params["shifts"] == "work")
    assert not general.meta.get("except_who")


# ---- 2回目の通しの試し（2026-11-07 2時、直した版で同じ10月をもう一度作った）で見つかった形
def test_施設全体の同じ人数のルールがあれば施設全体(made, tmp_path, monkeypatch):
    """作った表が段を下げて崩れていると、いつもの数を表から当てられず「夜勤は毎日ちょうど2人」がユニットごと2人のままだった"""
    import shiftvoice.intake as I
    monkeypatch.setattr(I, "_typical_scope", lambda *a, **k: None)   # 表から当てられない時
    import shutil
    shutil.copytree(made, tmp_path / "src")
    st = Store(tmp_path / "src")
    led = st.ledger()
    led.append("add", rule={"id": "t-all", "ver": 1, "name": "夜勤の人数（施設全体の下限）", "kind": "coverage", "flex": "fixed",
                            "params": {"shifts": ["夜"], "scope": "all", "op": ">=", "n": 2}, "who": None, "when": None,
                            "scope": "rule"})
    st.save_ledger(led)
    (tmp_path / "b").mkdir()
    r, new = _say(tmp_path / "src", tmp_path / "b", "夜勤は毎日ちょうど2人。毎月", [{"type": "coverage", "shift": "夜", "min": 2, "max": 2}])
    assert r.kind == "applied", r.message
    assert [x.params["scope"] for x in new] == ["all"]


def test_読み取りの返事の長さは8人分を言える():
    """8人の勤務を1回で言った一言の返事が、400トークンで途中で切れて読み取りに失敗した"""
    from shiftvoice import reader
    assert reader.MAX_TOKENS >= 800


def test_夜勤は金曜と土曜だけは毎週夜勤にしない(made, tmp_path):
    """「夜勤は金曜と土曜だけ」に「金土曜は夜勤にする」が足され、104マス動いた"""
    items = [{"type": "weekday_pref", "staff": "青木", "weekdays": ["sun", "mon", "tue", "wed", "thu"], "shift": "夜", "avoid": True},
             {"type": "weekday_pref", "staff": "青木", "weekdays": ["fri", "sat"], "shift": "夜", "avoid": False}]
    r, new = _say(made, tmp_path, "青木さん、日曜から木曜は夜勤にしないで。夜勤は金曜と土曜だけ。毎月", items)
    assert r.kind == "applied", r.message
    assert [(x.params.get("not_in"), sorted(x.params["weekdays"])) for x in new] == \
        [(["夜"], sorted(["sun", "mon", "tue", "wed", "thu"]))]
    (tmp_path / "b").mkdir()
    r, new = _say(made, tmp_path / "b", "石田さん、夜勤は金曜と土曜だけ。毎月",
                  [{"type": "weekday_pref", "staff": "石田", "weekdays": ["fri", "sat"], "shift": "夜", "avoid": False}])
    assert r.kind == "applied", r.message
    assert [(x.params.get("not_in"), sorted(x.params["weekdays"])) for x in new] == \
        [(["夜"], sorted(["sun", "mon", "tue", "wed", "thu"]))]


def test_公休の日数を日付の休みと読んだら聞き返す(made, tmp_path):
    r, new = _say(made, tmp_path, "青木さん、公休は今月は10日", [{"type": "day_off", "staff": "青木", "days": [10]}])
    assert r.kind != "applied" and "公休の日数" in (r.message or "") and new == []


def test_夜勤の間を空ける頼みを2連夜勤の禁止にしない(made, tmp_path):
    r, new = _say(made, tmp_path, "全員、夜勤のあと3日以内に次の夜勤を入れないで",
                  [{"type": "forbid_sequence", "staff": None, "first": "夜", "next": "夜", "strength": "negotiable"}])
    assert r.kind != "applied" and "何日空けるか" in (r.message or "") and new == []


def test_休みか早番は1マスのどちらか(made, tmp_path):
    """「5日は休みか早番」が、休みと早番の2つの指定（ぶつかる）になった"""
    items = [{"type": "day_off", "staff": "青木", "days": [5]}, {"type": "set_cell", "staff": "青木", "days": [5], "shift": "早"}]
    r, new = _say(made, tmp_path, "青木さん、5日は休みか早番", items)
    assert r.kind == "applied", r.message
    assert [(x.params, x.when) for x in new] == [({"in": ["公", "早"]}, [5])]
    assert Store(tmp_path / "f").month(2026, 12).table["青木"][4] in ("公", "早")


def test_日付を決めた頼みの日は同じ人の曜日の頼みより優先():
    """「土日は事務所に入れない」と「18日（日）は事務所の日直」がぶつかり、表が作れず段を下げてほかの人の頼みまで外れた"""
    from shiftvoice.model import Facility, Month, ShiftType, Staff
    from shiftvoice.rules import expand
    fac = Facility("x", "x", ["東"], [ShiftType("事", "事務所", "work"), ShiftType("公", "公休", "rest")], {})
    staff = [Staff("赤坂", "東", ["事"])]
    m = Month(2026, 10, [], {})
    wk = Rule("w", 1, "赤坂さん 土日曜は事務所に入れない", "weekday", "wish", {"weekdays": ["sat", "sun"], "not_in": ["事"]}, who=["赤坂"])
    cell = Rule("c", 1, "赤坂さん 18日 事務所", "cell", "wish", {"in": ["事"]}, who=["赤坂"], when=[18])
    eff = next(r for r in with_overrides([wk, cell]) if r.id == "w")
    days = sorted(int(i.key.split("/")[1]) for i in expand(eff, fac, staff, m))
    assert 18 not in days and 17 in days and 25 in days
