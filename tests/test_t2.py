"""T2 言い換えと失敗。優先の10件（T2-02・03・04・06・07・10・12・14・16・19）。T2-12 は test_utterance.py にある。

どの件も共通で、動かせない決まり（人数・公休9日・夜勤の並び）の破れ0、本人の希望の外れが前より増えない
（T2-03 の取り消しだけは、取り消した希望が減る）、言われていない人のマスを黙って動かさないことを確かめる。
"""
import copy
import json

from shiftvoice.build import month_rules
from shiftvoice.reader import RecordedReader
from shiftvoice.rules import check_table
from shiftvoice.session import ASK_PERIOD, Session
from shiftvoice.store import Store, atomic_write_json

from conftest import FIXED_NOW


def snap(s):
    st = s.store
    m = st.month(2026, 11)
    rules = month_rules(st, 2026, 11, with_premise=False)
    br = check_table(rules, st.facility(), st.staff_on(2026, 11), m, m.table)
    return {"table": copy.deepcopy(m.table), "rev": st.ledger().rev,
            "fixed": [b for b in br if b["flex"] == "fixed"],
            "wish": [b for b in br if b["flex"] == "wish"]}


def moved(a, b):
    return {n for n in a["table"] if a["table"][n] != b["table"][n]}


def common(before, after, *, people=None, wish_may_drop=False):
    assert after["fixed"] == [], after["fixed"]
    if not wish_may_drop:
        assert len(after["wish"]) <= len(before["wish"]), after["wish"]
    if people is not None:
        assert moved(before, after) <= set(people), moved(before, after)


def unchanged(before, after):
    assert after["table"] == before["table"] and after["rev"] == before["rev"]


def test_t2_02_misheard_name(sess):
    """聞き違いの名前（尾島→小島）。候補を手元の名簿から出し、選ぶまで表は動かない。AI へ名前を送らない。"""
    b = snap(sess)
    r = sess.say("尾島さん、29日まで夜勤3回までにして")
    assert r.kind == "ask" and r.message == "尾島さんは名簿にいません。小島さんですか"
    assert r.extra["ask_name"]["candidates"] == ["小島"]
    assert sess.reader.sent == []          # 選ぶ前は何も送らない
    unchanged(b, snap(sess))
    r = sess.choose_name(r.uid, "尾島", "小島")
    assert r.kind == "applied", r
    sent = json.dumps([x.messages for x in sess.reader.sent], ensure_ascii=False)
    assert "小島" not in sent and "尾島" not in sent and "職員11" in sent
    a = snap(sess)
    assert sum(x == "夜" for x in a["table"]["小島"][:29]) <= 3
    common(b, a)


def test_t2_02_no_candidate_shows_roster(sess):
    r = sess.say("森田さん、20日お休みしたいって")
    assert r.kind == "ask" and "名簿から選んでください" in r.message
    assert len(r.extra["ask_name"]["roster"]) == 16


def test_t2_02_same_reading_name(sess):
    """声の聞き違いで、同じ読みの別の字になった名前（菊地→菊池。README の「聞き違い・読み違いの時」の写真の場面）。
    名簿の1人に絞って聞き、選ぶまで表は動かず、AI へは名前を送らない。"""
    b = snap(sess)
    r = sess.say("菊地さんの早番を4回までにして、毎月ね")
    assert r.kind == "ask" and r.message == "菊地さんは名簿にいません。菊池さんですか"
    assert r.extra["ask_name"]["candidates"] == ["菊池"]
    assert sess.reader.sent == []          # 選ぶ前は何も送らない
    unchanged(b, snap(sess))
    r = sess.choose_name(r.uid, "菊地", "菊池")
    assert r.kind == "applied", r
    sent = json.dumps([x.messages for x in sess.reader.sent], ensure_ascii=False)
    assert "菊池" not in sent and "菊地" not in sent and "職員09" in sent
    a = snap(sess)
    assert a["table"]["菊池"].count("早") <= 4
    common(b, a)


def test_t2_03_cancel_touches_nobody_else(sess):
    """取り消しは岡田さんの15日の休みの指定だけを外す。16日と加藤さんの研修は残る。表のマスは動かさない。"""
    sess.say("岡田さん、15と16は法事で休み")
    sess.say("加藤さん、21日は外の研修")
    b = snap(sess)
    r = sess.say("岡田さんの15日の休み、やっぱりいらない。あ、16日はそのままで。加藤さんは触らないで")
    assert r.kind == "applied", r
    a = snap(sess)
    assert a["table"] == b["table"]        # 15日が公休のまま残っても誤りではない（13章）
    rules = sess.store.ledger().rules_at()
    okada = [x for x in rules.values() if x.who == ["岡田"] and x.kind == "cell"]
    assert [x.when for x in okada] == [[16]]
    assert any(x.who == ["加藤"] and x.when == [21] and x.params == {"in": ["研"]} for x in rules.values())
    common(b, a, wish_may_drop=True)
    # ひとつ戻すで、取り消しの前に戻る
    sess.undo()
    assert [x.when for x in sess.store.ledger().rules_at().values()
            if x.who == ["岡田"] and x.kind == "cell"] == [[15, 16]]


def test_t2_04_until_29(sess):
    """29日までの期限。期間を言っているので聞かない。夜勤が3回までに直り、動いたマスは0でない。"""
    b = snap(sess)
    r = sess.say("小島さん、29日に試験だから、それまで夜勤3回まで")
    assert r.kind == "applied" and r.changes
    a = snap(sess)
    assert sum(x == "夜" for x in a["table"]["小島"][:29]) <= 3
    assert moved(b, a)
    common(b, a)


def test_t2_06_leave_without_days(sess):
    """日数も日付もない有給。見送らず、上限（試験の前提で斉藤さんは月2日）を下限にして2日入る。期間は聞く。"""
    led = sess.store.ledger()
    led.append("add", rule={"id": "r090", "name": "斉藤さん 有給の上限", "kind": "count", "flex": "operational",
                            "scope": "rule", "params": {"shifts": ["有"], "max": 2}, "who": ["斉藤"], "when": None},
               frm="2026-11-01", until="2026-11-30")
    sess.store.save_ledger(led)
    b = snap(sess)
    r = sess.say("斉藤さん、有給とりたいって言ってたよ。いつとは聞いてないけど")
    assert r.kind == "ask_period" and r.message == ASK_PERIOD
    unchanged(b, snap(sess))
    r = sess.answer(r.uid, "今月だけ")
    assert r.kind == "applied", r
    a = snap(sess)
    assert a["table"]["斉藤"].count("有") == 2
    common(b, a)


def test_t2_06_no_cap_asks_days(sess):
    r = sess.say("斉藤さん、有給とりたいって言ってたよ。いつとは聞いてないけど")
    assert r.kind == "ask" and r.message == "斉藤さんの有給は何日ですか"


def test_t2_07_absolute_day_shift(sess):
    """「絶対」の日勤は動かせない決まりで入り、本人の希望を黙って外した表を返さない。"""
    b = snap(sess)
    r = sess.say("田村さん、23日の祝日は絶対日勤にして。これは外せないって")
    assert r.kind == "applied", r
    rid = r.rule_ids[0]
    assert sess.store.ledger().rules_at()[rid].flex == "fixed"
    a = snap(sess)
    assert a["table"]["田村"][22] == "日"
    common(b, a)


def test_t2_10_restated_date(sess):
    """日付の言い直し。15日だけが休みで、14日は入れない。"""
    b = snap(sess)
    r = sess.say("岡田さん、法事で14日、あ、違う、15日休み")
    assert r.kind == "applied"
    a = snap(sess)
    assert a["table"]["岡田"][14] == "公"
    assert [x.when for x in sess.store.ledger().rules_at().values() if x.who == ["岡田"]] == [[15]]
    common(b, a)


def test_t2_14_cut_reply_then_retry(sess):
    """返事が途中で切れる。表も帳面も変えず、言った文を残し、同じ発言を送り直せる。"""
    text = "菊池さんの早番を4回までにして、毎月ね"
    good = sess.reader
    sess.reader = RecordedReader({"職員09さんの早番を4回までにして、毎月ね": '{"items": [{"type": "count_li'})
    b = snap(sess)
    r = sess.say(text)
    assert r.kind == "error" and "変えていません" in r.message
    unchanged(b, snap(sess))
    assert sess.store.pending(2026, 11)[0]["text"] == text
    sess.reader = good
    r = sess.retry(r.uid)
    assert r.kind == "applied", r
    a = snap(sess)
    assert a["table"]["菊池"].count("早") <= 4
    common(b, a)


def test_t2_16_closed_before_saving(sess):
    """反映の後、月の表を保存する前に閉じた。開き直すと、札と表が食い違わない。"""
    st = sess.store
    m0 = copy.deepcopy(st.month(2026, 11))
    b = snap(sess)
    r = sess.say("岡田さん、15と16は法事で休み")
    st.save_month(m0)                     # 月の表を書く前に止まった形
    atomic_write_json(st._pending_path(2026, 11), [{"id": r.uid, "text": "岡田さん、15と16は法事で休み",
                                                    "status": "read"}])
    s2 = Session(Store(st.root), 2026, 11, RecordedReader({}), now=lambda: FIXED_NOW)
    s2.open()
    a = snap(s2)
    assert a["table"]["岡田"][14] == "公" and a["table"]["岡田"][15] == "公"
    assert not [w for w in a["wish"] if "岡田" in w["name"]]
    common(b, a)


def test_t2_19_about_five_asks_period(sess):
    """「5回くらいまで」だけ。毎月か今月だけかを聞き、答えるまで表も帳面も変えない。話し言葉の答えで通る。"""
    b = snap(sess)
    r = sess.say("菊池さん、早番が多すぎる。月5回くらいまで")
    assert r.kind == "ask_period" and r.message == ASK_PERIOD
    unchanged(b, snap(sess))
    r = sess.answer(r.uid, "毎月で")
    assert r.kind == "applied", r
    rule = sess.store.ledger().rules_at()[r.rule_ids[0]]
    assert rule.scope == "rule"
    a = snap(sess)
    assert a["table"]["菊池"].count("早") <= 5
    common(b, a)


# 残り10件のうち、今の作りで確かめられる物（T2-05・08・09・18 は作っていない。18 は L6 と同じく今回は作らない）
def test_t2_01_name_not_on_roster(sess):
    """名簿にない人。黙って飛ばさず聞き返し、表は1マスも動かない。"""
    b = snap(sess)
    r = sess.say("えっと、森田さん、20日お休みしたいって。うん、20日")
    assert r.kind == "ask" and r.message.startswith("森田さんは名簿にいません")
    assert sess.reader.sent == []
    unchanged(b, snap(sess))


def test_t2_11_person_restated(sess):
    """人の言い直し。上野さんの研修が12日から13日へ移り、加藤さんの21日は動かない。"""
    b = snap(sess)
    r = sess.say("加藤さんの21日は研修……じゃない、上野さん、上野さんの研修が13日に変わったの")
    assert r.kind == "applied", r
    a = snap(sess)
    assert a["table"]["上野"][12] == "研" and a["table"]["上野"][11] != "研"
    assert a["table"]["加藤"][20] == b["table"]["加藤"][20]
    common(b, a)


def test_t2_13_no_connection(sess):
    """読み取りに繋がらない。表は動かず、言った文は残り、戻ったら送り直せる。"""
    from shiftvoice.reader import ReadError

    class Down:
        sent = []

        def read(self, masked, history=None):
            raise ReadError("通信が切れている")
    good = sess.reader
    sess.reader = Down()
    b = snap(sess)
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.kind == "error" and "変えていません" in r.message
    unchanged(b, snap(sess))
    sess.reader = good
    assert sess.retry(r.uid).kind == "applied"


def test_t2_15_after_name_question(sess):
    """聞き返しの後に言い直す。「うまく聞き取れませんでした」にならず、岡田さんの20日が休みで入る。"""
    sess.say("えっと、森田さん、20日お休みしたいって。うん、20日")
    b = snap(sess)
    r = sess.say("あ、岡田さんだった。岡田さんの20日お休み")
    assert r.kind == "applied", r
    a = snap(sess)
    assert a["table"]["岡田"][19] == "公"
    common(b, a)


def test_t2_17_closed_before_choosing(sess):
    """配った後の直しの案が出たまま閉じる。開き直すと、配った表のまま。"""
    sess.distribute()
    b = snap(sess)
    r = sess.say("石田さんから電話で、25日お休みしたいって")
    assert r.kind == "fix_offer", r
    s2 = Session(Store(sess.store.root), 2026, 11, RecordedReader({}), now=lambda: FIXED_NOW)
    s2.open()
    unchanged(b, snap(s2))


def test_t2_20_zero_this_month_only(sess):
    """「ゼロで。あ、毎月じゃなくて、今月だけね」。0回として今月だけ入り、毎月か今月だけかは聞かない。"""
    b = snap(sess)
    r = sess.say("小島さん、今月は夜勤ゼロで。あ、毎月じゃなくて、今月だけね")
    assert r.kind == "applied", r
    rule = sess.store.ledger().rules_at()[r.rule_ids[0]]
    assert rule.scope == "once" and rule.params["max"] == 0
    a = snap(sess)
    assert a["table"]["小島"].count("夜") == 0
    common(b, a)


def test_t2_05_weekday_until_december(sess):
    """12月いっぱいの曜日の頼み。11月の木曜の早番が全部なくなり、12月の写しには残り、1月には残らない。"""
    b = snap(sess)
    r = sess.say("石田さん、木曜は子どもの送りがあるから、12月いっぱい早番にしないでほしいって")
    assert r.kind == "applied", r
    a = snap(sess)
    assert [a["table"]["石田"][d - 1] for d in (5, 12, 19, 26)].count("早") == 0
    led = sess.store.ledger()
    rid = r.rule_ids[0]
    assert rid in led.rules_at(between=("2026-12-01", "2026-12-31"))
    assert rid not in led.rules_at(between=("2027-01-01", "2027-01-31"))
    assert led.rules_at()[rid].name.endswith("（12月まで）")
    common(b, a)


def test_t2_09_must_and_maybe_in_one(sess):
    """「必ず」と「できれば」が1文に。15日は外せない、16日はできれば。言っていない決まりは足さない。"""
    b = snap(sess)
    r = sess.say("岡田さん、15日は必ず休み、16日もできれば休み、あとはできるだけ今のまま")
    assert r.kind == "applied", r
    rules = sess.store.ledger().rules_at()
    new = [rules[i] for i in r.rule_ids]
    assert sorted((x.when[0], x.flex) for x in new) == [(15, "fixed"), (16, "negotiable")]
    a = snap(sess)
    assert a["table"]["岡田"][14] == "公"
    common(b, a)


def test_t2_08_maybe_shows_tradeoff(sess):
    """「できれば」の頼み。守れるかを先に試し、守れる時は代わりに動くマスを見せ、選ぶまで表を変えない。"""
    b = snap(sess)
    r = sess.say("田村さん、23日の祝日は日勤だと助かるって。できればでいいけど")
    assert r.kind == "fix_offer" and "は守れます。代わりに" in r.message, r
    unchanged(b, snap(sess))
    a = sess.accept(r.uid)
    assert a.kind == "applied"
    after = snap(sess)
    assert after["table"]["田村"][22] == "日"
    common(b, after)
    assert sess.undo().kind == "applied" and snap(sess)["table"] == b["table"]


def test_named_count_overrides_only_its_side():
    """名指しの回数のルールは、言った側だけ全員向けを上書きする（下限だけの頼みで、全員向けの上限まで外さない）。"""
    from shiftvoice.model import Rule
    from shiftvoice.rules import with_overrides
    gen = Rule(id="g", ver=1, name="夜勤の回数", kind="count", flex="operational", params={"shifts": ["夜"], "min": 3, "max": 5})
    lo = Rule(id="a", ver=1, name="小川 4回以上", kind="count", flex="operational", params={"shifts": ["夜"], "min": 4}, who=["小川"])
    zero = Rule(id="b", ver=1, name="小島 0回まで", kind="count", flex="operational", params={"shifts": ["夜"], "max": 0}, who=["小島"])
    g = next(r for r in with_overrides([gen, lo, zero]) if r.id == "g")
    assert set(g.meta["except_min"]) == {"小川", "小島"} and g.meta["except_max"] == ["小島"]   # 上限0は下限3ともぶつかる
