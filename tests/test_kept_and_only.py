"""作者の指摘をサクサク通す4つ（2026-11-06 今朝の確かめ flow-check.md の場面3〜5）。

1 もう守られている指摘は、表もルールも変えずにそう言う（今月だけ）。毎月なら来月のためにルールに残す
2 声はスペースキーを押している間だけ聞く（画面の作りを見る。声のモデルなしで試せる所）
3 右のルールのカードに強さを1語
4 「〇〇さんは日勤だけ」を開いている月から効かせ、その月の表もその場で直す。ほかの人へ寄ったら1行で知らせる
"""
import json
import pathlib
import shutil

import pytest

import shiftvoice
from shiftvoice.reader import RecordedReader
from shiftvoice.session import Session
from shiftvoice.store import Store

from conftest import FIXED_NOW, REPLIES

AKE = "夜勤明けの次の日に日勤は入れないで"
AKE_READ = '{"items":[{"type":"forbid_sequence","staff":null,"first":"明","next":"日","strength":"negotiable","reason":null}]}'
RUN = "遅番の次の日に早番はきついから、やめてあげて"   # 1幕の一言。今の表で破れている


@pytest.fixture
def s(built_dir, tmp_path):
    d = tmp_path / "hidamari"
    shutil.copytree(built_dir, d)
    replies = json.loads(REPLIES.read_text(encoding="utf-8"))
    replies.update({AKE: AKE_READ})
    return Session(Store(d), 2026, 11, RecordedReader(replies), now=lambda: FIXED_NOW)


def _rules(s):
    return dict(s.store.ledger().rules_at(between=s._span()))


def _table(s):
    return json.dumps(s.store.month(2026, 11).table, ensure_ascii=False)


# 1 もう守られている指摘 -------------------------------------------------------------

def test_kept_asks_period_with_note(s):
    """明けの翌日は公休（動かせない）があるので、明け→日勤の禁止は今の表でもう守られている。聞く時にそう添える。"""
    r = s.say(AKE)
    assert r.kind == "ask_period"
    assert "今の表で、もう守られています" in r.extra.get("kept", "")


def test_kept_once_changes_nothing(s):
    rules, table = _rules(s), _table(s)
    r = s.say(AKE)
    r2 = s.answer(r.uid, "once")
    assert "今の表で、もう守られています" in r2.message
    assert "明けの次の日に日勤を置かない" in r2.message   # 何と読んだかを見せる（読み違いに気づけるように）
    assert "「明けの翌日は公休」があります" in r2.message    # どのルールで守られているか
    assert r2.changes == [] and r2.rule_ids == []
    assert _rules(s).keys() == rules.keys()               # 余分なルールが1件増えない
    assert _table(s) == table
    assert not s.store.pending(2026, 11)                   # 未反映の発言も残さない


def test_kept_every_month_keeps_rule_without_moving(s):
    """毎月と言ったら、来月からの表のために残す（作者の作り方＝指摘をルールに足して洗練する）。今の表は動かさない。"""
    table = _table(s)
    r = s.say(AKE)
    r2 = s.answer(r.uid, "rule")
    assert r2.kind == "applied" and r2.changes == [] and len(r2.rule_ids) == 1
    assert "今の表で、もう守られています" in r2.summary
    assert _table(s) == table
    assert s.undo().kind == "applied"                      # 戻せる
    assert r2.rule_ids[0] not in _rules(s)


def test_kept_only_when_a_rule_already_decides_it(built_dir, tmp_path):
    """今たまたま守られているだけのルールは「もう守られています」にしない（この後の直しを縛るので足す）。"""
    from shiftvoice.session import implied_by
    st = Store(built_dir)
    cur = list(st.ledger().rules_at(between=("2026-11-01", "2026-11-30")).values())
    seq = {"kind": "sequence", "flex": "negotiable", "params": {"if": ["明"], "next_not_in": ["日"]}, "who": ["菊池"]}
    assert implied_by(seq, cur) == "明けの翌日は公休"
    late = {"kind": "sequence", "flex": "negotiable", "params": {"if": ["遅"], "next_not_in": ["早"]}, "who": None}
    assert implied_by(late, cur) is None
    cap = {"kind": "count", "flex": "operational", "params": {"shifts": ["夜"], "max": 4}, "who": ["小川"]}
    assert implied_by(cap, cur) is None
    run = {"kind": "max_run", "flex": "operational", "params": {"shifts": "work", "max": 6}, "who": ["小島"]}
    assert implied_by(run, cur) == "連勤の上限"


def test_not_kept_still_asks_plainly(s):
    """今の表で破れている指摘（遅番の次の日に早番）は、今までどおり聞いて直す。"""
    r = s.say(RUN)
    assert r.kind == "ask_period" and not r.extra.get("kept")


# 3 強さの1語 ------------------------------------------------------------------------

def _html():
    return (pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_card_shows_strength_word():
    h = _html()
    for w in ("動かせない", "本人の希望", "運用の都合", "相談できる"):
        assert f'"{w}"' in h, w
    assert "STRENGTH[c.flex]" in h


# 2 キーを押す間だけ話す --------------------------------------------------------------

def test_push_to_talk_keys_in_html():
    h = _html()
    assert 'addEventListener("keydown"' in h and 'addEventListener("keyup"' in h
    assert "スペースを押している間、聞きます" in h
    assert "pttHeld" in h                                  # 押してすぐ離した時（マイクが開く前）も止める


# 4 日勤だけ --------------------------------------------------------------------------

def test_day_only_fixes_open_month_now(s):
    r = s.say("杉山さんは日勤だけにして")
    assert r.kind == "applied", r.message
    assert r.sent is None                                  # 手元で読めたので Claude へは送らない
    row = s.store.month(2026, 11).table["杉山"]
    assert not set(row) & {"早", "遅", "夜"}, row
    assert "日" in row and r.changes
    assert "杉山さん 11月から日勤だけ" in r.summary and "早番・遅番に入れません" in r.summary
    assert "もできる" not in r.summary
    st = next(x for x in s.store.staff() if x.name == "杉山")
    assert st.can_on("2026-11") == ["日"]


def test_day_only_undo_restores_staff(s):
    table = _table(s)
    s.say("杉山さんは日勤だけにして")
    s.undo()
    assert _table(s) == table
    st = next(x for x in s.store.staff() if x.name == "杉山")
    assert st.can_on("2026-11") == ["早", "日", "遅"]


def test_day_only_future_month_leaves_table(s):
    table = _table(s)
    r = s.say("杉山さんは12月から日勤だけにして")
    assert r.kind == "applied" and r.changes == []
    assert _table(s) == table
    assert "12月から日勤だけ" in r.summary and "12月の表を作る時から効きます" in r.summary


def test_haiku_staff_info_without_month_is_open_month(s):
    """読み取りが月なしの staff_info を返しても聞き返さず、開いている月から。"""
    s.reader.replies["職員14さんは日勤専属でお願いしますね、ずっと"] = (
        '{"items":[{"type":"staff_info","staff":"職員14","from_month":null,"can_add":[],"can_remove":["早","遅"]}]}')
    r = s.say("杉山さんは日勤専属でお願いしますね、ずっと")
    assert r.kind == "applied", r.message
    assert "11月から日勤だけ" in r.summary


def test_shift_note_names_who_carries_more():
    from shiftvoice.session import shift_note
    before = {"杉山": list("早早遅遅日日"), "久保": list("遅日日日日日"), "菊池": list("日日日日日日")}
    after = {"杉山": list("日日日日日日"), "久保": list("遅遅遅遅日日"), "菊池": list("早早日遅日日")}
    note = shift_note(before, after, "杉山", {"早": "早番", "遅": "遅番"})
    assert note == "久保さんの遅番が1回から4回になります"
    assert shift_note(before, before, "杉山", {"早": "早番", "遅": "遅番"}) == ""
