"""厚生労働省の標準様式（従業者の勤務の体制及び勤務形態一覧表）の形の表を、シフト記号表と一緒に取り込む。
試験の表は作り話（公式の記載例は入れない）。作りは同じ（見出しが複数行・名前の前後の列・1人3行・兼務の2行・a〜j の記号・暦月30日）。"""
import csv
import io
from pathlib import Path

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice.interval import count_short
from shiftvoice.onboard import ParseError, create_facility, infer, parse_paste, parse_symbol_table, plan_symbols
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
