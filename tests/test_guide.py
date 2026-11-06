"""見本の施設の11月の表の上に出す、問題の案内（遅番の次の日に早番）の試験。API は呼ばない。"""
import dataclasses
import pathlib
import re

import shiftvoice
from shiftvoice.guide import guide
from shiftvoice.interval import count_short
from shiftvoice.store import Store

SAY = "遅番の次の日に早番はきついから、やめてあげて"


def _late_early(st, fac, m):
    return count_short(m.table, fac.shift_types, 14, m.prev_tail)[1].get("遅番→早番", 0)


def test_guide_counts_the_real_pairs_and_marks_cells(sess):
    st = sess.state()
    g = st["guide"]
    assert g and len(g["problems"]) == 1
    p = g["problems"][0]
    fac, m = sess.store.facility(), sess.store.month(2026, 11)
    # 短い並びの数え方（前の月の末日から続けて数える）と同じ数
    assert p["before"] == p["now"] == _late_early(st, fac, m) == 11
    cells = {(n, d) for n, d in p["cells"]}
    # 枠のマスは、どれも遅番か早番で、日は月の中だけ
    for n, d in cells:
        assert 1 <= d <= m.days and m.table[n][d - 1] in ("遅", "早")
    # 前の月の末日が遅番で1日目が早番の人（岡田さん）は、1日目のマスだけに枠
    assert m.prev_tail["岡田"][-1] == "遅" and m.table["岡田"][0] == "早"
    assert ("岡田", 1) in cells
    assert p["say"] == SAY


def test_guide_goes_to_zero_after_the_one_line(sess):
    r = sess.say(SAY)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "rule")
    assert r.kind == "applied"
    p = sess.state()["guide"]["problems"][0]
    assert p["before"] == 11 and p["now"] == 0 and p["cells"] == []


def test_no_guide_for_other_facilities_or_months(sess, tmp_path):
    fac, m = sess.store.facility(), sess.store.month(2026, 11)
    other = dataclasses.replace(fac, id="komorebi-test")
    assert guide(other, m) is None                                   # 取り込んだ自分の施設には出さない
    assert guide(fac, dataclasses.replace(m, month=12)) is None     # 11月のほかは出さない


def test_screen_has_the_guide_words():
    html = (pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="guide"' in html
    for w in ("表で見る", "翌朝", "家にいられるのは", "ふだんの言葉で話してかまいません", "か所"):
        assert w in html, w
    # 案内の文にコロンや「札」「道具」を使わない
    body = html[html.index("function renderGuide"):html.index("// end renderGuide")]
    texts = re.findall(r'"([^"]*[぀-ヿ一-鿿][^"]*)"', body)
    for t in texts:
        assert not any(x in t for x in ("：", "札", "道具", "体に悪")), t
