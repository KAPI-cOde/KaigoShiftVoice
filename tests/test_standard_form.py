"""厚生労働省の標準様式（従業者の勤務の体制及び勤務形態一覧表）の形の表を、シフト記号表と一緒に取り込む。
試験の表は作り話（公式の記載例は入れない）。作りは同じ（見出しが複数行・名前の前後の列・1人3行・兼務の2行・a〜j の記号・暦月30日）。"""
import csv
import io
from pathlib import Path

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice.interval import count_short
from shiftvoice.onboard import (OnboardError, ParseError, create_facility, infer, parse_paste, parse_symbol_table,
                                plan_symbols)
from shiftvoice.reader import RecordedReader
from shiftvoice.server import _parse_setup, preview_out, table_tsv
from shiftvoice.session import Session
from shiftvoice.store import Store

FX = Path(__file__).resolve().parent / "fixtures"
HTML = Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html"


def form(name="standard_form_gh30.tsv"):
    return (FX / name).read_text(encoding="utf-8")


SYMBOLS = (FX / "standard_form_symbols.tsv").read_text(encoding="utf-8")
NAMES = ["橋本", "鈴木", "高橋", "田中", "清水", "伊藤", "中村", "小林", "吉田"]


def test_見出しから全部の列を貼っても読める_時間数の行は人にしない():
    p = parse_paste(form())
    assert list(p.table) == NAMES                       # 氏名の列（日の番号の2行上の見出し）を名前にした
    assert p.days == 30 and p.skipped == 0
    assert set(p.unknown_symbols) == {"a", "b", "c", "d", "i", "j"}
    assert all("時間" not in n and "シフト" not in n for n in p.table)


def test_兼務の2行は1人にまとめて知らせる():
    p = parse_paste(form())
    assert p.merged == ["田中"]
    rows = [c for c in csv.reader(io.StringIO(form()), delimiter="\t")]
    mine = [r[7:37] for r in rows if len(r) > 6 and r[5] == "田中" and r[6] == "シフト記号"]
    assert len(mine) == 2
    want = [(a or b or "公") for a, b in zip(*mine)]
    assert p.table["田中"] == want                         # 日ごとに、片方が空ならもう片方の記号。違えば1行目
    prop = infer(p, 2026, 11, plan_symbols(p, SYMBOLS).answers)
    assert any(t.startswith(f"{p.merged[0]}さんは2行（兼務）あったので、1人にまとめました") for t in prop.merge_notes)
    assert not any("兼務" in t for t in prop.rest_notes)   # 警告（赤）の知らせには入れない


def test_名前の重なりは標準様式でなければ今までどおり止める():
    t = "氏名\t" + "\t".join(str(i) for i in range(1, 31)) + "\n"
    t += "橋本\t" + "\t".join(["早"] * 30) + "\n" + "橋本\t" + "\t".join(["遅"] * 30) + "\n"
    with pytest.raises(ParseError, match="重なっています"):
        parse_paste(t)


def test_4週の表を暦月として貼ると様式の切り替えを知らせる():
    p = parse_paste(form("standard_form_4weeks.tsv"))
    assert p.days == 28
    with pytest.raises(ParseError) as e:
        infer(p, 2026, 11)
    assert str(e.value) == "4週（28日）の表のようです。様式を暦月に切り替えてからコピーしてください"
    infer(p, 2027, 2)                                      # 28日の月ならそのまま読める
    q = parse_paste(form())
    with pytest.raises(ParseError, match="30日分あります"):   # 28日でない表の食い違いは今までの知らせ
        infer(q, 2026, 12)


def test_記号表から記号と時刻を読む():
    t = parse_symbol_table(SYMBOLS)
    assert t["a"] == {"start": 7 * 60, "end": 16 * 60}
    assert t["i"] == {"start": 17 * 60, "end": 0} and t["j"] == {"start": 0, "end": 10 * 60}
    assert "k" not in t and "l" not in t                   # 時刻の無い行は入れない
    assert parse_symbol_table("早\t7時\t16時\n休\n公\t0:00\t0:00\n")["早"] == {"start": 420, "end": 960}
    assert parse_symbol_table("早\t7時\t16時\n休\n公\t0:00\t0:00\n")["公"] == {"rest": True}


def test_仮の答えは始まりの時刻で決まる():
    p = parse_paste(form())
    plan = plan_symbols(p, SYMBOLS)
    got = {s: a["same_as"] for s, a in plan.answers.items()}
    assert got == {"a": "早", "b": "遅", "c": "日", "d": "日", "i": "夜", "j": "明"}
    assert "a＝早番（7:00〜16:00）" in plan.lines and "i＝夜勤（17:00〜24:00）" in plan.lines
    assert "j＝明け（0:00〜10:00）" in plan.lines
    assert plan.times["早"] == {"start": "07:00", "hours": 9}
    assert plan.times["日"]["start"] == "09:00"            # 日勤は貼った月でいちばん多く出た記号（c）の時刻
    assert plan.undecided == []
    # 記号表に無い記号は決めずに、今までどおり聞く
    left = "\n".join(x for x in SYMBOLS.splitlines() if "\td\t" not in x)
    assert plan_symbols(p, left).undecided == ["d"]


def test_休みと日またぎの夜勤():
    p = parse_paste("氏名\t" + "\t".join(str(i) for i in range(1, 29)) + "\n"
                    + "橋本\t" + "\t".join(["休", "N", "休"] * 9 + ["X"]) + "\n")
    plan = plan_symbols(p, "休\t0:00\t0:00\nN\t16:30\t9:00\n")
    assert plan.answers["N"] == {"same_as": "夜"}          # 終わりが始まりより早い（日をまたぐ1記号）は夜
    assert plan.undecided == ["X"]


def made(tmp_path, symbols=SYMBOLS):
    p = parse_paste(form())
    plan = plan_symbols(p, symbols)
    prop = infer(p, 2026, 11, plan.answers)
    c = create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=12,
                        answers=plan.answers, times=plan.times)
    return p, plan, prop, c


def test_翌月の表ができて_記号表の時刻で短い並びを数える(tmp_path):
    p, plan, prop, c = made(tmp_path, SYMBOLS.replace("11:00\t～\t20:00", "13:00\t～\t22:00"))
    assert c.next_table_made and prop.problems == []
    st = {s.symbol: s for s in Store(tmp_path / "f").facility().shift_types}
    assert (st["遅"].start, st["遅"].hours) == ("13:00", 9) and (st["早"].start, st["早"].hours) == ("07:00", 9)
    line = c.short_rest["line"]
    assert line == 14 and c.short_rest["untimed"] == []
    assert c.short_rest["before"] == count_short(prop.table, [s.model_dump() if hasattr(s, "model_dump") else s.__dict__ for s in st.values()], line)[0]
    assert c.short_rest["before"] > 0                      # 遅→早（22:00 の次の 7:00）が貼った月にある
    assert c.short_rest["after"] >= 0


def test_持ち出しで施設の記号に戻る(tmp_path):
    p, plan, prop, c = made(tmp_path)
    store = Store(tmp_path / "f")
    ps = store.facility().settings["paste_symbols"]
    assert ps["夜"] == "i" and ps["明"] == "j" and ps["公"] == ""      # 夜勤の後半 j が「明」から j に戻る
    s = Session(store, 2026, 12, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    s.open()
    rows = [r.split("\t") for r in table_tsv(s.state(), ps).splitlines()][2:]
    cells = {x for r in rows[:len(NAMES)] for x in r[1:]}
    assert cells <= {"a", "b", "c", "d", "i", "j", ""}, cells        # 早・日・遅・夜・明・公のままの字は残らない
    assert {"i", "j"} & cells


def test_画面の受け口は記号表の読み取りを返す():
    data = {"text": form(), "symbols": SYMBOLS, "year": 2026, "month": 11}
    pasted, prop, _, _ = _parse_setup(data)
    out = preview_out(pasted, prop)
    assert "a＝早番（7:00〜16:00）" in out["symbol_lines"]
    assert out["suggestions"]["i"] == "same:夜" and out["suggestions"]["j"] == "same:明"
    assert sorted(out["symbol_guessed"]) == ["a", "b", "c", "d", "i", "j"]
    # 画面はおすすめをそのまま答えにして、もう一度読み取る。そこで勤務の時刻の初めの値に記号表の物が入る
    ans = {sym: {"same_as": v[5:]} for sym, v in out["suggestions"].items()}
    out = preview_out(*_parse_setup({**data, "answers": ans})[:2])
    timed = {s["symbol"]: (s["start"], s["hours"]) for s in out["shift_types"] if s["timed"]}
    assert timed["早"] == ("07:00", 9)
    assert any("兼務" in t for t in out["merge_notes"])
    plain = preview_out(*_parse_setup({"text": form(), "year": 2026, "month": 11})[:2])
    assert plain["symbol_lines"] == [] and plain["suggestions"].get("a") is None   # 記号表が無ければ今までどおり


def test_取り込みの画面に2つ目の欄がある():
    h = HTML.read_text(encoding="utf-8")
    assert "シフト記号表（様式の別のシート）を貼ると、記号と時刻を読み取ります" in h
    assert 'id = "su-symbols"' in h and "symbols: SU.symbols" in h
    assert "札" not in h[h.index("su-symbols"):h.index("su-symbols") + 900]


# ---- 2026-11-04 深夜の敵対レビュー（Opus）の直し ----

def _info(s, e):
    h = lambda t: int(t.split(":")[0]) * 60 + int(t.split(":")[1])   # noqa: E731
    return {"start": h(s), "end": h(e)}


def test_短い勤務と19時より前に終わる遅い勤務は記号表から決めない():
    from shiftvoice.onboard import _guess_target
    assert _guess_target(_info("07:00", "16:00")) == "早"
    assert _guess_target(_info("09:00", "18:00")) == "日"
    assert _guess_target(_info("11:00", "20:00")) == "遅"
    assert _guess_target(_info("10:00", "19:00")) == "遅"
    assert _guess_target(_info("10:00", "16:00")) is None   # パート（6時間）。前は遅番になり、遅番の時刻までずれた
    assert _guess_target(_info("07:00", "09:30")) is None
    assert _guess_target(_info("09:00", "15:00")) is None
    assert _guess_target(_info("10:00", "18:00")) is None   # 中番。19時より前に終わる


def test_選び直した答えで時刻と行を作る():
    legend = "記号\t始業時刻\t終業時刻\na\t7:00\t16:00\nb\t11:00\t20:00\nf\t10:00\t16:00\n"
    days = ["f"] * 10 + ["b"] * 4 + ["a"] * 6 + ["公"] * 10
    pasted = parse_paste("氏名\t" + "\t".join(str(d) for d in range(1, 31)) + "\n"
                         + "\n".join(f"{n}\t" + "\t".join(days[i:] + days[:i]) for i, n in enumerate(["上田", "森川", "北村"])))
    plan = plan_symbols(pasted, legend)
    assert "f" in plan.undecided and "f" not in plan.answers
    assert plan.times["遅"]["start"] == "11:00"                       # f が b より多く出ても、遅番の時刻は b
    assert any(x.startswith("f＝まだ決めていません") for x in plan.lines)
    chosen = plan_symbols(pasted, legend, {"f": {"name": "fの勤務", "kind": "work"}, "b": {"name": "bの勤務", "kind": "work"}})
    assert "遅" not in chosen.times                                    # 遅番にした記号が無ければ、遅番の時刻は記号表から入れない
    assert "f＝新しい勤務（10:00〜16:00）" in chosen.lines and "b＝新しい勤務（11:00〜20:00）" in chosen.lines


def test_頼まれた時だけと答えた記号は記号表の行でもそう読む():
    # 画面で［会議・研修など（頼まれた時だけ）］を選んだ記号を、記号表から読んだ行で「新しい勤務」と言っていた
    legend = "記号\t始業時刻\t終業時刻\na\t7:00\t16:00\nm\t13:00\t15:00\n"
    days = ["m"] * 2 + ["a"] * 18 + ["公"] * 10
    pasted = parse_paste("氏名\t" + "\t".join(str(d) for d in range(1, 31)) + "\n"
                         + "\n".join(f"{n}\t" + "\t".join(days[i:] + days[:i]) for i, n in enumerate(["青木", "石田", "上野"])))
    plan = plan_symbols(pasted, legend, {"m": {"name": "会議", "kind": "work", "occasional": True}})
    assert "m＝頼まれた時だけの勤務（13:00〜15:00）" in plan.lines, plan.lines


def test_ふつうの表の見出しはセルの1行目でも読む():
    # 前の版で読めた「所属（改行）(ユニット)」の見出し。改行をつないで照らすだけだと、ユニットが黙って消えた
    text = (FX / "pastes" / "19.tsv").read_text(encoding="utf-8")
    before = parse_paste(text)
    lines = text.split("\n")
    lines[0] = lines[0].replace("ユニット", '"ユニット\n(東・西)"', 1)
    after = parse_paste("\n".join(lines))
    assert after.unit_of == before.unit_of and len(set(after.unit_of.values())) >= 2


def test_様式で氏名の見出しが無ければ止める():
    text = (FX / "standard_form_gh30.tsv").read_text(encoding="utf-8").replace("(10) 氏　名", "(10) 従業者")
    with pytest.raises(ParseError, match="氏名"):
        parse_paste(text)


def test_同じ勤務にまとまる記号はそう言い時刻の出どころを残す():
    legend = "記号\t始業時刻\t終業時刻\nc\t9:00\t18:00\nd\t8:30\t17:30\na\t7:00\t16:00\n"
    days = ["c"] * 8 + ["d"] * 4 + ["a"] * 8 + ["公"] * 10
    pasted = parse_paste("氏名\t" + "\t".join(str(d) for d in range(1, 31)) + "\n"
                         + "\n".join(f"{n}\t" + "\t".join(days[i:] + days[:i]) for i, n in enumerate(["上田", "森川", "北村"])))
    plan = plan_symbols(pasted, legend)
    assert plan.time_from["日"] == "c" and plan.time_from["早"] == "a"
    assert any(x.startswith("c・d は") or x.startswith("c・dは") for x in plan.lines), plan.lines


# ---- 夜勤を日をまたいで2つの記号で書き、公休を×で書く表（新潟市の記入例の形。表と名前は作り話）----
# 16時間の夜勤を1日目「準（17:00〜24:00）」、2日目「夜（0:00〜9:00）」と書く。別名で「夜」を夜勤の始まりと読むと、
# 準→夜のつながりを短い並びに数え、翌月の表も夜勤を取り違えていた（2026-11-05）

CYCLE = ["準", "夜", "×", "早", "日1", "遅1", "日1", "×"]
NIGHT2_NAMES = ["篠原", "杉山", "田村", "千葉", "小島", "斉藤", "菊池", "落合"]
NIGHT2_SYMBOLS = ("No\t記号\t始業時刻\t終業時刻\t休憩\t勤務時間\n"
                  "1\t早\t7:00\t16:00\t1:00\t8\n2\t日1\t8:30\t17:30\t1:00\t8\n3\t遅1\t10:00\t19:00\t1:00\t8\n"
                  "4\t準\t17:00\t0:00\t0:00\t7\n5\t夜\t0:00\t9:00\t1:00\t8\n6\t公\t公休\n")


PART = ["日1", "日1", "×", "日1", "×", "日1", "日1"]   # 日勤だけの人（公休の人数が日によって変わる）


def night2_paste(days=31):
    head = "職種\t氏名\t" + "\t".join(str(d) for d in range(1, days + 1))
    rows = [f"介護職員\t{nm}\t" + "\t".join(CYCLE[(d + k) % 8] for d in range(days)) for k, nm in enumerate(NIGHT2_NAMES)]
    rows.append("介護職員\t久保\t" + "\t".join(PART[d % 7] for d in range(days)))
    return "\n".join([head] + rows) + "\n"


def screen_answer(sym, choice):
    """画面（index.html の buildAnswers）が、おすすめの選び方から作って送る答え。"""
    if choice.startswith("same:"):
        return {"same_as": choice[5:]}
    if choice == "night":
        return {"name": f"夜勤（{sym}）", "kind": "work", "next_is_ake": True}
    if choice == "rest":
        return {"same_as": "公"}
    return {"name": f"{sym}の勤務", "kind": "work"}


NIGHT2 = {"text": night2_paste(), "symbols": NIGHT2_SYMBOLS, "year": 2026, "month": 7}


def test_夜勤を準と夜で書く表は_時刻が合わない夜を別名で決めずに聞き_明けを勧める():
    out = preview_out(*_parse_setup(NIGHT2)[:2])
    assert {"準", "夜", "×"} <= set(out["unknown_symbols"])            # 「夜」も、別名で夜勤の始まりと決めずに聞く
    assert out["suggestions"]["夜"] == "same:明" and out["suggestions"]["準"] == "night"
    assert out["suggestions"]["×"] == "rest"
    assert out["suggestion_why"]["夜"] == "いつも準の翌日で、記号表では0:00〜9:00なので、明けと思われます"
    assert out["suggestion_why"]["準"] == "翌日がいつも夜で、記号表では17:00〜24:00なので、夜勤と思われます"
    assert out["suggestion_why"]["×"] == "×は休みと思われます"
    assert "夜＝明け（0:00〜9:00）" in out["symbol_lines"] and "準＝夜勤（17:00〜24:00）" in out["symbol_lines"]
    # 画面はおすすめを答えに入れて読み直す。おすすめと理由の一言は残る（画面が理由を出し続ける）
    ans = {s: screen_answer(s, v) for s, v in out["suggestions"].items()}
    again = preview_out(*_parse_setup({**NIGHT2, "answers": ans})[:2])
    assert again["suggestions"] == out["suggestions"] and again["suggestion_why"] == out["suggestion_why"]
    assert again["problems"] == [] and "夜→明け" in again["reading"] and "×→公休" in again["reading"]
    assert "準＝夜勤（17:00〜24:00）" in again["symbol_lines"]           # 夜勤（次の日は明け）と答えても「新しい勤務」と言わない
    rules = [r["name"] for r in again["rules"]]
    assert {"夜勤（準）の翌日は明け", "明けは夜勤の翌日だけ", "明けの翌日は公休"} <= set(rules)


def test_準と夜の表で作った翌月は準の翌日が明けで_持ち出すと施設の記号に戻る(tmp_path):
    out = preview_out(*_parse_setup(NIGHT2)[:2])
    ans = {s: screen_answer(s, v) for s, v in out["suggestions"].items()}
    pasted, prop, _, _ = _parse_setup({**NIGHT2, "answers": ans})
    assert prop.paste_symbols["明"] == "夜" and prop.paste_symbols["公"] == "×" and "準" not in prop.paste_symbols
    times = {s["symbol"]: {"start": s["start"], "hours": s["hours"]} for s in preview_out(pasted, prop)["shift_types"] if s["timed"]}
    c = create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=8, answers=ans, times=times)
    assert c.next_table_made, c.stage_note
    store = Store(tmp_path / "f")
    st = {s.symbol: s for s in store.facility().shift_types}
    assert st["準"].next_is_ake and "夜" not in st                     # アプリの夜勤（夜）は作らない。施設の夜は明け
    aug = store.month(2026, 8).table
    assert all(r[d + 1] == "明" for r in aug.values() for d in range(30) if r[d] == "準")
    _, by = count_short(prop.table, [s.__dict__ if not hasattr(s, "model_dump") else s.model_dump() for s in st.values()], 14)
    assert by and not any("夜勤" in k or "明け" in k for k in by), by   # 準→夜のつながりを短い並びに数えない
    assert c.short_rest["before"] == sum(by.values())


def test_時刻が合わない夜は答えないと作れず_夜勤と答えればアプリの夜勤_新しい勤務にはしない(tmp_path):
    pasted = parse_paste(night2_paste(), NIGHT2_SYMBOLS)
    assert pasted.asked == {"夜": "明"}
    base = {"準": screen_answer("準", "night"), "×": {"same_as": "公"}, "日1": {"same_as": "日"}, "遅1": {"same_as": "遅"}}
    pr = infer(pasted, 2026, 7, base)
    assert "夜" not in {s["symbol"] for s in pr.shift_types}           # 答えるまで、アプリの夜勤として黙って数えない
    with pytest.raises(OnboardError, match="夜"):
        create_facility(pr, tmp_path / "a", name="試験", next_year=2026, next_month=8, answers=base)
    # ［夜勤（次の日は明け）］と答えたら、アプリの夜勤と同じ（同じ記号の勤務を2つ作らない）
    night = infer(pasted, 2026, 7, {**base, "準": {"same_as": "公"}, "夜": screen_answer("夜", "night")})
    assert night.same_as["夜"] == "夜" and [s["symbol"] for s in night.shift_types].count("夜") == 1
    # ［新しい勤務］は選び直してもらう
    new = infer(pasted, 2026, 7, {**base, "夜": screen_answer("夜", "new")})
    assert any("『夜』はアプリの夜勤と同じ書き方なので、新しい勤務にはできません" in p for p in new.problems)
    assert [s["symbol"] for s in new.shift_types].count("夜") <= 1


def test_夜勤を1つの記号で書く記号表なら_夜は今までどおり夜勤と読む():
    cyc = ["夜", "明", "公", "早", "日"]
    text = "氏名\t" + "\t".join(str(d) for d in range(1, 31)) + "\n" + "\n".join(
        f"{nm}\t" + "\t".join(cyc[(d + k) % 5] for d in range(30)) for k, nm in enumerate(["青木", "石田", "上野", "江口", "小川"]))
    for legend in ("記号\t始業時刻\t終業時刻\n夜\t16:30\t9:30\n早\t7:00\t16:00\n",     # 日をまたぐ1つの記号
                   "記号\t始業時刻\t終業時刻\n夜\t17:00\t0:00\n明\t0:00\t9:00\n"):     # 夜が前半、明が後半
        p = parse_paste(text, legend)
        assert p.asked == {} and "夜" not in p.unknown_symbols and p.unknown_symbols == []


def test_公休をバツで書く表は休みを勧めて理由を出す_決めるのは作成者(tmp_path):
    week = ["早", "日", "遅", "×", "早", "日", "×"]
    text = "氏名\t" + "\t".join(str(d) for d in range(1, 31)) + "\n" + "\n".join(
        f"{nm}\t" + "\t".join((week * 5)[k:k + 30]) for k, nm in enumerate(["赤坂", "井上", "内田"]))
    p = parse_paste(text)
    assert p.unknown_symbols == ["×"]                                   # 別名にして黙って公休にしない
    pr = infer(p, 2026, 11)
    assert pr.suggestions == {"×": "rest"} and pr.suggestion_why == {"×": "×は休みと思われます"}
    done = infer(p, 2026, 11, {"×": screen_answer("×", "rest")})       # 画面が休みを入れて読み直した後も、同じ勧めと理由
    assert done.suggestions == {"×": "rest"} and done.suggestion_why == {"×": "×は休みと思われます"}
    assert done.same_as == {"×": "公"} and done.problems == []
    with pytest.raises(OnboardError, match="×"):
        create_facility(pr, tmp_path / "x", name="試験", next_year=2026, next_month=12)
    assert not (tmp_path / "x").exists()


def test_取り込みの画面はアプリの側の言い方で聞き_選び方ごとの翌月を短く言う():
    import re
    h = HTML.read_text(encoding="utf-8")
    assert "アプリがまだ知らない記号があります。何の勤務か選んでください" in h
    assert "アプリがまだ知らない記号が\" + pv.unknown_symbols.length" in h
    assert "（\" + why + \"）" in h and "pv.suggestion_why" in h            # おすすめのままの時、記号ごとの理由の一言を出す
    i = h.index("su-symhelp")
    helps = re.findall(r'"([^"]*＝[^"]*|休みの記号[^"]*)"', h[i:i + 700])
    assert len(helps) == 3, helps
    assert helps[0].startswith("新しい勤務＝翌月も") and "表を作る人が一言で頼んだ日だけ" in helps[1]
    assert "［休み］か［有給と同じ］" in helps[2]
    assert not any(":" in t or "：" in t or "札" in t for t in helps)
