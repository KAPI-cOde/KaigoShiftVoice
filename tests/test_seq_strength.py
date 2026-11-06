"""並びの禁止の強さ。言い切ったら運用の都合（operational）、「きつい」「あげて」「できれば」は配慮（negotiable）。
読み取りが negotiable と返しても、配慮の言葉が一言に無ければ operational（作者 2026-11-07）。"""
import pytest

from shiftvoice.intake import to_proposal


def _seq(strength, who=None, nxt=("早", "日")):
    return [{"type": "forbid_sequence", "staff": who, "first": "遅", "next": list(nxt), "strength": strength}]


def _flex(nov, text, items):
    fac, staff, m, _ = nov
    rs = to_proposal(items, text, fac, staff, m).rules
    assert [r["kind"] for r in rs] == ["sequence"]
    return rs[0]["flex"]


@pytest.mark.parametrize("text,got,want", [
    ("遅番の次の日は、早番も日勤もやめて", "negotiable", "operational"),      # 言い切り。読み取りが配慮と返してもそろえる
    ("遅番の次の日に早番は入れないで", "negotiable", "operational"),
    ("遅番の次の日は、早番も日勤もやめて", "operational", "operational"),
    ("遅番の次の日に早番はきついから、やめてあげて", "operational", "negotiable"),
    ("遅番の次の日に早番は、できれば置かないで", "operational", "negotiable"),
    ("遅番の次の日に早番は、なるべくやめて", "wish", "negotiable"),
    ("遅番の次の日に早番は、絶対に入れないで", "fixed", "fixed"),            # 必ず系は今の読み方のまま
    ("遅番の次の日に早番は、絶対に入れないで", "negotiable", "negotiable"),
])
def test_seq_strength_by_words(nov, text, got, want):
    assert _flex(nov, text, _seq(got)) == want


def test_named_sequence_same_rule(nov):
    assert _flex(nov, "岡田さん、遅番の次の日に早番を入れないで", _seq("negotiable", "岡田", ["早"])) == "operational"
    assert _flex(nov, "岡田さん、遅番の次の日に早番はきついから入れないであげて", _seq("operational", "岡田", ["早"])) == "negotiable"
