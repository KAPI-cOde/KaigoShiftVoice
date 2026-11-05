"""現場のふだんの言い方（2026-11-05、作者の書いた言い方と、別の系統の AI が書いた30件を本物の Claude Haiku で通した）。
- 「明日」「あさって」「来週の水曜」「今度の日曜」は、表の月の日付に置き換えてから送る（前は Claude が日付を読めずに聞き返した）
- 「14日の遅番、早番に変えて」を、元と先が同じ日の「移す」と読んだ時に、表が作れなくならない（前は「見つかりませんでした」）
- 名前の無い一言（「私、13日の…」）に Claude が人の欄へ「職員不明」と入れた時、「名簿にない人がいます（職員不明）」と出さない
API は呼ばない（録った形の返事で回す）。"""
import datetime as dt
import json
import shutil

from shiftvoice.period import resolve_relative_days
from shiftvoice.reader import RecordedReader
from shiftvoice.session import JST, Session
from shiftvoice.store import Store

THU = dt.date(2026, 11, 5)   # 木曜
NOW = dt.datetime(2026, 11, 5, 22, 0, tzinfo=JST)


def test_相対の日付を表の月の日付に置き換える():
    r = lambda s, today=THU: resolve_relative_days(s, today, 2026, 11)   # noqa: E731
    assert r("職員12さん、明日の日勤、有給にしてくれる？") == "職員12さん、6日の日勤、有給にしてくれる？"
    assert r("あさっての早番、遅番でお願い") == "7日の早番、遅番でお願い"
    assert r("来週の水曜、研修じゃなくて日勤") == "11日、研修じゃなくて日勤"
    assert r("今度の日曜の日勤") == "8日の日勤"
    assert r("来週の日曜日") == "15日"          # 週は月曜から
    assert r("今週の月曜") == "2日"
    assert r("次の金曜は休み") == "6日は休み"
    assert r("今度の木曜") == "12日"            # 今日が木曜なら、次の週の木曜


def test_置き換えない言い方():
    r = lambda s, today=THU: resolve_relative_days(s, today, 2026, 11)   # noqa: E731
    for s in ["遅番の次の日に早番はきついから、やめてあげて",   # 次の日＝翌日（日曜ではない）
              "職員08さんから電話。明日の30日、熱で休みたいって",   # すぐ後に日付がある
              "今日の夜、職員10さんが早く来てくれて助かった",     # 今日は置き換えない
              "明日香の件", "来週の遅番を休みにしてください",   # 名前の頭・曜日の無い来週
              "月末は休みにしといて"]:
        assert r(s) == s
    assert r("明日の早番", dt.date(2026, 11, 30)) == "明日の早番"   # 12月1日は表の月の外


def _session(built_dir, tmp_path, replies):
    root = tmp_path / "h"
    shutil.copytree(built_dir, root)
    s = Session(Store(root), 2026, 11, RecordedReader(replies), now=lambda: NOW)
    s.open()
    return s


def _reply(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def test_明日の一言を日付にして送り_表が直る(built_dir, tmp_path):
    s = _session(built_dir, tmp_path, {"職員12さん、6日の日勤、有給にしてくれる？": _reply(
        {"type": "set_cell", "staff": "職員12", "days": [6], "shift": "有", "strength": "wish", "reason": None})})
    r = s.say("斉藤さん、明日の日勤、有給にしてくれる？")
    assert r.kind == "applied", r
    assert s.store.month(2026, 11).table["斉藤"][5] == "有"
    assert s.reader.sent and "6日" in json.dumps(s.reader.sent[-1].messages, ensure_ascii=False)


def test_元と先が同じ日の移すは捨てて_同じ日の頼みで直す(built_dir, tmp_path):
    text = "職員03さん、14日の遅番、早番に変えてほしいって。"
    move = {"type": "move", "staff": "職員03", "shift": "遅", "from_day": 14, "to_day": 14, "reason": None}
    cell = {"type": "set_cell", "staff": "職員03", "days": [14], "shift": "早", "strength": "wish", "reason": None}
    s = _session(built_dir, tmp_path, {text: _reply(move, cell)})
    r = s.say("上野さん、14日の遅番、早番に変えてほしいって。")
    assert r.kind == "applied", r
    assert s.store.month(2026, 11).table["上野"][13] == "早"


def test_元と先が同じ日の移すだけで_一言に勤務の名前が無ければ_何の勤務にするか聞く(built_dir, tmp_path):
    text = "職員03さん、14日の、変えてほしいって。"
    move = {"type": "move", "staff": "職員03", "shift": "遅", "from_day": 14, "to_day": 14, "reason": None}
    s = _session(built_dir, tmp_path, {text: _reply(move)})
    before = [list(x) for x in s.store.month(2026, 11).table.values()]
    r = s.say("上野さん、14日の、変えてほしいって。")
    assert r.kind == "ask" and r.message == "上野さんの14日を、どの勤務にしますか。表もルールも変えていません"
    assert [list(x) for x in s.store.month(2026, 11).table.values()] == before


def test_移す先の無い移すだけでも_一言に出る勤務の名前が1つなら_その日をその勤務にする(built_dir, tmp_path):
    # 「14日の遅番、早番に変えて」（元と先が同じ日）と「来週の水曜、研修じゃなくて日勤になった」（先の日が無い）
    a = "職員03さん、14日の遅番、早番に変えてほしいって。"
    b = "職員14さん、11日、研修じゃなくて日勤になったって。"
    s = _session(built_dir, tmp_path, {
        a: _reply({"type": "move", "staff": "職員03", "shift": "遅", "from_day": 14, "to_day": 14, "reason": None}),
        b: _reply({"type": "move", "staff": "職員14", "shift": "研", "from_day": 11, "to_day": None,
                   "reason": "研修じゃなくて日勤になった"})})
    r = s.say("上野さん、14日の遅番、早番に変えてほしいって。")
    assert r.kind == "applied", r
    assert s.store.month(2026, 11).table["上野"][13] == "早"
    r = s.say("杉山さん、来週の水曜、研修じゃなくて日勤になったって。")
    assert r.kind == "applied", r
    assert s.store.month(2026, 11).table["杉山"][10] == "日"


def test_名前の無い一言で人の欄が埋め草なら_誰の頼みかを聞く(built_dir, tmp_path):
    text = "私、13日の日勤、有給にしてもらっていいですか。"
    s = _session(built_dir, tmp_path, {text: _reply(
        {"type": "set_cell", "staff": "職員不明", "days": [13], "shift": "有", "strength": "wish", "reason": None})})
    before = [list(x) for x in s.store.month(2026, 11).table.values()]
    r = s.say(text)
    assert r.kind == "ask" and r.message.startswith("誰の頼みか読み取れませんでした"), r
    assert "職員不明" not in r.message
    assert [list(x) for x in s.store.month(2026, 11).table.values()] == before


def test_今度の日曜はその1日だけを直し_毎週の休みにしない(built_dir, tmp_path):
    text = "職員03さん、8日の早番、公休にしてほしいって。"
    s = _session(built_dir, tmp_path, {text: _reply(
        {"type": "day_off", "staff": "職員03", "days": [8], "reason": None})})
    before = {n: list(r) for n, r in s.store.month(2026, 11).table.items()}
    weekly = {x.id for x in s.store.ledger().rules_at().values() if x.kind == "weekday"}
    assert before["上野"][7] == "早"
    r = s.say("上野さん、今度の日曜の早番、公休にしてほしいって。")
    assert r.kind == "applied", r
    after = s.store.month(2026, 11).table
    assert after["上野"][7] == "公"
    assert {x.id for x in s.store.ledger().rules_at().values() if x.kind == "weekday"} == weekly   # 毎週の休みにしない
    assert [d for d, (a, b) in enumerate(zip(before["上野"], after["上野"]), 1) if a != b and b == "公"] == [8]


def test_追加しないでと言った回数の上限は_今入っている回数にする(built_dir, tmp_path):
    """「今月はもう夜勤、追加しないでね。今入ってる分はそのままで」を上限0回と読んでも、今の夜勤を外さない。"""
    text = "職員05さん、今月はもう夜勤、追加しないでね。今入ってる分はそのままで。"
    s = _session(built_dir, tmp_path, {text: _reply(
        {"type": "count_limit", "staff": ["職員05"], "shift": "夜", "min": None, "max": 0, "until_day": None,
         "strength": "operational", "reason": None})})
    now = s.store.month(2026, 11).table["小川"].count("夜")
    assert now > 0
    r = s.say("小川さん、今月はもう夜勤、追加しないでね。今入ってる分はそのままで。")
    assert r.kind == "applied", r
    assert f"夜勤は月{now}回まで" in r.message
    assert s.store.month(2026, 11).table["小川"].count("夜") == now   # 今の夜勤を外していない
