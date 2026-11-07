"""新しいルールを守れず段を下げた時の知らせは、理由1つと提案1つの2文までにする（作者 2026-11-07「人間には認知的負荷が
高すぎる。『こういう理由でできなかったけれど、こうしてもいいですか？』くらいが、人間に判断できる限度」）。前は「〇〇は、
ほかのルールとぶつかって守れませんでした。運用の都合を外せる形で作りました（本人の希望は守っています）。外れたのは、A が
…、B が…」と長い一覧を赤で出していた。一覧は消さずに、画面でたたんだ「外れたルール（〇件）」の中へ移す。
施設と職員は作り話（ひだまりの郷）。録った返事で回すので鍵は要らない。"""
from __future__ import annotations

from shiftvoice.build import month_rules
from shiftvoice.rules import check_table

KOJIMA = "小島さん、今月は夜勤0で。あ、毎月じゃなくて、今月だけね"
SAITO = "斉藤さん、毎月、夜勤は2回まで"


def _say(sess, text):
    r = sess.say(text)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "once" if "今月" in text else "rule")
    return r


def _sentences(text: str) -> list[str]:
    return [s for s in text.split("。") if s.strip()]


def test_段を下げた時の知らせは理由と提案の2文で_外れの全部はたたんだ側にある(sess):
    """西の夜勤に入れる7人のうち、小島さんを今月0回、斉藤さんを毎月2回までにすると、ほかの人が夜勤の回数の上限（5回）を
    超える。運用の都合を外す段へ下がって作る"""
    sess.open()
    assert _say(sess, KOJIMA).warning == ""
    r = _say(sess, SAITO)
    assert r.kind == "applied", r.message
    # 本文は2文まで。1文目は新しいルールと、いちばんぶつかったルール（同じ夜勤を数えるルール）。2文目は作り直しの提案
    assert len(_sentences(r.warning)) <= 2, r.warning
    assert _sentences(r.warning)[0] == "「斉藤さん 夜勤は月2回まで」を守ると、「夜勤の回数（夜勤に入れる人）」が守れません", r.warning
    assert _sentences(r.warning)[1] == "全部のルールで一から作り直しますか", r.warning
    assert "外れたのは" not in r.warning and "外せる形" not in r.warning
    # たたんだ側に、段の知らせと、外れたルールの全部
    bx = r.extra["broken"]
    assert bx["rebuild"] is True
    assert bx["lead"] == "運用の都合を外せる形で作りました（本人の希望は守っています）"
    st = sess.store
    rules = month_rules(st, 2026, 11, with_premise=False)
    now = check_table(rules, st.facility(), st.staff_on(2026, 11), st.month(2026, 11), st.month(2026, 11).table)
    names = {b["name"] for b in now if b["flex"] in ("operational", "wish")}
    assert names >= {"夜勤の回数（夜勤に入れる人）", "小島さん 夜勤は月0回まで", "斉藤さん 夜勤は月2回まで"}, names
    assert bx["count"] == len(names) == len(bx["items"])
    for n in names:
        assert any(i.startswith(n + "が") or i.startswith(n + "（") for i in bx["items"]), (n, bx["items"])
    # 表は直した表のまま（戻すかどうかは人が選ぶ）。［ひとつ戻す］で斉藤さんのルールの前へ戻せる
    assert sess.state()["can_undo"]
    u = sess.undo()
    assert u.kind == "applied" and "斉藤" in u.message


def test_公開版では作り直しを勧めず_戻すことだけを言う(sess, monkeypatch):
    """公開版は重い作り直しを受けない（server.py）。受けない作り直しを勧めない"""
    monkeypatch.setenv("SHIFTVOICE_PUBLIC", "1")
    sess.open()
    _say(sess, KOJIMA)
    r = _say(sess, SAITO)
    assert len(_sentences(r.warning)) <= 2, r.warning
    assert "作り直しますか" not in r.warning and "ひとつ戻す" in r.warning
    assert r.extra["broken"]["rebuild"] is False


def test_段を下げていない時の知らせは今までどおり(sess):
    """段を下げずに新しい希望が守れなかった時（動かせない研修とぶつかる）は、今までの一文のまま。たたむ物も無い"""
    import json
    sess.say("加藤さん、21日は外の研修")
    sess.reader.replies["職員06さん、21日は休み"] = json.dumps(
        {"items": [{"type": "day_off", "staff": "職員06", "days": [21], "reason": None}]}, ensure_ascii=False)
    r = sess.say("加藤さん、21日は休み")
    assert r.warning == "加藤さんの21日は「表にある研（加藤さん 21日）」があるので、希望休を守れませんでした", r.warning
    assert "broken" not in r.extra
