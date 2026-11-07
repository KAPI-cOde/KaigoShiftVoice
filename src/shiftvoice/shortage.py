"""一から作る前に、勤務ごとの人手の足りなさを数える。毎日の人数のルール（施設全体）で要る回数と、入れる人の回数の上限の
合計を比べ、足りない時は名前つきの短い文にする（作者 2026-11-07。有給消化が2人になり夜勤が7回足りないのに、計算は
ルールを外して作り、何が足りないのかが分からなかった。「こういう理由でできなかったけれど、こうしてもいいですか？」の形）。"""
from __future__ import annotations

from .model import Facility, Month, Rule, Staff
from .rules import _days, _syms_of, on_days, with_overrides

HARD = ("fixed", "wish", "operational")


def _need(rule: Rule, month: Month) -> int:
    p = rule.params
    return sum(p["n"] for d in _days(rule, month) if on_days(p, month, d))


def _cap(facility: Facility, eff: list[Rule], s: Staff, sym: str, days: int) -> int:
    c = days
    for q in eff:
        qp = q.params
        if (q.kind == "count" and q.flex in HARD and qp.get("max") is not None and sym in _syms_of(qp.get("shifts"))
                and (not q.who or s.name in q.who) and s.name not in q.meta.get("except_max", ())
                and not qp.get("capable_only") and not q.when):
            if _syms_of(qp.get("shifts")) == {sym}:
                c = min(c, qp["max"])
            elif qp["max"] == 0:
                c = 0   # 「勤務を入れない」（全部の勤務の回数0）
    return c


def shortages(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month) -> list[dict]:
    """足りない勤務の一覧。1件は {"unit"（施設全体は None）, "symbol", "name", "need", "cap", "people": [(名前, 上限), ...]}。
    毎日の人数のルール（施設全体か、ユニットごと）で要る回数と、入れる人の回数の上限の合計を比べる。"""
    eff = with_overrides(rules)
    busy = {w for q in eff if q.kind == "count" and q.who and (q.params.get("min") or 0) >= 10
            and not ({"有"} - _syms_of(q.params.get("shifts"))) for w in q.who}   # 有給消化など
    out = []
    for r in eff:
        p = r.params
        if not (r.kind == "coverage" and r.flex in HARD and p.get("op") in ("==", ">=") and not r.who and not r.when
                and p.get("scope") in ("all", "each_unit")):
            continue
        syms = _syms_of(p.get("shifts"))
        if len(syms) != 1:
            continue
        sym = next(iter(syms))
        per = _need(r, month)
        if p.get("scope") == "all" and not p.get("units"):
            groups = [(None, staff)]
        elif p.get("scope") == "each_unit":
            groups = [(u, [s for s in staff if s.unit == u]) for u in facility.units if not p.get("units") or u in p["units"]]
        else:
            continue
        for unit, members in groups:
            caps = {s.name: _cap(facility, eff, s, sym, month.days) for s in members if sym in s.can}
            cap = sum(caps.values())
            if cap < per:
                room = sorted(((n, c) for n, c in caps.items() if 0 < c < month.days and n not in busy), key=lambda x: x[1])
                out.append({"unit": unit, "symbol": sym, "name": facility.shift(sym).name, "need": per, "cap": cap, "people": room})
    return out


def shortage_text(items: list[dict]) -> str:
    """「夜勤が7回足りません。上限の合計は55回で、毎日の人数には62回要ります。上限を上げられそうな人は〇〇さん（4回まで）…」"""
    out = []
    for x in items:
        ppl = "・".join(f"{n}さん（{c}回まで）" for n, c in x["people"][:6]) + ("ほか" if len(x["people"]) > 6 else "")
        where = f"{x['unit']}の" if x.get("unit") else ""
        out.append(f"{where}{x['name']}が{x['need'] - x['cap']}回足りません。入れる人の上限の合計は{x['cap']}回で、毎日の人数には"
                   f"{x['need']}回要ります。" + (f"上限を上げられそうな人は{ppl}です。" if ppl else ""))
    # 次に言うことは、上げられそうな最初の人の名前と、今の上限より1回多い数で1文にする（月は言わない。毎月か今月だけかは
    # 一言の後に聞く。前の例は「12月だけ」と月を決めていて、11月の表では別の月の話として止まった）
    x = next((x for x in items if x["people"]), None)
    if x is None:
        return "".join(out) + "入れる人か上限を増やしてから、もう一度作り直してください。表は変えていません"
    n, c = x["people"][0]
    return "".join(out) + f"上げるには「{n}さんの{x['name']}は{c + 1}回まで」のように言い、もう一度作り直してください。表は変えていません"
