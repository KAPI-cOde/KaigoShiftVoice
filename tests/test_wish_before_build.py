"""作者の作り方に合わせる（2026-11-06）。全員の希望休を入れてから表を作る。
- 月が違う一言は送らずに聞き返す（11月を開いたまま「12月の15日と16日は希望休」が11月の表に入っていた）
- ［12月へ］の後、表を作る前に一言で希望休を入れ、一覧で見え、［12月の表を作る］で守られる
- 来月の希望を書いた表（名前・日・「希」）を貼ると希望休として入る
- 何も入れなければ、帳面も12月の置き場も変わらない（撮影の数を変えない）"""
import json

from shiftvoice.nextflow import NextFlow, parse_wish_paste
from shiftvoice.reader import ReadError
from shiftvoice.session import Reply

OKADA = "岡田さん、15と16は法事で休み"


class NoCall:
    calls_claude = True

    def read(self, masked, history=None):
        raise AssertionError(f"Claude へ送った {masked}")


def _ready(sess):
    fl = NextFlow(sess.store, 2026, 11)
    v = fl.review()
    for c in v["cards"]:
        fl.answer(c["rule_id"], "archive" if c["reason"] == "expired" else "keep")
    return fl


def test_other_month_is_asked_not_sent(sess):
    nov = sess.store.month(2026, 11).table
    rev = sess.store.ledger().rev
    sess.reader = NoCall()
    r = sess.say("岡田さん、12月の15日と16日は希望休")
    assert isinstance(r, Reply) and r.kind == "ask"
    assert "12月の頼みです" in r.message and "［12月へ］" in r.message and "：" not in r.message
    assert r.sent is None
    assert sess.store.month(2026, 11).table == nov and sess.store.ledger().rev == rev
    assert sess.store.pending(2026, 11) == []
    r = sess.say("岡田さん、１０月の３日は休み")       # 全角・前の月
    assert r.kind == "ask" and "いま開いているのは11月" in r.message


def test_month_from_still_goes_to_claude(sess):
    r = sess.say("杉山さん、12月から夜勤もできるって")   # 〇月から は月違いの頼みではない
    assert "12月の頼みです" not in r.message


def test_wish_before_build_is_kept(sess):
    fl = _ready(sess)
    nov = sess.store.month(2026, 11).table
    v = fl.review()
    assert v["wishes"] == [] and "岡田" in v["wish_missing"] and v["wish_month"] == 12
    w = fl.wish(OKADA, sess.reader)
    assert "wish_error" not in w, w
    assert [x["who"] for x in w["wishes"]] == [["岡田"]] and "岡田" not in w["wish_missing"]
    assert w["wish_note"].startswith("入れました")
    assert fl.wish(OKADA, sess.reader)["wish_note"] == "12月はもう入っています"   # 二重に入れない
    assert len(fl.review()["wishes"]) == 1
    # 表は作らない・11月の表もルールも変えない
    assert sess.store.month(2026, 12).table is None
    assert sess.store.month(2026, 11).table == nov
    assert all(r.meta.get("said") != OKADA for r in sess.store.ledger().rules_at(between=("2026-11-01", "2026-11-30")).values())
    # 違う月の一言は入れない
    e = fl.wish("岡田さん、11月の20日は休み", NoCall())
    assert "11月の頼みです" in e["wish_error"] and len(e["wishes"]) == 1
    b = fl.build()
    assert b["status"] == "ok", b
    for plan in ("base", "give"):
        assert b["tables"][plan]["岡田"][14:16] == ["公", "公"]


def test_wish_needs_dates(sess):
    fl = _ready(sess)
    e = fl.wish("遅番の次の日に早番はきついから、やめてあげて", sess.reader)
    assert "表を作る前に入れられるのは" in e["wish_error"] and e["wishes"] == []


def test_wish_no_key(sess):
    class NoKey:
        calls_claude = False

        def read(self, masked, history=None):
            raise ReadError("鍵なし")
    e = _ready(sess).wish(OKADA, NoKey())
    assert "貼ると、鍵なしで入れられます" in e["wish_error"]


PASTE = "\n".join([
    "12月の希望休",
    "\t".join(["名前"] + [str(d) for d in range(1, 32)]),
    "\t".join(["岡田"] + ["希" if d in (20, 21) else "" for d in range(1, 32)]),
    "\t".join(["千葉さん"] + ["希望" if d == 3 else "" for d in range(1, 32)]),
    "\t".join(["青木"] + [""] * 31),
    "\t".join(["山本"] + ["希" if d == 9 else "" for d in range(1, 32)]),
])


def test_parse_wish_paste():
    got, unknown = parse_wish_paste(PASTE, ["岡田", "千葉", "青木"], 31)
    assert got == {"岡田": [20, 21], "千葉": [3]} and unknown == ["山本"]


def test_wish_paste_is_kept_and_undone(sess):
    fl = _ready(sess)
    w = fl.wish_paste(PASTE)
    assert sorted(x["who"][0] for x in w["wishes"]) == ["千葉", "岡田"]
    assert "2人分を入れました" in w["wish_note"] and "山本" in w["wish_note"]
    rid = next(x["rule_id"] for x in w["wishes"] if x["who"] == ["千葉"])
    u = fl.unwish(rid)
    assert [x["who"] for x in u["wishes"]] == [["岡田"]] and "千葉" in u["wish_missing"]
    assert "wish_error" in fl.unwish("r001")              # 作る前に入れた頼みでない物は外さない
    b = fl.build()
    assert b["status"] == "ok"
    assert b["tables"]["base"]["岡田"][19:21] == ["公", "公"]


def test_no_wish_changes_nothing(sess):
    """希望休を入れなければ、見直しで帳面にも12月の置き場にも何も足さない（撮影の数を変えない）。"""
    fl = NextFlow(sess.store, 2026, 11)
    fl.review()
    before = (json.dumps(sess.store.ledger().to_list(), sort_keys=True),
              json.dumps(sess.store.month(2026, 12).layers, sort_keys=True))
    fl.review()
    fl.wishes()
    assert (json.dumps(sess.store.ledger().to_list(), sort_keys=True),
            json.dumps(sess.store.month(2026, 12).layers, sort_keys=True)) == before


def test_日付を決めた人数も表を作る前に入れられる(sess):
    """全部の希望休を入れると組めない時、詰まり所の日の人数を変える一言（「12日は早番を3人」）を、表を作る前に言える。
    前は「12月の表を開いてから言ってください」と断られ、表が作れない時は空ける手が無かった（作者 2026-11-07
    「その日の人数を減らすなどの対処をしつくしてから、どうしても空かない時だけ相談」）。名前は作り話。"""
    import json as _j

    from shiftvoice.reader import RecordedReader
    fl = _ready(sess)
    text = "12日は早番を3人"
    reader = RecordedReader({text: _j.dumps({"items": [{"type": "coverage", "shift": "早", "min": 3, "max": None}]})})
    w = fl.wish(text, reader)
    assert "wish_error" not in w, w
    assert w["wish_note"].startswith("入れました") and "12日" in w["wish_note"]
    assert any(x["said"] == text for x in w["wishes"])
    assert sess.store.month(2026, 12).table is None            # 表はまだ作らない
    b = fl.build()
    assert b["status"] == "ok", b
    assert sum(row[11] == "早" for row in b["tables"]["base"].values()) == 3
    # 日付の無い人数（毎月のルール）は、今までどおり表を開いてから
    reader2 = RecordedReader({"早番は毎日3人": _j.dumps({"items": [{"type": "coverage", "shift": "早", "min": 3, "max": None}]})})
    e = fl.wish("早番は毎日3人", reader2)
    assert "表を作る前に入れられるのは" in e["wish_error"]


def test_翌月の表そのものが作れない時は_次にすることを言う(sess, monkeypatch):
    """返さない案が作れない時、見出しは「この条件では返せません」ではなく「12月の表が作れませんでした」。文は、答える
    ボタンの無い「もう少し考えますか、どれを譲るかを決めますか」ではなく、詰まり所や日付の人数を一言で空けることを言う。"""
    from shiftvoice import nextflow
    from shiftvoice.build import C3_MESSAGE, TIMEOUT_MESSAGE
    from shiftvoice.giveback import GiveBack
    from shiftvoice.solver import SolveResult
    fl = _ready(sess)
    for msg, choke, want in ((TIMEOUT_MESSAGE, [{"day": 12, "text": "いちばんの詰まり所は12日です", "cells": []}], "詰まり所"),
                             (TIMEOUT_MESSAGE, [], "この時間では見つかりませんでした"),
                             (C3_MESSAGE, [], "動かせないルール")):
        monkeypatch.setattr(nextflow, "build_pair", lambda *a, _m=msg, _c=choke, **k: GiveBack(
            "base_failed", SolveResult("timeout", None), None, message=_m, base_choke=_c))
        monkeypatch.setattr(nextflow, "_choke_out", lambda c, m: c)
        b = fl.build()
        assert b["status"] == "base_failed" and b["title"] == "12月の表が作れませんでした"
        assert want in b["message"] and "もう少し考えますか" not in b["message"] and "外しますか" not in b["message"]
        assert "見直しへ戻る" in b["message"]


def test_日付の人数を言い直して前の数に戻せる(sess):
    """「12日は早番を3人」→「4人」→「3人」と言い直すと、最後の3人が「もう入っています」で入らず、4人のままだった
    （日付の人数は新しい方が勝つのに、古い3人と同じ中身だと見た。Astra 2026-11-07）。名前は作り話。"""
    import json as _j

    from shiftvoice.reader import RecordedReader
    fl = _ready(sess)
    said = {f"12日は早番を{n}人": _j.dumps({"items": [{"type": "coverage", "shift": "早", "min": n, "max": None}]})
            for n in (3, 4)}
    reader = RecordedReader(said)
    for n in (3, 4, 3):
        w = fl.wish(f"12日は早番を{n}人", reader)
        assert w.get("wish_note", "").startswith("入れました"), (n, w)
    assert fl.wish("12日は早番を3人", reader)["wish_note"] == "12月はもう入っています"   # 同じ数を続けて言った時は二重に入れない
    b = fl.build()
    assert b["status"] == "ok", b
    assert sum(row[11] == "早" for row in b["tables"]["base"].values()) == 3


def test_日付の人数の数え方を切り替えて言い直せる(sess):
    """「12日は早番を3人」（施設全体）→「12日はどのユニットも早番は0人」→「12日は早番を3人」と言い直すと、最後の3人が
    最初の3人と同じ中身と見られて入らず、各ユニット0人が残った（Astra 2026-11-07。数え方が違っても後に言った方が勝つ）"""
    import json as _j

    from shiftvoice.reader import RecordedReader
    fl = _ready(sess)
    reader = RecordedReader({
        "12日は早番を3人": _j.dumps({"items": [{"type": "coverage", "shift": "早", "min": 3, "max": None}]}),
        "12日はどのユニットも早番は0人": _j.dumps({"items": [{"type": "coverage", "shift": "早", "min": 0, "max": 0}]})})
    for text in ("12日は早番を3人", "12日はどのユニットも早番は0人", "12日は早番を3人"):
        w = fl.wish(text, reader)
        assert w.get("wish_note", "").startswith("入れました"), (text, w)
    b = fl.build()
    assert b["status"] == "ok", b
    assert sum(row[11] == "早" for row in b["tables"]["base"].values()) == 3
