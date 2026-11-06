"""取り込みで確かめた勤務の時刻（早番6時45分から9時間 など）が、施設に入り、次の勤務までの間の数え方に効く。
［新しい勤務］は時刻を持たないので、入れなければ数えない（敵対レビュー。貼り付けの見本25で、遅Bに遅番の時刻を入れると効く）。"""
from pathlib import Path

import pytest

from shiftvoice import onboard
from shiftvoice.interval import count_short
from shiftvoice.onboard import OnboardError
from shiftvoice.store import Store

FIX = Path(__file__).resolve().parent / "fixtures" / "pastes" / "25.tsv"
ANS = {"遅A": {"same_as": "遅"}, "遅B": {"name": "遅番B", "kind": "work"}}


def make(tmp_path, tag, times=None):
    prop = onboard.infer(onboard.parse_paste(FIX.read_text(encoding="utf-8")), 2026, 11, ANS)
    c = onboard.create_facility(prop, tmp_path / tag, name="見本の施設B", next_year=2026, next_month=12,
                                answers=ANS, times=times)
    assert c.next_table_made
    return c, Store(tmp_path / tag)


def types(st):
    return {s.symbol: s for s in st.facility().shift_types}


def test_入れた時刻が施設に入る(tmp_path):
    _, st = make(tmp_path, "a", {"早": {"start": "6:30", "hours": "8.5"}, "遅": {"start": "12:00", "hours": 9}})
    t = types(st)
    assert (t["早"].start, t["早"].hours) == ("06:30", 8.5)
    assert (t["遅"].start, t["遅"].hours) == ("12:00", 9)
    assert (t["日"].start, t["日"].hours) == ("09:00", 9)          # 渡さなかった勤務は見本の時刻のまま


def test_新しい勤務に時刻を入れると目安が効いて数に入る(tmp_path):
    c0, st0 = make(tmp_path, "none")
    c1, st1 = make(tmp_path, "timed", {"遅B": {"start": "11:00", "hours": 9}})
    assert types(st0)["遅B"].start is None and c0.short_rest["untimed"] == ["遅番B"]
    assert types(st1)["遅B"].start == "11:00" and c1.short_rest["untimed"] == []
    # 同じ時刻で貼った月を数え直すと、遅番Bの分が数に入る
    prop = onboard.infer(onboard.parse_paste(FIX.read_text(encoding="utf-8")), 2026, 11, ANS)
    assert count_short(prop.table, st1.facility().shift_types, 14)[0] > count_short(prop.table, st0.facility().shift_types, 14)[0]
    assert c1.short_rest["before"] > c0.short_rest["before"]
    # 作った12月は、遅Bの短い並びを避けるので、時刻なしで作った12月に遅Bで残る数より少ない
    fac1 = st1.facility().shift_types
    dec0, dec1 = st0.month(2026, 12), st1.month(2026, 12)
    assert (count_short(dec1.table, fac1, 14, dec1.prev_tail)[0]
            < count_short(dec0.table, fac1, 14, dec0.prev_tail)[0])
    assert c1.short_rest["after"] == count_short(dec1.table, fac1, 14, dec1.prev_tail)[0]


def test_空なら今までと同じ(tmp_path):
    c0, st0 = make(tmp_path, "x")
    c1, st1 = make(tmp_path, "y", {"遅B": {"start": "", "hours": ""}})
    assert types(st1)["遅B"].start is None
    assert (c1.short_rest["before"], c1.short_rest["after"]) == (c0.short_rest["before"], c0.short_rest["after"])
    assert st1.month(2026, 12).table == st0.month(2026, 12).table


def test_見本の時刻を空にした勤務は数えない(tmp_path):
    c, st = make(tmp_path, "z", {"早": {"start": "", "hours": ""}})
    assert types(st)["早"].start is None
    assert set(c.short_rest["untimed"]) == {"早番", "遅番B"}


@pytest.mark.parametrize("t", [{"start": "25:00", "hours": 9}, {"start": "7:00", "hours": ""},
                               {"start": "7:00", "hours": "abc"}, {"start": "7:00", "hours": 30}])
def test_おかしい時刻は断る(tmp_path, t):
    prop = onboard.infer(onboard.parse_paste(FIX.read_text(encoding="utf-8")), 2026, 11, ANS)
    with pytest.raises(OnboardError):
        onboard.create_facility(prop, tmp_path / "bad", name="見本の施設B", next_year=2026, next_month=12,
                                answers=ANS, times={"遅B": t})


def test_夜勤と明けと休みには時刻を聞かない():
    prop = onboard.infer(onboard.parse_paste(FIX.read_text(encoding="utf-8")), 2026, 11, ANS)
    timed = {s["symbol"] for s in prop.shift_types if onboard.timed_kind(s)}
    assert timed == {"早", "日", "遅", "研", "遅B"}    # 夜・明・公・有は出ない


def test_ひだまりは変わらない():
    st = Store(Path(__file__).resolve().parents[1] / "data" / "hidamari")
    assert "short_rest" not in st.facility().settings.get("quality", {})
    assert {s.symbol: (s.start, s.hours) for s in st.facility().shift_types if s.start} == {
        "早": ("06:45", 9), "日": ("09:00", 9), "遅": ("13:15", 9), "夜": ("16:00", 17), "研": ("09:00", 9)}
