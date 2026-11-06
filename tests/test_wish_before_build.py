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
