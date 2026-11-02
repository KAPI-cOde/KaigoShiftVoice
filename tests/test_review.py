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
