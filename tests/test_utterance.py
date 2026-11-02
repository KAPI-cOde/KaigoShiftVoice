"""文字の一言 → 読み取り（録った返事）→ 札 → 狭い範囲の直し。試験 R1〜R10・L1〜L3・L7。"""
import json

import pytest

from shiftvoice.intake import Clarify, to_proposal
from shiftvoice.period import read_answer, read_period
from shiftvoice.reader import RecordedReader, ReadError
from shiftvoice.rules import check_table
from shiftvoice.session import ASK_PERIOD, Session
from shiftvoice.store import Store

from conftest import FIXED_NOW, REPLIES


def cell(s, name, day):
    return s.store.month(2026, 11).table[name][day - 1]


# 1幕
def test_act1_day_off_cells_only(sess):
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.kind == "applied"
    assert cell(sess, "岡田", 15) == "公" and cell(sess, "岡田", 16) == "公"
    card = next(c for c in sess.state()["cards"] if c["id"] in r.rule_ids)
    assert card["scope"] == "once"  # 今月だけ。画面では札にしない
    assert card["meta"]["reason"] == "法事"


def test_act1_training_and_move(sess):
    assert sess.say("加藤さん、21日は外の研修").kind == "applied"
    assert cell(sess, "加藤", 21) == "研"
    r = sess.say("上野さんの研修、13日に変わった")
    assert r.kind == "applied"
    assert cell(sess, "上野", 12) != "研" and cell(sess, "上野", 13) == "研"
    assert cell(sess, "加藤", 21) == "研"  # 前の頼みは残る


def test_r10_l3_late_early_asks_then_fixes(sess):
    """L3 期間を言っていない一言は聞く。答えるまで表と帳面に採用しない。R10 当てはまったマスが全部直る。"""
    before_table = json.dumps(sess.store.month(2026, 11).table)
    rev = sess.store.ledger().rev
    r = sess.say("遅番の次の日に早番はきついから、やめてあげて")
    assert r.kind == "ask_period" and r.message == ASK_PERIOD
    assert json.dumps(sess.store.month(2026, 11).table) == before_table
    assert sess.store.ledger().rev == rev
    assert sess.store.pending(2026, 11)[0]["status"] == "ask_period"
    r2 = sess.answer(r.uid, "毎月")
    assert r2.kind == "applied"
    st = sess.state()
    card = next(c for c in st["cards"] if c["id"] in r2.rule_ids)
    assert card["scope"] == "rule" and card["flex"] == "negotiable"
    fac, staff, m = sess.store.facility(), sess.store.staff(), sess.store.month(2026, 11)
    rule = sess.store.ledger().rules_at()[card["id"]]
    assert check_table([rule], fac, staff, m, m.table) == []
    print(f"R10 動いたマス {len(r2.changes)}（{r2.seconds:.2f} 秒）")
    assert sess.store.pending(2026, 11) == []


def test_l3_answer_once_and_unreadable(sess):
    r = sess.say("菊池さん、早番が多すぎる。月5回くらいまで")
    assert r.kind == "ask_period"
    assert sess.answer(r.uid, "うーん").kind == "ask_period"  # 読めない返事はもう一度聞く
    r2 = sess.answer(r.uid, "今月だけ")
    assert r2.kind == "applied"
    card = next(c for c in sess.state()["cards"] if c["id"] in r2.rule_ids)
    assert card["scope"] == "once"
    assert sess.store.month(2026, 11).table["菊池"].count("早") <= 5


def test_l1_l7_kojima_until_29(sess):
    """L1 札に言った言葉・誰が・いつ・理由・いつまで・期間。L7 29日までの累計で3回、30日は通常の決まりだけ。"""
    text = "小島さん、29日に試験だから、それまで夜勤3回まで"
    r = sess.say(text)
    assert r.kind == "applied"  # 期間を言っているので聞かない
    card = next(c for c in sess.state()["cards"] if c["id"] in r.rule_ids)
    meta = card["meta"]
    assert meta["said"] == text and meta["who"] == "作成者" and meta["at"].startswith("2026-10-28")
    assert "試験" in meta["reason"] and meta["until"] == "2026-11-29"   # 台本は「ルールに『試験』が残る」。言い方は本物の返事でぶれる
    assert card["scope"] == "rule" and card["flex"] == "operational"
    assert card["when"] == list(range(1, 30))
    row = sess.store.month(2026, 11).table["小島"]
    assert row[:29].count("夜") <= 3
    # 30日に1回加わっても、この札は破れない（境界日）
    fac, staff, m = sess.store.facility(), sess.store.staff(), sess.store.month(2026, 11)
    rule = sess.store.ledger().rules_at()[card["id"]]
    t = {k: list(v) for k, v in m.table.items()}
    t["小島"][29] = "夜"
    assert [b for b in check_table([rule], fac, staff, m, t)] == []
    # 12月には効かない
    assert card["id"] not in sess.store.ledger().rules_at(between=("2026-12-01", "2026-12-31"))


def test_act1_staff_info_no_table_change(sess):
    before = json.dumps(sess.store.month(2026, 11).table)
    r = sess.say("杉山さん、12月から夜勤もできるって")
    assert r.kind == "applied" and r.changes == []
    assert json.dumps(sess.store.month(2026, 11).table) == before
    s = next(x for x in sess.store.staff() if x.name == "杉山")
    assert s.can_on("2026-11") == ["早", "日", "遅"]
    assert "夜" in s.can_on("2026-12")


def test_act1_tamura_wish(sess):
    assert sess.say("田村さん、23日の祝日は日勤だと助かるって").kind == "applied"
    assert cell(sess, "田村", 23) == "日"


# R9・保存
def test_r9_no_names_sent(sess):
    texts = ["岡田さん、15と16は法事で休み", "小島さん、29日に試験だから、それまで夜勤3回まで",
             "杉山さん、12月から夜勤もできるって"]
    for t in texts:
        sess.say(t)
    sent = json.dumps([s.__dict__ for s in sess.reader.sent], ensure_ascii=False)
    for s in sess.store.staff():
        assert s.name not in sent, s.name
        if s.call:
            assert s.call not in sent
    for u in sess.store.facility().units:
        assert f"{u}ユニット" not in sent


def test_l2_restart_keeps_cards_and_table(sess):
    sess.say("岡田さん、15と16は法事で休み")
    r = sess.say("遅番の次の日に早番はきついから、やめてあげて")  # 未反映のまま閉じる
    s2 = Session(Store(sess.store.root), 2026, 11, RecordedReader({}), now=lambda: FIXED_NOW)
    st = s2.open()
    assert st["table"] == sess.state()["table"]
    assert [c["id"] for c in st["cards"]] == [c["id"] for c in sess.state()["cards"]]
    assert [u["id"] for u in st["pending"]] == [r.uid]  # 未反映の発言が残る
    # 再起動の後も、AI を呼び直さずに答えられる（読んだ結果を残している）
    assert s2.answer(r.uid, "毎月").kind == "applied"


def test_failed_read_keeps_everything_and_retry(sess):
    before = json.dumps(sess.store.month(2026, 11).table)
    good = sess.reader
    sess.reader = RecordedReader({})
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.kind == "error" and "変えていません" in r.message
    assert json.dumps(sess.store.month(2026, 11).table) == before
    assert sess.store.pending(2026, 11)[0]["status"] == "failed"
    sess.reader = good
    assert sess.retry(r.uid).kind == "applied"
    n = sess.store.ledger().rev
    assert sess.retry(r.uid).kind == "error"  # 済んだ発言は二度足さない
    assert sess.store.ledger().rev == n


def test_recover_after_ledger_written(sess):
    """帳面は書けたが、月の表を書く前に止まった。開き直すと帳面から表を直し直す。"""
    import copy
    store = sess.store
    m0 = copy.deepcopy(store.month(2026, 11))
    r = sess.say("岡田さん、15と16は法事で休み")
    pend = {"id": r.uid, "text": "岡田さん、15と16は法事で休み", "status": "read"}
    store.save_month(m0)  # 月の表を書く前に止まった形に戻す
    from shiftvoice.store import atomic_write_json
    atomic_write_json(store._pending_path(2026, 11), [pend])
    st = Session(Store(store.root), 2026, 11, RecordedReader({}), now=lambda: FIXED_NOW).open()
    assert st["table"]["岡田"][14] == "公" and st["pending"] == []


# R1〜R8（型への当てはめ。録った返事と手元の補正）
@pytest.fixture
def ctx(store):
    return store.facility(), store.staff(), store.month(2026, 11)


def prop(sess, text):
    masked, _ = __import__("shiftvoice.names", fromlist=["Masker"]).Masker(
        sess.store.staff(), sess.store.facility()).mask(text)
    items = sess.reader.read(masked)
    from shiftvoice.names import Masker
    items = Masker(sess.store.staff(), sess.store.facility()).unmask(items)
    return to_proposal(items, text, sess.store.facility(), sess.store.staff(), sess.store.month(2026, 11))


def test_r1_day_off_with_restatement(sess):
    p = prop(sess, "久保さん、14日、あ、違う、18日は休みで")
    assert p.kind == "cells" and p.rules[0]["when"] == [18] and p.rules[0]["params"] == {"in": ["公"]}


def test_r2_sequence_and_r3_count(sess):
    p = prop(sess, "遅番の次の日に早番はきついから、やめてあげて")
    assert p.rules[0]["kind"] == "sequence" and p.scope is None
    p = prop(sess, "斉藤さん、毎月、夜勤は2回まで")
    assert p.rules[0]["params"] == {"shifts": ["夜"], "max": 2} and p.scope == "rule"


def test_r4_weekday(sess):
    p = prop(sess, "石田さん、木曜は早番にしないで。毎月")
    assert p.rules[0]["kind"] == "weekday" and p.rules[0]["params"]["not_in"] == ["早"]
    assert p.rules[0]["params"]["weekdays"] == ["thu"]


def test_r5_named_cell(sess):
    p = prop(sess, "篠原さん、20日は遅番で")
    assert p.rules[0]["who"] == ["篠原"] and p.rules[0]["params"] == {"in": ["遅"]}


def test_r6_zero(sess):
    p = prop(sess, "小川さん、今月は夜勤ゼロで")
    assert p.rules[0]["params"]["max"] == 0 and p.scope == "once"


def test_r7_not_night(sess):
    p = prop(sess, "落合さん、19日は夜勤じゃなくして")
    assert p.rules[0]["params"] == {"not_in": ["夜"]}


def test_r8_no_type_asks_back(sess):
    for t in ("よろしくね", "あれをこう、いい感じに"):
        with pytest.raises(Clarify):
            prop(sess, t)
    r = sess.say("あれをこう、いい感じに")
    assert r.kind == "ask"


def test_unknown_name_not_sent(sess):
    n = len(sess.reader.sent)
    r = sess.say("山本さん、20日休み")
    assert r.kind == "ask" and "名簿" in r.message
    assert len(sess.reader.sent) == n


def test_period_words():
    assert read_period("毎月、夜勤は2回まで") == "rule"
    assert read_period("今月は夜勤0で") == "once"
    assert read_period("毎月じゃなくて今回だけ") == "once"
    assert read_period("来月から元に戻して") == "once"
    assert read_period("今月から早番は5回まで") == "rule"
    assert read_period("今月の希望休を入れて") is None
    assert read_period("早番は5回くらいまで") is None
    assert read_answer("毎月") == "rule" and read_answer("今月だけ") == "once"
    assert read_answer("毎月じゃなくて") == "once" and read_answer("えっと") is None


def test_undo_restores_table_and_card(sess):
    t0 = json.dumps(sess.store.month(2026, 11).table)
    r = sess.say("岡田さん、15と16は法事で休み")
    t1 = json.dumps(sess.store.month(2026, 11).table)
    a = sess.say("遅番の次の日に早番はきついから、やめてあげて")
    r2 = sess.answer(a.uid, "毎月")
    u = sess.undo()
    assert u.kind == "applied" and "遅番" in u.message
    assert json.dumps(sess.store.month(2026, 11).table) == t1
    ids = {c["id"] for c in sess.state()["cards"]}
    assert r2.rule_ids[0] not in ids and r.rule_ids[0] in ids
    sess.undo()
    assert json.dumps(sess.store.month(2026, 11).table) == t0
    assert sess.undo().kind == "ask"


def test_period_reason_words_not_scope():
    """理由の「いつも」「ずっと」は期間ではない。聞く（Astra の指摘3）。"""
    assert read_period("いつも早番が多くてつらいから、早番は5回までにして") is None
    assert read_period("ずっと夜勤続きで疲れてるから、夜勤は3回まで") is None
    assert read_period("これからずっと夜勤は3回まで") == "rule"


def test_missed_wish_is_told(sess):
    """新しい希望が守れない時は、黙って「反映した」と言わない（Astra の指摘1）。"""
    r = sess.say("加藤さん、21日は外の研修")
    assert r.kind == "applied" and r.warning == ""
    sess.reader.replies["職員06さん、21日は休み"] = json.dumps(
        {"items": [{"type": "day_off", "staff": "職員06", "days": [21], "reason": None}]}, ensure_ascii=False)
    r2 = sess.say("加藤さん、21日は休み")   # 研修（動かせない）と休みの希望がぶつかる
    assert r2.kind == "applied"
    assert "守れませんでした" in r2.warning
    assert cell(sess, "加藤", 21) == "研"


def test_t2_12_same_request_twice(sess):
    """T2-12 同じ頼みを2回言っても、ルールは1件だけ。2回目は「もう入っています」。"""
    sess.say("岡田さん、15と16は法事で休み")
    rev = sess.store.ledger().rev
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.kind == "ask" and "もう入っています" in r.message
    assert sess.store.ledger().rev == rev
