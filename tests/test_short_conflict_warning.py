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


def test_直す前からの外れは理由に選ばない(store):
    """直す前の表でもう外れていたルールを、新しいルールとぶつかった相手として言わない（Astra 2026-11-07。同じ夜勤を数える
    ルールが前から外れていると、無関係でも「〇〇を守ると、夜勤の回数が守れません」と言っていた）。"""
    from shiftvoice.model import Rule
    from shiftvoice.session import conflict_warning
    fac = store.facility()
    new = Rule(id="t-new", ver=1, name="斉藤さん 夜勤は月2回まで", kind="count", flex="operational",
               params={"shifts": ["夜"], "max": 2}, who=["斉藤"], scope="rule")
    old = Rule(id="t-old", ver=1, name="夜勤の回数", kind="count", flex="operational",
               params={"shifts": ["夜"], "max": 5}, scope="rule")
    other = Rule(id="t-oth", ver=1, name="早番は月4回まで", kind="count", flex="operational",
                 params={"shifts": ["早"], "max": 4}, scope="rule")
    broken = [{"rule": "t-old", "key": "小島", "flex": "operational", "name": "夜勤の回数"},
              {"rule": "t-oth", "key": "青木", "flex": "operational", "name": "早番は月4回まで"}]
    w = conflict_warning([new], broken, {r.id: r for r in (new, old, other)}, fac, {("t-old", "小島")}, "作り直しますか")
    assert w == "「斉藤さん 夜勤は月2回まで」を守ると、「早番は月4回まで」が守れません。作り直しますか", w
    w = conflict_warning([new], broken[:1], {r.id: r for r in (new, old)}, fac, {("t-old", "小島")}, "作り直しますか")
    assert "夜勤の回数" not in w, w


def test_期限つきのルールは効く日だけで外れを数える(sess):
    """右の一覧の外れの数は、期限の中の日だけで数える（Astra 2026-11-07。「15日まで日勤に入れない」を守っていても、16日以降の
    日勤で赤い外れが出ていた）。"""
    sess.open()
    led = sess.store.ledger()
    led.append("add", rule={"id": "t-until", "ver": 1, "name": "小島さん 15日まで日勤に入れない", "kind": "cell",
                            "flex": "operational", "params": {"not_in": ["日"]}, "who": ["小島"], "when": None,
                            "scope": "once", "meta": {}},
               op_id="t-until", frm="2026-11-01", until="2026-11-15")
    sess.store.save_ledger(led)
    m = sess.store.month(2026, 11)
    t = {n: list(r) for n, r in m.table.items()}
    t["小島"] = ["公"] * 15 + ["日"] * 15
    m.table = t
    sess.store.save_month(m)
    cards = {c["id"]: c for c in sess.state()["cards"]}
    assert cards["t-until"]["broken"] == 0


def test_ぶつかった時のボタンの下に_押すと何が起きるかの説明がある():
    """［一から作り直す］と［ひとつ戻す］は、押す前に何が起きるか分からないと選べない。説明はボタンの並びの下に置き、
    赤い本文（2文まで）には足さない"""
    from pathlib import Path
    import shiftvoice
    html = (Path(shiftvoice.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    both = ("［一から作り直す］は、全部のルールで白紙から何回か作り、今の表より良い時だけ置き換えます（数分かかることがあります）。"
            "［ひとつ戻す］は、表とルールを1つ前に戻します。")
    undo_only = "［ひとつ戻す］は、表とルールを1つ前に戻します。"
    made = html.index('var bb = el("div", "btns brokenbtns");')
    appended = html.index("box.appendChild(bb);", made)
    assert html.count(both) == 1 and html.index(both) > appended
    # 戻すだけの文は、両方の文とは別に、ボタンの並びより後に1回ある
    assert html.index(undo_only, appended) < html.index('var det = el("details", "brokenlist");')
    assert html.count(undo_only) == 2
