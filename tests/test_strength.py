"""読み取りのぶれを手元で吸収する所（強さ・件の落とし方）。敵対レビュー（2026-11-03 夜）の指摘を試験に残す。"""
import pytest

from shiftvoice.intake import Clarify, to_proposal


def _flex(nov, text, items):
    fac, staff, m, _ = nov
    return [r["flex"] for r in to_proposal(items, text, fac, staff, m).rules]


def _off(strength):
    return [{"type": "day_off", "staff": "岡田", "days": [15], "strength": strength, "reason": None}]


@pytest.mark.parametrize("text,got,want", [
    ("岡田さん、15と16は法事で休み", "fixed", "wish"),                  # 私用の休みを動かせない扱いにしない
    ("岡田さん、15日はどうしても休みたいって", "fixed", "fixed"),
    ("岡田さん、15日はかならず休み", "fixed", "fixed"),
    ("岡田さん、15日はできるだけ休みにしてあげて", "negotiable", "negotiable"),
    ("岡田さん、15日は出来れば休み", "negotiable", "negotiable"),
    ("岡田さん、15日は夜勤続きできついから休ませてあげて", "negotiable", "negotiable"),
    ("岡田さん、15日は休み、あとはなるべく今のままで", "wish", "wish"),  # 締めの言葉で弱めない
    ("岡田さん、15日は休み。絶対ってわけじゃない", "wish", "wish"),      # 打ち消しで強めない
    ("岡田さん、15日は休み", "high", "wish"),                           # 知らない強さで落ちない
])
def test_day_off_strength(nov, text, got, want):
    assert _flex(nov, text, _off(got)) == [want]


def test_set_cell_external_fixed_kept_and_helpful_not_negotiable(nov):
    cell = lambda s: [{"type": "set_cell", "staff": "田村", "days": [23], "shift": "日", "strength": s}]
    assert _flex(nov, "田村さん、20日は外部の会議があるから日勤で", cell("fixed")) == ["fixed"]
    assert _flex(nov, "田村さん、23日の祝日は日勤だと助かるって", cell("negotiable")) == ["wish"]   # T2-08 に入れない


def test_count_strength(nov):
    cnt = lambda s: [{"type": "count_limit", "staff": ["岡田"], "shift": "夜", "max": 2, "strength": s}]
    assert _flex(nov, "岡田さん、本人の希望で夜勤は月2回まで", cnt("wish")) == ["wish"]
    assert _flex(nov, "小川さん、今月は夜勤0で", cnt("wish")) == ["operational"]


def test_dateless_item_dropped_only_when_copy_of_weekday(nov):
    fac, staff, m, _ = nov
    wd = {"type": "weekday_pref", "staff": "岡田", "weekdays": ["thu"], "shift": "早", "avoid": True}
    p = to_proposal([{"type": "avoid_cell", "staff": "岡田", "days": None, "shift": "早"}, wd],
                    "岡田さん、木曜は早番にしないで", fac, staff, m)
    assert [r["kind"] for r in p.rules] == ["weekday"]
    with pytest.raises(Clarify):   # 別の人の日付の無い休みは、落とさずに聞き返す
        to_proposal([wd, {"type": "day_off", "staff": "小川", "days": None}], "木曜は早番にしないで。あと小川さんも休み",
                    fac, staff, m)


def test_できれば休みにしないでは相談できる強さ(nov):
    """年末の3日を「できれば休みにしないで」と頼むと、前は本人の希望の強さで入り、3日とも必ず勤務になった
    （作者 2026-11-07「年末年始に入れてしまって構わない」）。言い切った時は今までどおり本人の希望の強さ。"""
    avoid = lambda s: [{"type": "avoid_cell", "staff": "岡田", "days": [28, 29, 30], "shift": "公", "strength": s}]
    assert _flex(nov, "岡田さん、28日から30日は、できれば休みにしないで", avoid("negotiable")) == ["negotiable"]
    assert _flex(nov, "岡田さん、28日から30日は、できれば休みにしないで", avoid(None)) == ["negotiable"]   # 読み取りが強さを付けない時
    assert _flex(nov, "岡田さん、28日から30日は休みにしないで", avoid("negotiable")) == ["wish"]   # 言い切りは弱めない
    assert _flex(nov, "岡田さん、28日から30日は絶対に休みにしないで", avoid("fixed")) == ["wish"]   # 動かせない強さにはしない
