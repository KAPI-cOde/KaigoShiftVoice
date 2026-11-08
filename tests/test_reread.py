"""読み直し（最初の読みが型に当てはまらずに止まった時だけ、文脈つきで Sonnet に読ませ直す）。
録った返事（replies.json の最初の読み・rereads.json の読み直し）で回す。鍵は要らない。"""
import json
import re

import pytest

from shiftvoice.reader import RecordedReader, parse_reread
from shiftvoice.session import Session

AOKI = "青木さんは有給消化。夜勤は単発で1〜3回なら手伝える。24日と25日と年末は夜勤に入れない"
IINKAI = "委員会の日は、できるだけ委員の3人を午前の勤務に。全員が無理なら2人いればいい"
NOT_YET = "この形の頼みは、まだ扱えません。別の言い方で言ってください"


def _sent_text(reader) -> str:
    return json.dumps([[s.system, s.messages] for s in reader.sent + reader.reread_sent], ensure_ascii=False)


def _roster_words(sess) -> set[str]:
    out = set()
    for s in sess.store.staff():
        out.add(s.name)
        if s.call:
            out.add(s.call)
    return out


def test_first_read_that_passes_does_not_reread(sess):
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.kind != "error"
    assert sess.reader.reread_sent == []


def test_stuck_reread_asks_only_what_is_missing(sess):
    r = sess.say(IINKAI)
    assert len(sess.reader.reread_sent) == 1
    assert r.kind == "ask" and r.message == "委員の3人は誰で、委員会は何日ですか"


def test_mixed_reread_keeps_rules_and_points_to_dates(sess):
    r = sess.say(AOKI)
    assert r.kind == "ask"
    assert r.message.startswith("青木さんの有給は何日ですか")
    assert "続けて「青木さん、24日と25日は夜勤にしない」" in r.message
    r2 = sess.say("20日")   # 聞き返しへの答え。読み直した3件のルールにつながる（Claude へは送らない）
    assert r2.kind == "ask_period"
    assert "有給20日" in r2.summary and "夜勤" in r2.summary


def test_reread_sends_no_roster_names(sess):
    sess.say(IINKAI)
    sess.say(AOKI)
    text = _sent_text(sess.reader)
    assert sess.reader.reread_sent, "読み直しを送っていない"
    leaked = [w for w in _roster_words(sess) if w in text]
    assert leaked == []
    ctx = sess.reader.reread_sent[-1].messages[0]["content"]
    assert "職員16" in ctx and "ユニット2" in ctx and "r012" in ctx   # 名簿と今のルールは番号で渡している
    assert "職員12" in ctx   # 人の名前の入ったルール（斉藤さんの水曜）は番号に伏せてある


def test_name_left_in_context_is_not_sent(sess, monkeypatch):
    real = Session._reread_context

    def leaky(self, masker, tokens):
        ctx = real(self, masker, tokens)
        ctx["rules"].append({"id": "x", "name": "岡田さんの夜勤", "kind": "count"})
        return ctx
    monkeypatch.setattr(Session, "_reread_context", leaky)
    r = sess.say(IINKAI)
    assert sess.reader.reread_sent == []
    assert r.kind == "ask" and r.message == NOT_YET   # 最初の読みの聞き返しのまま


def test_without_recorded_reread_falls_back_to_first_answer(sess):
    sess.reader.rereads = {}
    r = sess.say(IINKAI)
    assert r.kind == "ask" and r.message == NOT_YET


def test_reader_without_reread_keeps_old_behavior(sess):
    class Plain:   # 公開版の包みなど、読み直しを持たない読み手
        def __init__(self, inner):
            self.inner, self.sent = inner, inner.sent

        def read(self, masked_text, history=None):
            return self.inner.read(masked_text, history)
    inner = sess.reader
    sess.reader = Plain(inner)
    r = sess.say(IINKAI)
    assert r.kind == "ask" and r.message == NOT_YET
    assert inner.reread_sent == []


def test_reread_replace_hint_asks_to_replace(sess):
    masked = "職員16さんの夜勤、もう少し減らして"
    sess.reader = RecordedReader(
        {masked: '{"items":[{"type":"unclear","question":"何回までですか"}]}'},
        {masked: json.dumps({"items": [{"type": "count_limit", "staff": ["職員16"], "shift": "夜", "min": None,
                                        "max": 4, "until_day": None, "strength": "operational", "reason": None}],
                             "replace": ["r003", "r999"]}, ensure_ascii=False)})
    r = sess.say("千葉さんの夜勤、もう少し減らして")
    assert r.kind == "ask_period"
    r2 = sess.answer(r.uid, "rule")
    assert r2.kind == "ask_replace" and "夜勤の人数" in r2.message and "r999" not in r2.message


def test_parse_reread_reads_fenced_json_and_drops_unusable_types():
    got = parse_reread('説明です。\n```json\n{"items":[{"type":"custom","summary":"x"},'
                       '{"type":"day_off","staff":"職員01","days":[3]}],"replace":"r1","next":" "}\n```')
    assert [it["type"] for it in got["items"]] == ["day_off"]
    assert got["replace"] == [] and got["next"] is None


NO_REQUEST = "表を変える頼みが見つかりませんでした。もう一度言ってください（「使い方を教えて」で、言える一言の例が出ます）"
LATE4 = "遅番4回はやめましょう。"   # 本物の Haiku は none を返した


def test_no_request_found_rereads_and_says_how_it_read(sess):
    r = sess.say(LATE4)
    assert len(sess.reader.reread_sent) == 1
    assert r.kind == "ask_period" and r.message != NO_REQUEST
    assert "遅番は続けて3日まで" in r.summary and "と読みました" in r.summary   # 1つに決めて入れ、どう読んだかを添える
    assert [w for w in _roster_words(sess) if w in _sent_text(sess.reader)] == []
    r2 = sess.answer(r.uid, "rule")
    assert r2.kind == "applied"


def test_reread_two_choice_question_when_undecided(sess):
    sess.reader.rereads[LATE4] = json.dumps({"items": [{"type": "unclear", "question":
        "遅番を4日続けないようにしますか（「全員、遅番は続けて3日まで」）、月に4回入れないようにしますか（「全員、遅番は月3回まで」）"}]},
        ensure_ascii=False)
    r = sess.say(LATE4)
    assert r.kind == "ask" and "「全員、遅番は続けて3日まで」" in r.message and "how" not in (r.extra or {})


def test_everyone_mark_is_ignored_when_a_name_is_said(sess):
    masked = "職員16さん、遅番4回はやめましょう"
    sess.reader = RecordedReader(
        {masked: '{"items":[{"type":"none"}]}'},
        {masked: json.dumps({"items": [
            {"type": "count_limit", "staff": ["職員16"], "shift": "遅", "min": None, "max": 3, "until_day": None,
             "strength": "operational", "reason": None},
            {"type": "consecutive_limit", "staff": None, "shift": "遅", "max": 3, "strength": "negotiable",
             "reason": None, "everyone": True}]})})
    r = sess.say("千葉さん、遅番4回はやめましょう")   # 名指しの一言に、全員のルールを足さない
    assert len(sess.reader.reread_sent) == 1
    assert r.kind == "ask" and r.message.startswith("誰の頼みか読み取れませんでした")


def test_reread_that_finds_nothing_keeps_the_old_ask(sess):
    t = "ありがとう、助かりました"
    sess.reader = RecordedReader({t: '{"items":[{"type":"none"}]}'}, {t: '{"items":[{"type":"none"}]}'})
    r = sess.say(t)
    assert len(sess.reader.reread_sent) == 1
    assert r.kind == "ask" and r.message == NO_REQUEST


def test_empty_or_unreadable_first_read_is_reread(sess):
    masked = "職員16さん、早番4回はやめましょう"
    rule = json.dumps({"items": [{"type": "count_limit", "staff": ["職員16"], "shift": "早", "min": None, "max": 3,
                                  "until_day": None, "strength": "operational", "reason": None}]}, ensure_ascii=False)
    for first in ('{"items":[]}', '{"items":[{"type":"unclear"}]}'):
        sess.reader = RecordedReader({masked: first}, {masked: rule})
        r = sess.say("千葉さん、早番4回はやめましょう")
        assert len(sess.reader.reread_sent) == 1, first
        assert r.kind == "ask_period" and "早番は月3回まで" in r.summary, first


def test_rule_without_person_is_read_as_everyone_and_says_so(sess):
    r = sess.say("遅番4連続はやめよう")   # 最初の読みは人の無い連勤の上限。言い直しを求めず、全員と読んで入れる
    assert len(sess.reader.reread_sent) == 1
    assert r.kind == "ask_period"   # 毎月か今月だけかは今までどおり聞く
    assert r.summary.startswith("全員 遅番は続けて3日まで") and "全員" in r.summary.split("。", 1)[1] and "と読みました" in r.summary
    assert [w for w in _roster_words(sess) if w in _sent_text(sess.reader)] == []
    assert sess.answer(r.uid, "rule").kind == "applied"
    rule = next(x for x in sess.store.ledger().rules_at(between=sess._span()).values() if x.name == "全員 遅番は続けて3日まで")
    assert not rule.who


def test_rule_without_person_keeps_the_everyone_ask_when_reread_cannot_run(sess):
    sess.reader.rereads = {}   # 読み直しを呼べない時（鍵切れ・通信）は、全員に入れずに聞き返す
    r = sess.say("遅番4連続はやめよう")
    assert r.kind == "ask" and r.message.startswith("誰の頼みか読み取れませんでした") and "「全員」" in r.message


def test_everyone_without_how_still_says_it_read_everyone(sess):
    t = "遅番4連続はやめよう"
    sess.reader.rereads = {t: json.dumps({"items": [{"type": "consecutive_limit", "staff": None, "shift": "遅", "max": 3,
                                                     "strength": "operational", "reason": None, "everyone": True}]})}
    r = sess.say(t)
    assert r.kind == "ask_period" and "全員のルールと読みました" in r.summary


def test_day_request_without_person_is_not_reread(sess):
    t = "13日は休みにして"
    sess.reader = RecordedReader({t: '{"items":[{"type":"day_off","staff":null,"days":[13]}]}'},
                                 {t: '{"items":[{"type":"day_off","staff":null,"days":[13],"everyone":true}]}'})
    r = sess.say(t)   # 日付の頼みを全員に当てることはない
    assert sess.reader.reread_sent == []
    assert r.kind == "ask" and r.message.startswith("誰の頼みか読み取れませんでした")


def test_everyone_mark_does_not_override_a_name_said_without_roster_match(store):
    from shiftvoice.intake import Clarify, to_proposal
    with pytest.raises(Clarify):   # 名簿に無い呼び名を言った一言は、全員の印があっても全員にしない
        to_proposal([{"type": "count_limit", "shift": "夜", "max": 4, "everyone": True}], "たくくん、夜勤は月4回まで",
                    store.facility(), store.staff(), store.month(2026, 11))


def _rename_chiba_to_full_name(sess):
    """名簿の千葉を、姓と下の名前の千葉花子にする（実際の施設の名簿のように下の名前まで書いた形）。"""
    for f in sess.store.root.rglob("*.json"):
        f.write_text(f.read_text(encoding="utf-8").replace("千葉", "千葉花子"), encoding="utf-8")


def test_rule_names_are_not_sent_only_their_contents(sess):
    _rename_chiba_to_full_name(sess)
    led = sess.store.ledger()
    led.append("add", rule={"id": "r900", "name": "花子は夜勤なし（森田の希望）", "kind": "count", "flex": "wish",
                            "params": {"shifts": ["夜"], "max": 0}, "who": ["千葉花子"], "when": None, "scope": "rule"})
    sess.store.save_ledger(led)
    sess.say(LATE4)
    assert len(sess.reader.reread_sent) == 1
    text = _sent_text(sess.reader)
    assert "花子" not in text and "森田" not in text
    ctx = json.loads(sess.reader.reread_sent[-1].messages[0]["content"].split("\n")[0].removeprefix("施設の文脈 "))
    r900 = next(r for r in ctx["rules"] if r["id"] == "r900")
    assert r900["who"] == ["職員16"] and r900["params"] == {"shifts": ["夜"], "max": 0}   # 中身は番号と記号で渡す


def test_part_of_a_roster_name_left_in_what_is_sent_stops_the_reread(sess, monkeypatch):
    _rename_chiba_to_full_name(sess)
    real = Session._reread_context

    def leaky(self, masker, tokens):
        ctx = real(self, masker, tokens)
        ctx["rules"].append({"id": "x", "kind": "count", "note": "花子の夜勤"})
        return ctx
    monkeypatch.setattr(Session, "_reread_context", leaky)
    sess.say(LATE4)
    assert sess.reader.reread_sent == []


def test_given_name_without_honorific_is_not_read_as_everyone(sess):
    """下の名前だけ・敬称なしの名指しは、全員のルールにしない。名簿の下の名前と分かれば番号に伏せて送り、その人のルールになる"""
    from shiftvoice.names import Masker
    _rename_chiba_to_full_name(sess)
    t = "花子は日勤を月1回まで"
    masked = Masker(sess.store.staff(), sess.store.facility()).mask(t)[0]
    assert "花子" not in masked
    code = re.search(r"職員\d{2,3}", masked).group(0)
    rule = {"type": "count_limit", "staff": code, "shift": "日", "min": None, "max": 1, "until_day": None,
            "strength": "operational", "reason": None}
    sess.reader = RecordedReader({masked: json.dumps({"items": [rule]})},
                                 {masked: json.dumps({"items": [{**rule, "staff": None, "everyone": True}]})})
    r = sess.say(t)
    assert sess.reader.reread_sent == []
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "once")
    added = [x for x in sess.store.ledger().rules_at().values() if x.kind == "count" and x.params.get("max") == 1
             and x.params.get("shifts") == ["日"]]
    assert added and all(x.who == ["千葉花子"] for x in added)
