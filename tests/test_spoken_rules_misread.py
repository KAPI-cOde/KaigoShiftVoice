"""話して足すルールの読み違い（2026-11-07 夜、実際の施設の表と同じ条件を画面の口で作った通しの試しで見つかった物）。
作り話の2ユニット10人の施設（tests/fixtures/pastes/excel_nights_no_ake.tsv を取り込んだ12月）で、録った形の返事で確かめる。
API は呼ばない。直す前は、どれも黙って違う中身で入った（表を大きく崩した物もある）。"""
import json
from pathlib import Path

import pytest

from shiftvoice.model import Rule
from shiftvoice.names import Masker
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.rules import with_overrides
from shiftvoice.session import Session
from shiftvoice.store import Store

PASTE = (Path(__file__).parent / "fixtures" / "pastes" / "excel_nights_no_ake.tsv").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    out = tmp_path_factory.mktemp("spoken") / "f"
    c = create_facility(infer(parse_paste(PASTE), 2026, 11, {}), out, name="試験", next_year=2026, next_month=12)
    assert c.next_table_made
    return out


def _say(root: Path, tmp_path: Path, text: str, items: list[dict]):
    import shutil
    dst = tmp_path / "f"
    shutil.copytree(root, dst)
    store = Store(dst)
    masker = Masker(store.staff(), store.facility())
    items = [{k: (masker.to_code[v] if k == "staff" and isinstance(v, str) and v in masker.to_code else v)
              for k, v in it.items()} for it in items]
    s = Session(store, 2026, 12, RecordedReader({masker.mask(text)[0]: json.dumps({"items": items}, ensure_ascii=False)}))
    s.open()
    before = set(store.ledger().rules_at())
    r = s.say(text)
    if r.kind == "ask_period":
        r = s.answer(r.uid, "rule")
    if r.kind == "ask_replace":   # 前からある同じ所のルール（連勤の上限など）と別に足す
        r = s.answer(r.uid, "add")
    new = [x for k, x in store.ledger().rules_at().items() if k not in before]
    return r, new


def test_勤務を言った続けないは_その勤務の続く日の上限(made, tmp_path):
    """「全員、早番を3日続けて入れないで」が「全員 連勤は2日まで」になり、124か所破れた"""
    r, new = _say(made, tmp_path, "全員、早番を3日続けて入れないで", [{"type": "consecutive_limit", "staff": None, "max": 2}])
    assert r.kind == "applied", r.message
    assert [(x.kind, x.params, x.name) for x in new] == [("max_run", {"shifts": ["早"], "max": 2}, "全員 早番は続けて2日まで")]


def test_夜勤を2日続けないは夜勤の上限1(made, tmp_path):
    r, new = _say(made, tmp_path, "青木さん、夜勤を2日続けないで", [{"type": "consecutive_limit", "staff": "青木", "max": 1}])
    assert r.kind == "applied", r.message
    assert [(x.params, x.who, x.name) for x in new] == [({"shifts": ["夜"], "max": 1}, ["青木"], "青木さん 夜勤を2日続けない")]


def test_連勤はこれまでどおり(made, tmp_path):
    r, new = _say(made, tmp_path, "全員、連勤は4日まで", [{"type": "consecutive_limit", "staff": None, "max": 4}])
    assert r.kind == "applied" and [x.params for x in new] == [{"shifts": "work", "max": 4}]


def test_有給の日数はちょうど(made, tmp_path):
    """「有給を1日」が下限だけになり、表を作る計算が有給を埋め草に使って8日になった"""
    r, new = _say(made, tmp_path, "青木さん、有給を1日", [{"type": "leave_count", "staff": "青木", "count": 1}])
    assert r.kind == "applied", r.message
    assert [x.params for x in new] == [{"shifts": ["有"], "min": 1, "max": 1}]
    assert Store(tmp_path / "f").month(2026, 12).table["青木"].count("有") == 1


def test_有給の多くてもは上限(made, tmp_path):
    """「有給は多くても月2日まで」が下限2日（日付は任せる）になり、有給が増えた"""
    r, new = _say(made, tmp_path, "青木さん、有給は多くても月2日まで", [{"type": "leave_count", "staff": "青木", "count": 2}])
    assert r.kind == "applied", r.message
    assert [(x.params, x.name) for x in new] == [({"shifts": ["有"], "max": 2}, "青木さん 有給は月2日まで")]


def test_日付を決めた人数は毎日にせず聞き返す(made, tmp_path):
    """「28日は行事なので、早番を5人にして」が毎日の早番5人になった"""
    r, new = _say(made, tmp_path, "28日は行事なので、早番を5人にして", [{"type": "coverage", "shift": "早", "min": 5, "max": None}])
    assert r.kind != "applied" and "日付を決めた人数" in (r.message or "")
    assert new == []


def test_できればは配慮の強さ(made, tmp_path):
    """「日勤はできれば毎日1人」が本人の希望の強さ（運用の都合より強い）で入り、ほかの決まりを押しのけた"""
    r, new = _say(made, tmp_path, "日勤はできれば毎日1人", [{"type": "coverage", "shift": "日", "min": 1, "max": None,
                                                         "strength": "wish"}])
    assert r.kind == "applied", r.message
    assert [x.flex for x in new] == ["negotiable"]


def test_合わせての回数は1本(made, tmp_path):
    """「日勤と遅番は合わせて月8回まで」が日勤8回・遅番8回の2本になった"""
    items = [{"type": "count_limit", "staff": ["青木"], "shift": "日", "min": None, "max": 8},
             {"type": "count_limit", "staff": ["青木"], "shift": "遅", "min": None, "max": 8}]
    r, new = _say(made, tmp_path, "青木さん、日勤と遅番は合わせて月8回まで", items)
    assert r.kind == "applied", r.message
    assert [(x.params, x.name) for x in new] == [({"shifts": ["日", "遅"], "max": 8}, "青木さん 日勤と遅番は合わせて月8回まで")]


def test_いつも施設全体の数を言った人数は施設全体(made, tmp_path):
    """夜勤は施設全体でいつも2人（ユニットでは1人）。「夜勤は毎日ちょうど2人」がユニットごと2人になり、35か所破れた"""
    r, new = _say(made, tmp_path, "夜勤は毎日ちょうど2人", [{"type": "coverage", "shift": "夜", "min": 2, "max": 2}])
    assert r.kind == "applied", r.message
    assert [(x.params["scope"], x.params["n"]) for x in new] == [("all", 2)]


def test_後から言った全ユニット向けの人数に_前の名指しのユニットが譲る():
    """取り込みで作った「U1・U3は夜勤2人まで」（名指し）が、後から言った「どのユニットも夜勤は1人まで」に勝ち、新しいルールが
    U1・U3 に効かなかった。新しい方が厳しい時は両方効き（名指しの2人までと、全ユニットの1人まで）、ゆるい時は名指しが譲る"""
    named = Rule("r009", 1, "夜勤の人数（U1・U3の上限）", "coverage", "negotiable",
                 {"shifts": ["夜"], "scope": "each_unit", "op": "<=", "n": 2, "units": ["U1", "U3"]})
    strict = Rule("n001", 1, "どのユニットも夜勤は1人まで", "coverage", "operational",
                  {"shifts": ["夜"], "scope": "each_unit", "op": "<=", "n": 1})
    eff = {r.id: r for r in with_overrides([named, strict])}
    assert not eff["n001"].meta.get("skip_max") and not eff["r009"].meta.get("skip_max")
    loose = Rule("n002", 1, "どのユニットも夜勤は3人まで", "coverage", "operational",
                 {"shifts": ["夜"], "scope": "each_unit", "op": "<=", "n": 3})
    eff = {r.id: r for r in with_overrides([named, loose])}
    assert eff["r009"].meta.get("skip_max") == ["U1", "U3"] and not eff["n002"].meta.get("skip_max")
    # 名指しの方が新しい時は、これまでどおり名指しが勝つ
    eff = {r.id: r for r in with_overrides([strict, named])}
    assert eff["n001"].meta.get("skip_max") == ["U1", "U3"] and not eff["r009"].meta.get("skip_max")
