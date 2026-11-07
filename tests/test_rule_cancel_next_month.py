"""言い回し150件の試し（2026-11-08、本物の Claude）で、型で表せずに止まった・違った4件。返事は本物の Haiku で録った物。

- 曜日・回数のルールを一言で外す「斉藤さんの水曜の早番なしのルール、もういらない」「遅番の回数のルール、消して」。
  前は取り消しがマスの頼みだけで「何日の早番を取り消しますか」で止まった。確かめてから帳面から外し、［ひとつ戻す］で戻る
- 「来月から日勤は土日3人」。前は開いている月（11月）から効き、11月の表も直した。始まりを翌月1日にする
- 「祝日は日勤2人でいい」。祝日だけの人数は型で表せない。毎日のルールでもう足りていればそう言い、足りなければ言い直し方を示す
"""
import json

from shiftvoice.names import Masker
from shiftvoice.reader import RecordedReader


def _reader(sess, table):
    m = Masker(sess.store.staff(), sess.store.facility())
    out = {}
    for text, items in table.items():
        masked, _ = m.mask(text)
        code = {s.name: m.to_code[s.name] for s in sess.store.staff()}
        fix = lambda v: [code.get(x, x) for x in v] if isinstance(v, list) else code.get(v, v)
        out[masked] = json.dumps({"items": [{**it, "staff": fix(it.get("staff"))} for it in items]}, ensure_ascii=False)
    return RecordedReader(out)


def _table(sess):
    return json.dumps(sess.store.month(2026, 11).table)


NOV, DEC = ("2026-11-01", "2026-11-30"), ("2026-12-01", "2026-12-31")


def test_曜日のルールを一言で外し_ひとつ戻すで戻る(sess):
    text = "斉藤さんの水曜の早番なしのルール、もういらない"
    sess.reader = _reader(sess, {text: [{"type": "cancel", "staff": ["斉藤"], "days": None, "shift": "早"}]})
    sess.open()
    b, rev = _table(sess), sess.store.ledger().rev
    r = sess.say(text)
    assert r.kind == "fix_offer", r
    assert "水曜は早番に入れない" in r.message and "外しますか" in r.message
    assert _table(sess) == b and sess.store.ledger().rev == rev   # 確かめる前は変えない
    r = sess.accept(r.uid)
    assert r.kind == "applied", r
    assert "r012" not in sess.store.ledger().rules_at()
    assert _table(sess) == b                                       # 表のマスはそのまま
    sess.undo()
    assert "r012" in sess.store.ledger().rules_at()


def test_名前の無い回数のルールを外す(sess):
    first, text = "全員、遅番は月6回まで", "遅番の回数のルール、消して"
    sess.reader = _reader(sess, {
        first: [{"type": "count_limit", "staff": None, "shift": "遅", "min": None, "max": 6, "until_day": None,
                 "strength": "operational", "reason": None}],
        text: [{"type": "cancel", "staff": None, "days": None, "shift": "遅"}]})
    sess.open()
    r = sess.say(first)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "毎月")
    assert r.kind == "applied", r
    rid = r.rule_ids[0]
    r = sess.say(text)
    assert r.kind == "fix_offer", r
    r = sess.accept(r.uid)
    rules = sess.store.ledger().rules_at()
    assert rid not in rules and "r002" in rules                    # 遅番の人数のルールは残る


def test_夜勤の上限を外すは回数のルールだけに当てる(sess):
    text = "夜勤の上限、外して"
    sess.reader = _reader(sess, {text: [{"type": "cancel", "staff": None, "days": None, "shift": "夜", "reason": None}]})
    sess.open()
    r = sess.say(text)
    assert r.kind == "fix_offer" and "夜勤の回数" in r.message, r   # 連勤の上限・夜勤の人数には当てない


def test_当てはまるルールが2つ以上なら聞き返して何も変えない(sess):
    text = "早番のルール、消して"
    sess.reader = _reader(sess, {text: [{"type": "cancel", "staff": None, "days": None, "shift": "早"}]})
    sess.open()
    b, rev = _table(sess), sess.store.ledger().rev
    r = sess.say(text)
    assert r.kind == "ask" and "どれを外すか" in r.message, r
    assert _table(sess) == b and sess.store.ledger().rev == rev


def test_日付の無いマスの取り消しは今どおりマスの頼みを確かめる(sess):
    first, text = "斉藤さん、10日は早番で", "さっきの斉藤さんの早番、やっぱりなしで"
    sess.reader = _reader(sess, {first: [{"type": "set_cell", "staff": "斉藤", "days": [10], "shift": "早"}],
                                 text: [{"type": "cancel", "staff": "斉藤", "days": None, "shift": "早"}]})
    sess.open()
    r = sess.say(first)
    assert r.kind == "applied", r
    r = sess.say(text)
    assert r.kind == "fix_offer" and "10日" in r.message, r     # 斉藤さんの水曜のルールではなく、10日の頼み


def test_来月からのルールは翌月1日から効き_今月の表は変えない(sess):
    text = "来月から日勤は土日3人"
    sess.reader = _reader(sess, {text: [{"type": "coverage", "shift": "日", "min": 3, "max": None, "weekdays": ["sat", "sun"],
                                         "holidays": False, "strength": "operational", "reason": None}]})
    sess.open()
    b = _table(sess)
    r = sess.say(text)
    assert r.kind == "applied", r
    led = sess.store.ledger()
    new = [x for x in led.rules_at().values() if x.kind == "coverage" and x.params.get("weekdays") == ["sat", "sun"]]
    assert len(new) == 1 and new[0].meta["from"] == "2026-12-01"
    assert new[0].id not in led.rules_at(between=NOV) and new[0].id in led.rules_at(between=DEC)
    assert _table(sess) == b and "12月の表を作る時から" in r.message
    sess.undo()
    assert new[0].id not in sess.store.ledger().rules_at()


def test_来月からの置き換えは前のルールを今月の終わりまで残す(sess):
    first, text = "岡田さん、遅番は月6回まで", "来月から岡田さんの遅番は月4回まで"
    item = {"type": "count_limit", "staff": ["岡田"], "shift": "遅", "min": None, "until_day": None,
            "strength": "operational", "reason": None}
    sess.reader = _reader(sess, {first: [{**item, "max": 6}], text: [{**item, "max": 4}]})
    sess.open()
    r = sess.say(first)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "毎月")
    old = r.rule_ids[0]
    r = sess.say(text)
    if r.kind == "ask_replace":
        r = sess.answer(r.uid, "置き換える")
    assert r.kind == "applied", r
    led = sess.store.ledger()
    assert old in led.rules_at(between=NOV) and old not in led.rules_at(between=DEC)
    assert r.rule_ids[0] not in led.rules_at(between=NOV) and r.rule_ids[0] in led.rules_at(between=DEC)


def test_祝日だけの人数は毎日のルールで足りていればそう言う(sess):
    text = "祝日は日勤2人でいい"
    sess.reader = _reader(sess, {text: [{"type": "coverage", "shift": "日", "min": 2, "max": None, "weekdays": None,
                                         "holidays": True, "strength": "operational", "reason": None}]})
    sess.open()
    b, rev = _table(sess), sess.store.ledger().rev
    r = sess.say(text)
    assert r.kind == "ask" and "もう入っています" in r.message and "日勤の人数" in r.message, r
    assert _table(sess) == b and sess.store.ledger().rev == rev
    assert sess.reader.reread_sent == []        # 型で表せないので読み直しても同じ。Sonnet へ送らない


def test_祝日だけの人数が足りない時は今月の祝日の日付で言い直す形を示す(sess):
    text = "祝日は日勤を3人にして"
    sess.reader = _reader(sess, {text: [{"type": "coverage", "shift": "日", "min": 3, "max": None, "weekdays": None,
                                         "holidays": True, "strength": "operational", "reason": None}]})
    sess.open()
    r = sess.say(text)
    assert r.kind == "ask" and "3日と23日は日勤3人" in r.message and "土日祝は日勤3人" in r.message, r
    assert sess.reader.reread_sent == []
