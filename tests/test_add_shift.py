"""話して新しい勤務の種類を足す（作者「日8のような新しいシフトを足す所を見せたい」2026-11-06）。
足す一言は手元で読み（Claude へ送らない）、続きの一言は足した勤務の名前を「勤務A」に伏せて送る。録った返事は本物の Haiku の物。"""
import json
import shutil

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice import reader as reader_mod
from shiftvoice.build import month_rules
from shiftvoice.interval import short_pairs
from shiftvoice.nextflow import NextFlow
from shiftvoice.public import check_text
from shiftvoice.reader import RecordedReader
from shiftvoice.rules import check_table
from shiftvoice.server import table_tsv
from shiftvoice.session import Session
from shiftvoice.solver import solve_month
from shiftvoice.store import Store

ADD = "8時から17時の日8を足して"
SET = "10日の小島さん、日8に"
RULE = "平日は日8を1人"


def symbols(s):
    return s.store.facility().symbols


def added(s, say=ADD):
    r = s.say(say)
    assert r.kind == "applied", r.message
    return r


class Fake(RecordedReader):
    """録った返事に、形だけ確かめる返事を足した読み手。"""

    def __init__(self, more):
        super().__init__({**json.loads(REPLIES.read_text(encoding="utf-8")),
                          **{k: json.dumps({"items": v}, ensure_ascii=False) for k, v in more.items()}})


@pytest.mark.parametrize("said", [ADD, "日8っていう勤務を足して。8時から17時", "日8という勤務を追加して。8時から17時まで",
                                  "新しい勤務の日8を足して、8時から17時", "8時から17時で日8を作って", "日8を足して。時間は8時から17時です"])
def test_add_is_read_at_hand_and_stays(sess, said):
    table = sess.store.month(2026, 11).table
    rev = sess.store.ledger().rev
    r = sess.say(said)
    assert r.kind == "applied" and r.sent is None and sess.reader.sent == []    # Claude へは送らない
    assert r.message.startswith("日8（8時〜17時）を足しました")
    assert "今ある勤務の人数には数えません" in r.message
    st = sess.store.facility().shift("日8")
    assert (st.name, st.kind, st.start, st.hours) == ("日8", "work", "08:00", 9)
    assert st.on_request_only and st.spoken
    assert sess.store.month(2026, 11).table == table and sess.store.ledger().rev == rev
    assert sess.store.pending(2026, 11) == []
    assert not any(s.can and "日8" in s.can for s in sess.store.staff())      # 入れる勤務は変えない（全員が頼まれて入れる）


@pytest.mark.parametrize("said,start,hours,times", [
    ("8時半から17時半の日8を足して", "08:30", 9, "8時半〜17時半"),
    ("朝8時から夕方5時までの日8を足して", "08:00", 9, "8時〜17時"),
    ("8:00〜17:00の日8を足して", "08:00", 9, "8時〜17時"),
    ("８時から１７時の日８を足して", "08:00", 9, "8時〜17時"),
    ("8時から9時間半の日8を足して", "08:00", 9.5, "8時〜17時半"),
    ("10時15分から19時の日8を足して", "10:15", 8.75, "10時15分〜19時"),
])
def test_time_words(sess, said, start, hours, times):
    r = added(sess, said)
    assert f"日8（{times}）" in r.message
    st = sess.store.facility().shift("日8")
    assert (st.start, st.hours) == (start, hours)


@pytest.mark.parametrize("said", [
    "早番を足して", "10日に夜勤を足して", "小島さんの10日に夜勤を足して", "日勤を1人足して", "遅番をもう1人足して", "土日の早番を足して",
    "12日に西の早番を足して", "10日に8時から17時の早番を足して", "待機当番を足して", "呼び出し当番を足して", "看護師を足して",
    "8時から17時の看護師を足して", "休みを足して", "有給を足して", "ルールを足して", "もう1人足して", "小島さんに日8を足して",
    "10日に日8を足して", "夜勤を足して", "人を足して", "平日に日8を足して", "公休を1日足して", "8時から17時の人を足して",
])
def test_adding_to_people_or_days_is_not_a_new_shift(sess, said):
    before = symbols(sess)
    r = sess.say(said)
    assert symbols(sess) == before
    assert r.message != "" and "足しました" not in r.message
    assert sess.reader.sent or r.kind == "ask"   # 手元で足さず、今までどおり読み取りへ回す


@pytest.mark.parametrize("said", ["8時から17時の早番を足して", "遅番っていう勤務を足して", "日勤っていう勤務を足して。8時から17時",
                                  "8時から17時の早を足して"])
def test_same_as_a_shift_we_have_is_told_not_added(sess, said):
    before = symbols(sess)
    r = sess.say(said)
    assert r.kind == "ask" and "もうある勤務です" in r.message
    assert symbols(sess) == before and sess.reader.sent == []


def test_saying_the_added_one_again_is_told(sess):
    added(sess)
    r = sess.say("7時から16時の日8を足して")
    assert r.kind == "ask" and "もうある勤務です" in r.message
    assert sess.store.facility().shift("日8").start == "08:00"


@pytest.mark.parametrize("said", ["日8を足して", "日8っていう勤務を足して", "新しい勤務の日8を足して", "8時からの日8を足して"])
def test_no_times_is_asked_then_answer_adds(sess, said):
    before = symbols(sess)
    r = sess.say(said)
    assert r.kind == "ask" and r.message.startswith("日8は何時から何時までですか")
    assert symbols(sess) == before and sess.reader.sent == []
    r = sess.say("8時から17時です")
    assert r.kind == "applied" and "日8（8時〜17時）を足しました" in r.message
    assert sess.store.facility().shift("日8").start == "08:00"


def test_times_after_another_word_are_not_an_answer(sess):
    sess.say("日8を足して")
    sess.say("早番を足して")          # 別の話に移った
    r = sess.say("8時から17時")
    assert "日8" not in symbols(sess) and r.kind != "applied"


@pytest.mark.parametrize("said,word", [("22時から7時の夜2を足して", "日をまたぐ"), ("16時から翌9時の長夜を足して", "日をまたぐ"),
                                       ("夜10時から朝7時の夜2を足して", "日をまたぐ"), ("8時から5時の日8を足して", "8時から17時")])
def test_over_midnight_is_asked_not_added(sess, said, word):
    before = symbols(sess)
    r = sess.say(said)
    assert r.kind == "ask" and word in r.message and "足していません" in r.message
    assert symbols(sess) == before


def test_name_too_long_for_the_cell_is_asked(sess):
    before = symbols(sess)
    r = sess.say("8時から17時のロング日勤を足して")
    assert r.kind == "ask" and symbols(sess) == before


def test_cell_after_add_keeps_the_day_and_hides_the_name(sess):
    added(sess)
    before = sess.store.month(2026, 11).table["小島"][9]
    assert before != "日8"
    r = sess.say(SET)
    assert r.kind == "applied", r.message
    assert r.sent == "10日の職員11さん、勤務Aに"
    t = sess.store.month(2026, 11).table
    assert t["小島"][9] == "日8"       # 10日のまま。「日8」の8を日付にしない
    assert t["小島"][7] != "日8"
    msgs = sess.reader.sent[-1].messages
    assert len(msgs) == 3 and msgs[-1]["content"] == "一言「10日の職員11さん、勤務Aに」"
    assert '"shift": "勤務A"' in msgs[1]["content"]
    assert "日8" not in json.dumps(msgs, ensure_ascii=False)


def test_coverage_after_add_counts_as_its_own_shift(sess):
    added(sess)
    r = sess.say(RULE)
    assert r.kind == "ask_period"       # 人数のルールは今までどおり毎月か今月だけかを聞く
    r = sess.answer(r.uid, "rule")
    assert r.kind == "applied", r.message
    m = sess.store.month(2026, 11)
    rules = month_rules(sess.store, 2026, 11, with_premise=False)
    card = next(c for c in sess.state()["cards"] if "日8" in c["name"])
    assert card["params"]["shifts"] == ["日8"] and card["params"]["op"] == "==" and card["scope"] == "rule"
    staff = sess.store.staff_on(2026, 11)
    unit = {s.name: s.unit for s in staff}
    for d in range(1, m.days + 1):
        on = [n for n, row in m.table.items() if row[d - 1] == "日8"]
        if m.weekday(d) in ("sat", "sun"):
            assert on == []
        else:
            assert sorted(unit[n] for n in on) == ["東", "西"]   # ユニットごとに1人
    hard = [b for b in check_table(rules, sess.store.facility(), staff, m, m.table) if b["flex"] != "negotiable"]
    assert hard == []
    row = next(x for x in sess.state()["day_counts"] if x["symbol"] == "日8" and x["unit"] is None)
    assert row["name"] == "日8" and row["short"] == [] and sum(row["counts"]) == 42


def test_two_or_more_is_kept_as_a_lower_bound(tmp_path, sess):
    added(sess)
    sess.reader = Fake({"平日は勤務Aを2人以上。毎月": [{"type": "coverage", "shift": "勤務A", "min": 2, "max": None,
                                         "weekdays": ["mon"], "holidays": False, "strength": "operational"}]})
    r = sess.say("平日は日8を2人以上。毎月")
    assert r.kind == "applied", r.message
    assert next(c for c in sess.state()["cards"] if "日8" in c["name"])["params"]["op"] == ">="


def test_not_placed_unless_asked(sess):
    added(sess)
    fac, staff, m = sess.store.facility(), sess.store.staff_on(2026, 11), sess.store.month(2026, 11)
    res = solve_month(fac, staff, month_rules(sess.store, 2026, 11, with_premise=False), m, max_dtime=2.0)
    assert res.status == "ok" and not any("日8" in row for row in res.table.values())


def test_other_people_are_not_placed_by_a_one_cell_request(sess):
    added(sess)
    sess.say(SET)
    t = sess.store.month(2026, 11).table
    assert [n for n, row in t.items() if "日8" in row] == ["小島"] and t["小島"].count("日8") == 1


def test_other_says_are_sent_as_before(sess):
    """足した勤務を言っていない一言の送る中身は変わらない。指示文も変えていない。"""
    added(sess)
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.sent == "職員07さん、15と16は法事で休み"
    sent = sess.reader.sent[-1]
    assert len(sent.messages) == 1 and sent.messages[0]["content"] == "一言「職員07さん、15と16は法事で休み」"
    assert sent.system == reader_mod.SYSTEM and "勤務A" not in reader_mod.SYSTEM


def test_undo_takes_the_type_away(sess):
    before = symbols(sess)
    added(sess)
    assert sess.state()["can_undo"]
    r = sess.undo()
    assert r.kind == "applied" and "戻しました" in r.message
    assert symbols(sess) == before
    assert not sess.state()["can_undo"]


def test_undo_with_cells_and_rules_placed(sess):
    before, t0, rules0 = symbols(sess), sess.store.month(2026, 11).table, set(sess.store.ledger().rules_at())
    added(sess)
    sess.say(SET)
    r = sess.say(RULE)
    sess.answer(r.uid, "rule")
    assert sum(row.count("日8") for row in sess.store.month(2026, 11).table.values()) >= 1
    sess.undo()                      # 人数のルール
    assert "日8" in symbols(sess)
    sess.state()
    assert sess.store.month(2026, 11).table["小島"][9] == "日8"
    sess.undo()                      # マス
    sess.state()
    sess.undo()                      # 足す
    assert symbols(sess) == before
    assert sess.store.month(2026, 11).table == t0 and set(sess.store.ledger().rules_at()) == rules0
    st = sess.state()
    assert not any(x["symbol"] == "日8" for x in st["day_counts"] + st["symbols"])


def test_undo_keeps_the_type_while_another_month_uses_it(sess):
    added(sess)
    from shiftvoice.nextmonth import copy_next_month
    other = copy_next_month(sess.store, 2026, 11)
    other.table = {n: ["公"] * other.days for n in sess.store.month(2026, 11).table}
    other.table["小島"][0] = "日8"
    sess.store.save_month(other)
    r = sess.undo()
    assert r.kind == "ask" and "12月の表" in r.message
    assert "日8" in symbols(sess) and sess.state()["can_undo"]


def test_stays_after_reopening_and_shows_everywhere(sess):
    added(sess)
    sess.say(SET)
    root = sess.store.root
    s2 = Session(Store(root), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    st = s2.open()
    assert ["日8", "日8"] in st["shift_names"]
    assert next(x for x in st["symbols"] if x["symbol"] == "日8")["start"] == "08:00"
    row = next(x for x in st["day_counts"] if x["symbol"] == "日8")
    assert row["counts"][9] == 1 and sum(row["counts"]) == 1
    assert [x["symbol"] for x in st["day_counts"] if x["unit"] is None][-2:] == ["日8", "公"]
    line = next(x for x in table_tsv(st).splitlines() if x.startswith("小島"))
    assert line.split("\t")[10] == "日8"
    assert s2.why("小島", 10)
    # 開き直した後の一言も読める
    r = s2.say(RULE)
    assert r.sent == "平日は勤務Aを1人"


def test_rule_detail_of_the_new_shift(sess):
    added(sess)
    r = sess.say(RULE)
    r = sess.answer(r.uid, "rule")
    assert sess.rule_detail(r.rule_ids[0])


def test_interval_counts_the_new_shift():
    from pathlib import Path
    fac = json.loads((Path(__file__).resolve().parents[1] / "data/hidamari/facility.json").read_text(encoding="utf-8"))
    st = fac["shift_types"] + [{"symbol": "日8", "name": "日8", "kind": "work", "start": "08:00", "hours": 9,
                                "on_request_only": True, "spoken": True}]
    pairs = short_pairs(st, 14)
    assert pairs[("遅", "日8")] == 2       # 20時の次の朝8時で12時間
    assert ("日8", "早") not in pairs       # 17時の次の朝7時で14時間


def test_another_cell_word_replaces_a_placed_cell(sess):
    """人数のルールで置いた日8のマスを、研修のように残さない。別の頼みが当たり、別の人に移る。"""
    added(sess)
    r = sess.say(RULE)
    sess.answer(r.uid, "rule")
    m = sess.store.month(2026, 11)
    who = next(n for n, row in m.table.items() if row[9] == "日8")
    code = {s.name: f"職員{i:02d}" for i, s in enumerate(sess.store.staff(), 1)}[who]
    sess.reader = Fake({f"{code}さん、10日は休み": [{"type": "day_off", "staff": code, "days": [10], "reason": None}]})
    r = sess.say(f"{who}さん、10日は休み")
    assert r.kind == "applied" and not r.warning, (r.message, r.warning)
    m = sess.store.month(2026, 11)
    assert m.table[who][9] == "公"
    assert sum(1 for row in m.table.values() if row[9] == "日8") == 2


def test_cell_can_change_after_cancelling_the_request(sess):
    """日8の頼みを取り消してマスが残っても、別の勤務の頼みでそのマスを直せる。"""
    added(sess)
    sess.say(SET)
    sess.reader = Fake({"10日の職員11さん、勤務Aはやめて": [{"type": "cancel", "staff": "職員11", "days": [10], "shift": "勤務A"}],
                        "10日の職員11さん、公休に": [{"type": "set_cell", "staff": "職員11", "days": [10], "shift": "公", "strength": "wish", "reason": None}]})
    assert sess.say("10日の小島さん、日8はやめて").kind == "applied"
    assert sess.store.month(2026, 11).table["小島"][9] == "日8"       # 取り消しはマスを動かさない（今までと同じ）
    r = sess.say("10日の小島さん、公休に")
    assert r.kind == "applied" and not r.warning, (r.message, r.warning)
    assert sess.store.month(2026, 11).table["小島"][9] == "公"


def test_coverage_cells_do_not_block_a_count_rule(sess):
    """人数のルールで置いたマスは固定しない。回数のルールを足しても守れる。"""
    added(sess)
    r = sess.say(RULE)
    sess.answer(r.uid, "rule")
    n = sess.store.month(2026, 11).table["小島"].count("日8")
    assert n >= 2
    sess.reader = Fake({"職員11さんは勤務Aを月1回まで。毎月": [
        {"type": "count_limit", "staff": ["職員11"], "shift": "勤務A", "min": None, "max": 1, "until_day": None,
         "strength": "operational", "reason": None}]})
    r = sess.say("小島さんは日8を月1回まで。毎月")
    assert r.kind == "applied", r.message
    assert sess.store.month(2026, 11).table["小島"].count("日8") <= 1


def test_support_with_the_new_shift_is_read_at_hand(sess):
    added(sess)
    r = sess.say("12日、小島さんは東の日8に入って")
    assert r.kind == "applied" and r.sent is None, r.message
    m = sess.store.month(2026, 11)
    assert m.table["小島"][11] == "日8" and m.support == {"小島/12": "東"}
    assert m.table["小島"][7] != "日8"     # 「日8」の8を日付にしない


def test_absence_on_the_new_shift_after_distribute(sess):
    added(sess)
    sess.say(SET)
    sess.distribute()
    sess.reader = Fake({"職員11さん、10日は休み": [{"type": "day_off", "staff": "職員11", "days": [10], "reason": None}]})
    r = sess.say("小島さん、10日は休み")
    assert r.kind == "fix_offer", r.message     # 落ちない。動かせない時の言い方はふつうの勤務の頼みと同じ


def test_next_month_words_hide_the_name_too(sess):
    added(sess)

    class Seen:
        calls_claude = True
        said = None

        def read(self, masked, history=None):
            Seen.said = (masked, history)
            return [{"type": "set_cell", "staff": "職員11", "days": [10], "shift": "勤務A", "strength": "wish", "reason": None}]

    fl = NextFlow(sess.store, 2026, 11)
    v = fl.review()
    for c in v["cards"]:
        fl.answer(c["rule_id"], "archive" if c["reason"] == "expired" else "keep")
    out = fl.wish("10日の小島さん、日8に", Seen())
    assert Seen.said[0] == "10日の職員11さん、勤務Aに" and len(Seen.said[1]) == 2
    assert "日8" in out.get("wish_note", "")


def test_public_gate_lets_the_hidden_name_through():
    check_text("10日の職員11さん、勤務Aに")
    check_text("平日は勤務Bを1人")
