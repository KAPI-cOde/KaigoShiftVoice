"""取り込んで作った施設で、一言で直す・翌々月へ進む（敵対レビュー 2026-11-03 の r5・r6・r7・r9）。

- 有給（有）の記号が無い施設でも、一言のたびに落ちない。有給の頼みは表を変えずに聞き返す
- 回数のルールは、同じ記号の組を同じ相手に数えるルールが2本ある時、新しい方が古い方に代わる
- 作る時に段を下げた施設でも、翌々月の返さない案を段の順で作って採れる
"""
import json
import shutil
from pathlib import Path

import pytest

from shiftvoice.names import Masker
from shiftvoice.nextflow import NextFlow
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.session import Session
from shiftvoice.store import Store

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"


def _lines():
    return SAMPLE.read_text(encoding="utf-8").rstrip("\n").split("\n")


def _part_row(rest_mark: str) -> str:
    """週3日だけ日勤に入るパートの人（11月1日は日曜）。"""
    row = ["野口"]
    for d in range(1, 31):
        wd = (d + 5) % 7
        row.append("日" if wd in (1, 3, 5) else rest_mark(d))
    return "\t".join(row)


@pytest.fixture(scope="module")
def plain(tmp_path_factory):
    """見本の表から作った施設。"""
    prop = infer(parse_paste(SAMPLE.read_text(encoding="utf-8")), 2026, 11)
    out = tmp_path_factory.mktemp("plain") / "f"
    c = create_facility(prop, out, name="こもれびの家（作り話）", next_year=2026, next_month=12)
    assert c.next_table_made
    return out


@pytest.fixture(scope="module")
def no_leave(plain, tmp_path_factory):
    """勤務の種類に有給（有）も研修（研）も無い施設（見本の表は有が0件。取り込みが入れない時の形）。"""
    out = tmp_path_factory.mktemp("noleave") / "f"
    shutil.copytree(plain, out)
    fj = json.loads((out / "facility.json").read_text(encoding="utf-8"))
    fj["shift_types"] = [st for st in fj["shift_types"] if st.get("kind") != "leave" and st["symbol"] not in ("有", "研")]
    (out / "facility.json").write_text(json.dumps(fj, ensure_ascii=False), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def with_part(tmp_path_factory):
    """公休の違うパートの人がいて、人ごとの公休のルール（公休の数（野口さん））がある施設。"""
    text = "\n".join(_lines() + [_part_row(lambda d: "公")]) + "\n"
    prop = infer(parse_paste(text), 2026, 11)
    assert any(r["name"] == "公休の数（野口さん）" for r in prop.rules)
    out = tmp_path_factory.mktemp("part") / "f"
    c = create_facility(prop, out, name="こもれびの家（作り話）", next_year=2026, next_month=12)
    assert c.next_table_made
    return out


@pytest.fixture(scope="module")
def lowered(plain, tmp_path_factory):
    """一言でぶつかるルールを足し、段を下げて直した施設（早番25回以上は、公休9日と連勤の上限とぶつかる）。"""
    out = tmp_path_factory.mktemp("lowered") / "f"
    shutil.copytree(plain, out)
    text = "赤坂さん、早番は毎月25回以上"
    s = _session(Store(out), {text: [{"type": "count_limit", "staff": "赤坂", "shift": "早", "min": 25}]})
    r = _say(s, text)
    assert r["kind"] == "applied" and r["warning"]
    return out


def _copy(src: Path, tmp_path: Path) -> Store:
    dst = tmp_path / "f"
    shutil.copytree(src, dst)
    return Store(dst)


def _session(store: Store, said: dict[str, list[dict]]) -> Session:
    """返事の読み手を、言う一言ごとに用意する（返事の名前は伏せた番号で書く）。"""
    masker = Masker(store.staff(), store.facility())
    replies = {}
    for text, items in said.items():
        masked = masker.mask(text)[0]
        items = [{k: (masker.to_code[v] if k == "staff" and v in masker.to_code else v) for k, v in it.items()}
                 for it in items]
        replies[masked] = json.dumps({"items": items}, ensure_ascii=False)
    s = Session(store, 2026, 12, RecordedReader(replies))
    s.open()
    return s


def _say(s: Session, text: str) -> dict:
    r = s.say(text).to_dict()
    if r["kind"] == "ask_period":
        r = s.answer(r["uid"], "rule").to_dict()
    return r


def _rest(store: Store) -> dict[str, int]:
    return {n: row.count("公") for n, row in store.month(2026, 12).table.items()}


# 1 有給の記号が無い施設

def test_有給の記号が無い施設で一言を言っても落ちない(no_leave, tmp_path):
    store = _copy(no_leave, tmp_path)
    assert not any(st.kind == "leave" for st in store.facility().shift_types)
    s = _session(store, {
        "赤坂さん、15日は休み": [{"type": "day_off", "staff": "赤坂", "days": [15]}],
        "遅番の次の日に早番はきついから、やめてあげて":
            [{"type": "forbid_sequence", "first": "遅", "next": ["早"], "strength": "negotiable"}],
        "赤坂さん、早番は月6回まで": [{"type": "count_limit", "staff": "赤坂", "shift": "早", "max": 6}],
    })
    r = s.say("赤坂さん、15日は休み").to_dict()
    assert r["kind"] == "applied"
    assert store.month(2026, 12).table["赤坂"][14] == "公"
    assert _say(s, "遅番の次の日に早番はきついから、やめてあげて")["kind"] == "applied"
    assert _say(s, "赤坂さん、早番は月6回まで")["kind"] == "applied"


@pytest.mark.parametrize("text,items", [
    ("赤坂さん、15日は有給", [{"type": "set_cell", "staff": "赤坂", "days": [15], "shift": "有"}]),
    ("赤坂さん、有給を2日", [{"type": "leave_count", "staff": "赤坂", "count": 2}]),
])
def test_有給の記号が無い施設では有給の頼みを聞き返す(no_leave, tmp_path, text, items):
    store = _copy(no_leave, tmp_path)
    before = store.month(2026, 12).table
    rev = store.ledger().rev
    s = _session(store, {text: items})
    r = s.say(text).to_dict()
    assert r["kind"] == "ask"
    assert "この施設には有給の記号がありません" in r["message"]
    assert store.month(2026, 12).table == before and store.ledger().rev == rev


def test_読み取りの後の想定外の例外は読み取りの失敗として返す(no_leave, tmp_path, monkeypatch):
    store = _copy(no_leave, tmp_path)
    before = store.month(2026, 12).table
    s = _session(store, {"赤坂さん、15日は休み": [{"type": "day_off", "staff": "赤坂", "days": [15]}]})

    def boom(*a, **k):
        raise RuntimeError("想定外")
    monkeypatch.setattr("shiftvoice.session.to_proposal", boom)
    r = s.say("赤坂さん、15日は休み").to_dict()
    assert r["kind"] == "error" and r["message"].startswith("読み取りに失敗しました")
    assert store.month(2026, 12).table == before


# 2 回数のルールを一言で変える

def test_全員の公休を一言で変えると新しい数が効く(plain, tmp_path):
    store = _copy(plain, tmp_path)
    assert set(_rest(store).values()) == {9}
    text = "全員、公休は毎月10日"
    s = _session(store, {text: [{"type": "count_limit", "shift": "公", "min": 10, "max": 10}]})
    r = _say(s, text)
    assert r["kind"] == "applied" and "守れませんでした" not in r["warning"]
    assert set(_rest(store).values()) == {10}
    # 毎月のルールなので、翌々月の元の表にも効く
    flow = NextFlow(store, 2026, 12)
    flow.review()
    out = flow.build()
    assert out["status"] == "ok"
    assert {row.count("公") for row in out["tables"]["base"].values()} == {10}


def test_人ごとの公休のルールを一言で変えると新しい数が効く(with_part, tmp_path):
    store = _copy(with_part, tmp_path)
    assert _rest(store)["野口"] == 18
    text = "野口さん、公休は毎月11日"
    s = _session(store, {text: [{"type": "count_limit", "staff": "野口", "shift": "公", "min": 11, "max": 11}]})
    r = _say(s, text)
    assert r["kind"] == "applied" and "守れませんでした" not in r["warning"]
    assert _rest(store)["野口"] == 11


def test_まとめた公休のルールの中の1人を一言で変えると効く(with_part, tmp_path):
    store = _copy(with_part, tmp_path)
    text = "赤坂さん、公休は毎月11日"
    s = _session(store, {text: [{"type": "count_limit", "staff": "赤坂", "shift": "公", "min": 11, "max": 11}]})
    r = _say(s, text)
    assert r["kind"] == "applied" and "守れませんでした" not in r["warning"]
    rest = _rest(store)
    assert rest["赤坂"] == 11 and rest["野口"] == 18 and rest["井上"] == 9


def test_パートの人がいる施設で全員の公休を変えても人ごとのルールは残る(with_part, tmp_path):
    store = _copy(with_part, tmp_path)
    text = "全員、公休は毎月10日"
    s = _session(store, {text: [{"type": "count_limit", "shift": "公", "min": 10, "max": 10}]})
    r = _say(s, text)
    assert r["kind"] == "applied" and "守れませんでした" not in r["warning"]
    rest = _rest(store)
    assert rest.pop("野口") == 18
    assert set(rest.values()) == {10}


# 3 段を下げて作った施設の翌々月

def test_段を下げて作った施設で翌々月を作って採れる(lowered, tmp_path):
    store = _copy(lowered, tmp_path)
    flow = NextFlow(store, 2026, 12)
    flow.review()
    out = flow.build()
    assert out["status"] == "ok", out.get("message")
    assert out.get("no_takers") and out.get("note")
    assert flow.adopt("base") == {"ok": True, "plan": "base"}
    jan = store.month(2027, 1)
    assert jan.table and all(len(row) == 31 for row in jan.table.values())
