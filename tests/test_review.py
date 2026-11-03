"""ルールの見直し（画面8）の土台。M1・M3・M4・M6a・M7・M9・M10・M11（画面なし）。"""
import json

import pytest

from shiftvoice.build import build_original, month_rules
from shiftvoice.ledger import LedgerError
from shiftvoice.nextmonth import copy_next_month
from shiftvoice.review import answer, answers_for, history, review_cards, undo_answer


@pytest.fixture
def nov_done(sess):
    """1幕を通した11月（撮影の12月の見直しの前提のうち、1幕の分）。"""
    sess.say("岡田さん、15と16は法事で休み")
    a = sess.say("遅番の次の日に早番はきついから、やめてあげて")
    late = sess.answer(a.uid, "毎月").rule_ids[0]
    b = sess.say("菊池さん、早番が多すぎる。月5回くらいまで")
    sess.answer(b.uid, "今月だけ")
    kojima = sess.say("小島さん、29日に試験だから、それまで夜勤3回まで").rule_ids[0]
    copy_next_month(sess.store, 2026, 11)
    return sess, late, kojima


def test_m1_m3_cards(nov_done):
    sess, late, kojima = nov_done
    v = review_cards(sess.store.ledger(), 2026, 12)
    assert [(c.rule_id, c.reason) for c in v["cards"]] == [(kojima, "expired"), (late, "added_last_month")]
    assert v["unchanged"] == 12 and v["once"] == 2       # 岡田さんの休み・菊池さんの今月だけ
    ids = {r.id for r in month_rules(sess.store, 2026, 12)}
    assert kojima not in ids                             # M1 期限が来た札は12月に効かない
    assert late in ids


def test_m4_resume_and_m6a_archive(nov_done):
    sess, late, kojima = nov_done
    nov_table = json.dumps(sess.store.month(2026, 11).table)
    acc = [a.to_dict() for a in sess.store.acceptances()]
    answer(sess.store, 2026, 12, kojima, "archive")
    v = review_cards(sess.store.ledger(), 2026, 12)      # 開き直した形（帳面だけから）
    assert v["cursor"] == 1 and v["cards"][1].rule_id == late
    assert [h["rule_id"] for h in history(sess.store.ledger())] == [kojima]
    assert json.dumps(sess.store.month(2026, 11).table) == nov_table     # M6a
    assert [a.to_dict() for a in sess.store.acceptances()] == acc
    with pytest.raises(LedgerError):
        answer(sess.store, 2026, 12, kojima, "archive")  # 同じ札に二重に答えない
    answer(sess.store, 2026, 12, late, "keep")
    assert review_cards(sess.store.ledger(), 2026, 12)["cursor"] == 2


def test_m10_expired_never_applies(nov_done):
    """答えないまま・片付けた後・片付けを戻した後のどれでも、期限が過ぎた札は12月に効かない。"""
    sess, late, kojima = nov_done
    assert kojima not in {r.id for r in month_rules(sess.store, 2026, 12)}
    answer(sess.store, 2026, 12, kojima, "archive")
    assert kojima not in {r.id for r in month_rules(sess.store, 2026, 12)}
    undo_answer(sess.store, 2026, 12, kojima)
    assert kojima not in {r.id for r in month_rules(sess.store, 2026, 12)}
    assert kojima not in answers_for(sess.store.ledger(), 2026, 12)
    assert history(sess.store.ledger()) == []
    assert kojima in sess.store.ledger().rules_at()       # 戻すと札は帳面に戻る


def test_m7_extend_applies_to_december(nov_done):
    """［期間を延ばす］で12月20日まで。12月は1〜20日の累計で3回まで効き、21日からは効かない。"""
    sess, late, kojima = nov_done
    answer(sess.store, 2026, 12, kojima, "extend", new_until="2026-12-20")
    rules = {r.id: r for r in month_rules(sess.store, 2026, 12)}
    assert rules[kojima].when == list(range(1, 21))
    res = build_original(sess.store, 2026, 12)
    assert res.ok
    assert res.result.table["小島"][:20].count("夜") <= 3
    assert not [b for b in res.result.broken if b["rule"] == kojima]


def test_m11_undo_one_keeps_others(nov_done):
    sess, late, kojima = nov_done
    answer(sess.store, 2026, 12, kojima, "archive")
    answer(sess.store, 2026, 12, late, "keep")
    undo_answer(sess.store, 2026, 12, late)
    a = answers_for(sess.store.ledger(), 2026, 12)
    assert list(a) == [kojima] and a[kojima]["answer"] == "archive"


def test_m2_keep_then_not_again(nov_done):
    """M2 先月増えた札は「今月も使いますか」で出て、おすすめは付かない。［今月も使う］で札はそのまま残り、
    次の月（1月）には「先月増えた」で出ない。"""
    from shiftvoice.nextflow import card_lines
    sess, late, kojima = nov_done
    v = review_cards(sess.store.ledger(), 2026, 12)
    c = next(c for c in v["cards"] if c.rule_id == late)
    lines = card_lines(c, 12, 11)
    assert lines["lines"] == ["今月も使いますか"] and not any("おすすめ" in b[1] for b in lines["buttons"])
    answer(sess.store, 2026, 12, late, "keep")
    led = sess.store.ledger()
    assert late in led.rules_at(between=("2026-12-01", "2026-12-31"))
    jan = review_cards(led, 2027, 1)
    assert late not in [x.rule_id for x in jan["cards"]]


def test_m6b_restore_archived(nov_done):
    """片付けたルールは一覧から戻せる。言った言葉・期間は元のまま、次に表を作る時から効く（spec 画面8・計画 M6b）。"""
    from shiftvoice.review import restore
    sess, late, kojima = nov_done
    answer(sess.store, 2026, 12, late, "keep")
    led = sess.store.ledger()
    led.append("remove", rule_id=late, op_id="t-arch", meta={"kind": "archive"})
    sess.store.save_ledger(led)
    assert late not in {r.id for r in month_rules(sess.store, 2026, 12)}
    assert [h["rule_id"] for h in history(sess.store.ledger())] == [late]
    r = sess.restore(late)
    assert r.kind == "applied" and "戻しました" in r.summary
    assert late in {r.id for r in month_rules(sess.store, 2026, 12)}
    assert history(sess.store.ledger()) == []
    assert sess.store.ledger().rules_at()[late].meta.get("said")
    assert sess.restore(late).kind == "ask"          # もう片付けていない
    assert sess.state()["archived"] == []


def test_m6b_undo_answer_after_restore_keeps_rule(nov_done):
    """見直しで片付けた後に一覧から戻し、さらに見直しの答えを戻しても、また外れない（二重の取り消しを避ける）。"""
    sess, late, kojima = nov_done
    answer(sess.store, 2026, 12, kojima, "archive")
    sess.restore(kojima)
    undo_answer(sess.store, 2026, 12, kojima)
    assert kojima in sess.store.ledger().rules_at()


def test_m6b_restore_expired_says_not_effective(nov_done):
    """期限が来て片付けたルールを戻しても、期限が過ぎていて効かないことを言う（次から効くとは言わない）。"""
    sess, late, kojima = nov_done
    answer(sess.store, 2026, 12, kojima, "archive")
    r = sess.restore(kojima)
    assert "期限（11月29日まで）が過ぎているので、このままでは効きません" in r.summary
