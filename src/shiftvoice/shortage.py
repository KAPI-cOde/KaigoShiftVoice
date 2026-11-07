"""一から作る前に、勤務ごとの人手の足りなさを数える。毎日の人数のルール（施設全体）で要る回数と、入れる人の回数の上限の
合計を比べ、足りない時は名前つきの短い文にする（作者 2026-11-07。有給消化が2人になり夜勤が7回足りないのに、計算は
ルールを外して作り、何が足りないのかが分からなかった。「こういう理由でできなかったけれど、こうしてもいいですか？」の形）。

「足りない」と言うのは、段1で外せないルール（HARD）だけで、どう組んでも足りない時に限る。迷う量は引かない。"""
from __future__ import annotations

from .model import Facility, Month, Rule, Staff
from .rules import _syms, _syms_of, cell_value, expand, with_overrides

HARD = ("fixed", "wish", "operational")


def _need(rule: Rule, facility: Facility, staff: list[Staff], month: Month) -> dict[tuple[str, int], int]:
    """人数のルールで要る人数を、(場面（ユニットの名前か「全体」）, 日) ごとに。表を作る時と同じ expand で数えるので、日付の
    例外（「1日は不要」など、その日だけ人数が違う日）と曜日を数え方に入れる（前は毎日の数を月の日数分足し、足りているのに止めた）"""
    out: dict[tuple[str, int], int] = {}
    for ins in expand(rule, facility, staff, month):
        g, d = ins.key.rsplit("/", 1)
        out[(g, int(d))] = max(out.get((g, int(d)), 0), ins.lo or 0)
    return out


def _cap(facility: Facility, eff: list[Rule], s: Staff, sym: str, month: Month) -> int:
    """その人のその勤務の月の上限。表を作る時と同じ expand で数える（名指しの優先・取り込みの幅の広げ方・その勤務に入れる人
    だけの上限を含む）。何種類かの勤務をまとめた上限（「現場勤務は月8日まで」）は、その中の1つの勤務の上限にもなる"""
    c = month.days
    for q in eff:
        if q.kind == "count" and q.flex in HARD and not q.when and sym in _syms(q.params.get("shifts"), facility):
            for ins in expand(q, facility, [s], month):
                if ins.hi is not None:
                    c = min(c, ins.hi)
    return c


def _facts(facility: Facility, staff: list[Staff], eff: list[Rule], month: Month) -> tuple[dict, dict, dict]:
    """段1で外せないルールが人ごとに決める事柄。
    pins 名前 → 日 → その日に置ける記号（1マスだけで決まる場面から。前の月の末の記号で決まる1日も含む。空なら何も置けない）
    lows 名前 → [(記号の組, 下限, 数える日)]（回数のルールの下限）
    after 名前 → [(記号の組, 翌日の記号の組)]（「夜勤の翌日は明け」。翌日の組がもとの組と重ならない並びだけ）
    回数の下限は全員で expand する（取り込みの幅の広げ方は入れる人の数で下限を下げるので、1人だけで数えると下限が高く出る）"""
    every = frozenset(facility.symbols)
    pins: dict[str, dict[int, frozenset[str]]] = {}
    lows: dict[str, list] = {}
    after: dict[str, list] = {}
    for r in eff:
        if r.flex not in HARD or r.kind not in ("cell", "weekday", "count", "sequence", "max_run"):
            continue
        p = r.params
        if r.kind == "sequence" and "next_in" in p and not r.when and not r.meta.get("except_cells"):
            a, t = frozenset(_syms(p["if"], facility)), frozenset(_syms(p["next_in"], facility))
            if not a & t:
                for s in staff:
                    if not r.who or s.name in r.who:
                        after.setdefault(s.name, []).append((a, t))
        for ins in expand(r, facility, staff, month):
            var = [t for t in ins.terms if t.day >= 1]
            k = sum(t.coef for t in ins.terms if t.day < 1 and cell_value({}, month, t.staff, t.day) in t.symbols)
            if r.kind == "count" and ins.lo and var and len({t.staff for t in var}) == 1 and all(t.coef == 1 for t in var):
                lows.setdefault(var[0].staff, []).append((frozenset(var[0].symbols), ins.lo - k,
                                                          frozenset(t.day for t in var)))
            if len(var) != 1:
                continue
            t = var[0]
            ok = [v for v in (0, 1) if (ins.lo is None or t.coef * v + k >= ins.lo) and (ins.hi is None or t.coef * v + k <= ins.hi)]
            day = pins.setdefault(t.staff, {})
            if ok == [1]:
                day[t.day] = day.get(t.day, every) & frozenset(t.symbols)
            elif ok == [0]:
                day[t.day] = day.get(t.day, every) - frozenset(t.symbols)
    return pins, lows, after


def _room(name: str, U: frozenset[str], days: int, facts: tuple[dict, dict, dict]) -> int:
    """その人が U の勤務に入れる日の数の上限（回数のルールの上限は見ない）。月の日数から、U を置けない決まったマスの日、
    ほかの勤務・休みの回数の下限（決まったマスで足りる分は引かない）、夜勤の翌日の明けを引く。
    下限は、記号の組が重ならない物だけを足す（「早番か8時の日勤は3回以上」と「早番は5回以上」は同じ日を数えうる）"""
    pins, lows, after = facts
    mine = pins.get(name, {})
    fixed = {d: a for d, a in mine.items() if not a & U}
    free = days - len(fixed)

    def left(S: frozenset[str], lo: int, on=None) -> int:
        return max(0, lo - sum(1 for d, a in fixed.items() if a & S and (on is None or d in on)))

    base = [(S, left(S, lo, on)) for S, lo, on in lows.get(name, []) if not S & U]

    def used(x: int) -> int:
        cands = list(base)
        for a, t in after.get(name, []):
            if t & U:
                continue
            n = max([lo for S, lo, _ in lows.get(name, []) if S <= a]
                    + [sum(1 for v in mine.values() if v and v <= a), x if U <= a else 0])
            cands.append((t, left(t, n - 1)))   # 末日の夜勤の明けは翌月なので、明けは夜勤より1日少なくてもよい
        out, taken = 0, frozenset()
        for S, lo in sorted(cands, key=lambda c: -c[1]):
            if lo > 0 and not S & taken:
                out, taken = out + lo, taken | S
        return out

    x = free
    while x > 0 and x + used(x) > free:
        x -= 1
    return x


def shortages(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month) -> list[dict]:
    """足りない勤務の一覧。1件は {"unit"（施設全体は None）, "symbol", "name", "need", "cap", "people": [(名前, 上限), ...]}。
    毎日の人数のルール（施設全体か、ユニットごと）で要る回数と、入れる人の入れる回数の合計を比べる。入れる回数は、回数の
    ルールの上限と、入れる日の数（_room）の小さい方。people は回数のルールの上限で止まっている人だけ（上げれば足りる人）"""
    from .build import impossible_wishes
    rules, _ = impossible_wishes(facility, rules, month)   # 表を作る時と同じく、どう組んでも叶わない希望休の日は外す
    eff = with_overrides(rules)
    busy = {w for q in eff if q.kind == "count" and q.who and (q.params.get("min") or 0) >= 10
            and not ({"有"} - _syms_of(q.params.get("shifts"))) for w in q.who}   # 有給消化など
    facts = _facts(facility, staff, eff, month)
    need: dict[tuple[str | None, str], dict[int, int]] = {}   # (ユニット, 勤務) → 日 → 要る人数
    for r in eff:
        p = r.params
        if not (r.kind == "coverage" and r.flex in HARD and p.get("op") in ("==", ">=") and not r.who and not r.when
                and p.get("scope") in ("all", "each_unit")):
            continue
        syms = _syms_of(p.get("shifts"))
        if len(syms) != 1:
            continue
        sym = next(iter(syms))
        if p.get("scope") == "all" and not p.get("units"):
            units = [None]
        elif p.get("scope") == "each_unit":
            units = [u for u in facility.units if not p.get("units") or u in p["units"]]
        else:
            continue
        per = _need(r, facility, staff, month)
        for unit in units:
            got = need.setdefault((unit, sym), {})
            for (g, d), lo in per.items():
                if g == (unit or "全体"):
                    got[d] = max(got.get(d, 0), lo)   # 同じ勤務の人数のルールが2本あれば、日ごとに多い方
    visits = {(k.rsplit("/", 1)[0], u) for k, u in month.support.items()}   # 応援で入る人も、そのユニットの人に数える
    out = []
    for (unit, sym), per in need.items():
        members = [s for s in staff if unit is None or s.unit == unit or (s.name, unit) in visits]
        caps, room = {}, []
        for s in members:
            if sym not in s.can:
                continue
            rc, wc = _cap(facility, eff, s, sym, month), _room(s.name, frozenset({sym}), month.days, facts)
            caps[s.name] = min(rc, wc)
            if 0 < rc < month.days and rc < wc and s.name not in busy:
                room.append((s.name, rc))
        cap, total = sum(caps.values()), sum(per.values())
        if cap < total:
            out.append({"unit": unit, "symbol": sym, "name": facility.shift(sym).name, "need": total, "cap": cap,
                        "people": sorted(room, key=lambda x: x[1])})
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
