"""ルールを押した時に出す中身（作者 2026-11-06「押した時に具体性が無い。連勤の上限を押すと全部のマスが黄色になり、
連勤いくつの上限で作っているのかも分からない」）。

- line  ① そのルールの中身を数で言う1行（「連勤は5日まで」「夜勤は月3〜5回」）
- now   ② 今の表がそのルールに対してどうなっているか、余りがあるか（短い文の並び）
- cells ③ 上限や下限に届いているマス（調整の余地が無い所）。届いている所が無ければ空
- glow  ③ の黄色が何かを言う1行（光らせる時だけ）

数え方は表を作る時と同じにする（rules.with_overrides → expand → evaluate）。名指しのルールが優先する人、
公休の足し引き、取り込みで見つけた回数の幅を月の日数に合わせて広げる所（fit_count）も、そのまま入る。
連勤だけは、窓の数では「5日続いた」が言えないので、同じ記号の組で、表を作る時と同じ前の月の末の日数から続く並びを数える
（月末が夜勤なら翌月1日の明けまで。expand の月またぎと同じ条件）。
並び（夜勤の次の日は明け・遅番の次の日に早番を置かない）は、守れていない所だけを光らせる。
日や曜日を決めたルールは、そのルールが決めているマスを光らせる。知らない種類は ① だけ（読めた範囲）。
"""
from __future__ import annotations

from collections import Counter

from .model import Facility, Month, Rule, Staff
from .rules import _days, _syms, _who, broken, cell_value, evaluate, expand, fit_count, with_overrides

WD_JP = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}


def _names(fac: Facility, syms) -> str:
    nm = {st.symbol: st.name for st in fac.shift_types}
    return "・".join(nm.get(s, s) for s in syms)


def _unit(fac: Facility, syms) -> str:
    off = {st.symbol for st in fac.shift_types if st.kind in ("rest", "leave")}
    return "日" if set(syms) <= off else "回"


def _people(names: list[str], cap: int = 3) -> str:
    shown = "・".join(f"{n}さん" for n in names[:cap])
    return shown + (f"ほか{len(names) - cap}人" if len(names) > cap else "")


def _days_text(days: list[int]) -> str:
    """[1..29] → 「1〜29日」、[15, 16] → 「15・16日」（3日以上続く時だけ〜でつなぐ）。"""
    days = sorted(days)
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{days[0]}〜{days[-1]}日"
    return "・".join(map(str, days)) + "日"


def _wd_text(p: dict) -> str:
    return "".join(WD_JP.get(w, "") for w in p.get("weekdays") or []) + ("祝" if p.get("holidays") else "")


def _who_no(who) -> str:
    if not who:
        return ""
    return f"{who[0]}さんの" if len(who) == 1 else f"{who[0]}さんほか{len(who) - 1}人の"


def _who_wa(who) -> str:
    if not who:
        return ""
    return f"{who[0]}さんは" if len(who) == 1 else f"{who[0]}さんほか{len(who) - 1}人は"


def _rng(lo, hi, u: str) -> str:
    if lo is not None and lo == hi:
        return f"{lo}{u}"
    if lo is not None and hi is not None:
        return f"{lo}〜{hi}{u}"
    return f"{hi}{u}まで" if hi is not None else f"{lo}{u}以上"


def _counted(inst, table, month) -> list[list]:
    """場面の項のうち、今の表で数えているマス（月の中だけ）。"""
    out = []
    for t in inst.terms:
        if t.day >= 1 and cell_value(table, month, t.staff, t.day) in t.symbols:
            out.append([t.staff, t.day])
    return out


def _dedupe(cells: list[list]) -> list[list]:
    seen, out = set(), []
    for c in cells:
        k = (c[0], c[1])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


# ------------------------------------------------------------------ 人数（coverage）

def _coverage(orig: Rule, eff: Rule, fac: Facility, staff: list[Staff], month: Month, table) -> dict:
    p = orig.params
    syms = _syms(p["shifts"], fac)
    what = _names(fac, syms) + ("（合わせて）" if len(syms) > 1 else "")
    each = p.get("scope") == "each_unit" and len(fac.units) > 1
    us = [u for u in fac.units if not p.get("units") or u in p["units"]]   # ユニットを名指しした人数（東だけ など）
    where = (("・".join(us) + ("それぞれ" if len(us) > 1 else "で") if len(us) <= 3 else "ユニットごとに") if each else
             ("施設全体で" if len(fac.units) > 1 else ""))
    n, op = p["n"], p["op"]
    tail = {"==": "人", ">=": "人以上", "<=": "人まで"}[op]
    if p.get("weekdays"):
        line = f"{_wd_text(p)}の{what}は{where}{n}{tail}"
    else:
        line = f"{what}は{where}毎日{n}{tail}"
    if orig.when:
        line += f"（{_days_text(orig.when)}）"

    groups: dict[str, list] = {}
    for inst in expand(eff, fac, staff, month):
        g, d = inst.key.split("/")
        groups.setdefault(g, []).append((int(d), evaluate(inst, table, month), inst))
    if not groups:
        return {"line": line, "now": ["この月は、このルールで数える日がありません"], "glow": None, "cells": []}

    cells: list[list] = []
    texts: dict[str, str] = {}
    for g, rows in groups.items():
        at = [(d, v) for d, v, i in rows if (i.lo is not None and v == i.lo) or (i.hi is not None and v == i.hi)]
        under = [(d, v) for d, v, i in rows if i.lo is not None and v < i.lo]
        over = [(d, v) for d, v, i in rows if i.hi is not None and v > i.hi]
        for d, v, i in rows:
            if (i.lo is not None and v <= i.lo) or (i.hi is not None and v >= i.hi):
                cells += _counted(i, table, month)
        vs = [v for _, v, _ in rows]
        listing = lambda xs: "・".join(f"{d}日 {v}人" for d, v in xs[:4]) + ("ほか" if len(xs) > 4 else "")  # noqa: E731
        if op == "==":
            if len(at) == len(rows):
                t = f"{len(rows)}日とも{n}人ちょうど"
            else:
                off = sorted(under + over)
                t = f"{n}人でない日が{len(off)}日（{listing(off)}）" if off else \
                    f"{n}人ちょうどの日が{len(at)}日、ほかの{len(rows) - len(at)}日は曜日の人数のルールが優先"
        elif op == ">=":
            t = f"ちょうど{n}人の日が{len(at)}日、{n + 1}人以上の日が{len(rows) - len(at) - len(under)}日（最多{max(vs)}人）"
            if under:
                t += f"。{n}人に足りない日が{len(under)}日（{listing(under)}）"
        else:
            t = f"上限の{n}人ちょうどの日が{len(at)}日、{n}人より少ない日が{len(rows) - len(at) - len(over)}日（最少{min(vs)}人）"
            if over:
                t += f"。{n}人を超える日が{len(over)}日（{listing(over)}）"
        texts[g] = t
    if len(texts) == 1:
        now = ["今の表では、" + next(iter(texts.values()))]
    elif len(set(texts.values())) == 1:
        now = ["今の表では、" + "も".join(texts) + "も、" + next(iter(texts.values()))]
    else:
        now = ["今の表では"] + [f"{g}は、{t}" for g, t in texts.items()]
    cells = _dedupe(cells)
    if not cells:
        now.append("上限・下限に届いている日はありません")
        glow = None
    elif op == "==":
        glow = f"黄色＝{_names(fac, syms)}のマス（どの日も決まった人数ちょうど）"
    elif op == ">=":
        glow = f"黄色＝下限の{n}人ちょうどの日の{_names(fac, syms)}"
    else:
        glow = f"黄色＝上限の{n}人ちょうどの日の{_names(fac, syms)}"
    return {"line": line, "now": now, "glow": glow, "cells": cells}


# ------------------------------------------------------------------ 回数（count）

def _count(orig: Rule, eff: Rule, fac: Facility, staff: list[Staff], month: Month, table) -> dict:
    p = orig.params
    syms = _syms(p["shifts"], fac)
    what, u = _names(fac, syms), _unit(fac, syms)
    people = _who(orig, staff)
    if p.get("capable_only"):
        people = [s for s in people if any(x in s.can for x in syms)]
    days = _days(orig, month)
    lo0, hi0 = p.get("min"), p.get("max")
    if p.get("seen"):   # 表を作る時と同じく、月の日数と入れる人の数に合わせて広げた数（rules.expand）
        lo0, hi0 = fit_count(lo0, hi0, p["seen"], len(people), len(days), Counter(s.unit for s in people))
    if orig.when:
        period = f"{month.month}月{_days_text(orig.when)}の累計で"
    else:
        period = "月"
    line = f"{_who_no(orig.who)}{what}は{period}{_rng(lo0, hi0, u)}"
    if p.get("capable_only"):
        line += f"（{what}に入れる{len(people)}人）"
    now: list[str] = []
    if (lo0, hi0) != (p.get("min"), p.get("max")):
        now.append(f"{month.month}月は{month.days}日あるので、{_rng(p.get('min'), p.get('max'), u)}から広げています")

    insts = expand(eff, fac, staff, month)
    if not insts:
        now.append("この月は、このルールで数える人がいません")
        return {"line": line, "now": now, "glow": None, "cells": []}
    rows = [(i.key, evaluate(i, table, month), i) for i in insts]
    at_hi = [(k, v) for k, v, i in rows if i.hi is not None and v == i.hi]
    at_lo = [(k, v) for k, v, i in rows if i.lo is not None and v == i.lo]
    over = [(k, v) for k, v, i in rows if i.hi is not None and v > i.hi]
    under = [(k, v) for k, v, i in rows if i.lo is not None and v < i.lo]
    cells: list[list] = []
    for k, v, i in rows:
        if (i.lo is not None and v <= i.lo) or (i.hi is not None and v >= i.hi):
            cells += _counted(i, table, month)
    exact = all(i.lo is not None and i.lo == i.hi for _, _, i in rows)
    vs = [v for _, v, _ in rows]

    if len(rows) == 1 and orig.who and len(orig.who) == 1:
        k, v, i = rows[0]
        if exact:
            st = "ちょうど" if v == i.lo else (f"{i.lo - v}{u}足りません" if v < i.lo else f"{v - i.hi}{u}多すぎます")
        elif over:
            st = f"上限を{v - i.hi}{u}超えています"
        elif under:
            st = f"下限に{i.lo - v}{u}足りません"
        elif at_hi:
            st = "上限ちょうど"
        elif at_lo:
            st = "下限ちょうど"
        elif i.hi is not None:
            st = f"上限まであと{i.hi - v}{u}"
        else:
            st = f"下限より{v - i.lo}{u}多い"
        now.append(f"今の表では{v}{u}（{st}）")
    elif exact:
        off = [(k, v) for k, v, i in rows if v != i.lo]
        if not off:
            now.append(f"今の表では、{len(rows)}人とも決まった数ちょうどで、余りはありません")
        else:
            now.append(f"今の表では、決まった数でない人が{len(off)}人（"
                       + "・".join(f"{k}さん {v}{u}" for k, v in off[:4]) + ("ほか" if len(off) > 4 else "") + "）")
    else:
        now.append(f"今の表では、最多{max(vs)}{u}・最少{min(vs)}{u}" if max(vs) != min(vs)
                   else f"今の表では、{len(rows)}人とも{vs[0]}{u}")
        if over:
            now.append(f"上限を超えているのは{len(over)}人（" + "・".join(f"{k}さん {v}{u}" for k, v in over[:3]) + "）")
        if at_hi:
            now.append(f"上限ちょうどは{len(at_hi)}人（{_people([k for k, _ in at_hi])}）")
        if at_lo:
            now.append(f"下限ちょうどは{len(at_lo)}人（{_people([k for k, _ in at_lo])}）")
        if under:
            now.append(f"下限に足りないのは{len(under)}人（" + "・".join(f"{k}さん {v}{u}" for k, v in under[:3]) + "）")
        if not (over or at_hi or at_lo or under):
            now.append("上限・下限に届いている人はいません")
    # 人ごとに数が違う人（公休の足し引き）と、名指しのルールの方で数える人
    if lo0 is not None or hi0 is not None:
        adj = [(k, i) for k, v, i in rows if (i.lo, i.hi) != (lo0, hi0)
               and k not in eff.meta.get("except_min", ()) and k not in eff.meta.get("except_max", ())]
        if adj:
            now.append("・".join(f"{k}さんは{_rng(i.lo, i.hi, u)}" for k, i in adj[:3]) + "（公休の足し引き）")
    names = {s.name for s in people}
    excepted = sorted((set(eff.meta.get("except_min", ())) | set(eff.meta.get("except_max", ()))) & names)
    if excepted and orig.who != excepted:
        now.append(f"{_people(excepted)}は、名指しのルールの方で数えます")
    cells = _dedupe(cells)
    if not cells:
        glow = None
    elif exact:
        glow = f"黄色＝{what}のマス（決まった数ちょうど）"
    elif len(rows) == 1:
        glow = f"黄色＝{what}のマス（" + ("上限" if at_hi or over else "下限") + "ちょうど）"
    else:
        glow = f"黄色＝上限・下限ちょうどの人の{what}"
    return {"line": line, "now": now, "glow": glow, "cells": cells}


# ------------------------------------------------------------------ 連勤（max_run）

def runs(rule: Rule, fac: Facility, staff: list[Staff], month: Month, table) -> list[dict]:
    """人ごとの並び（同じ記号の組が続く所）。表を作る時と同じ前の月の末の日数から読み、月の中に1日でもかかる物だけ。
    月末が夜勤で明けを数える時は、翌月1日の明けまで1日足す（rules.expand の月またぎと同じ条件）。"""
    p = rule.params
    syms = set(_syms(p["shifts"], fac))
    m = p["max"]
    tail = len(next(iter(month.prev_tail.values()), []))
    ake = [st.symbol for st in fac.shift_types if st.kind == "ake"]
    night = [st.symbol for st in fac.shift_types if st.next_is_ake]
    extend = bool(ake and night and ake[0] in syms and month.days >= m)
    out = []
    for s in _who(rule, staff):
        seq = [(d, cell_value(table, month, s.name, d)) for d in range(1 - tail, month.days + 1)]
        start = None
        for i, (d, v) in enumerate(seq + [(month.days + 1, None)]):
            if v in syms:
                if start is None:
                    start = d
                continue
            if start is not None:
                end = d - 1
                length = end - start + 1
                plus = extend and end == month.days and seq[-1][1] in night
                if end >= 1:
                    out.append({"staff": s.name, "start": start, "end": end, "len": length + (1 if plus else 0),
                                "next_month": plus})
                start = None
    return out


def _max_run(orig: Rule, eff: Rule, fac: Facility, staff: list[Staff], month: Month, table) -> dict:
    p = orig.params
    m = p["max"]
    work = p["shifts"] == "work"
    what = "連勤" if work else _names(fac, _syms(p["shifts"], fac))
    has_ake = any(st.kind == "ake" for st in fac.shift_types)
    if work:
        line = f"{_who_no(orig.who)}連勤は{m}日まで" + ("（明けも勤務の日に数えます）" if has_ake else "")
    elif m == 1:
        line = f"{_who_wa(orig.who)}{what}を2日続けない"
    else:
        line = f"{_who_wa(orig.who)}{what}を続けて{m}日まで"
    rs = runs(eff, fac, staff, month, table)
    if not rs:
        return {"line": line, "now": [f"今の表では、{what}の日がありません" if not work else "今の表では、勤務の日がありません"],
                "glow": None, "cells": []}
    longest = max(r["len"] for r in rs)
    hit = [r for r in rs if r["len"] == m]
    over = [r for r in rs if r["len"] > m]
    now = []
    if work:
        now.append(f"今の表では、いちばん長い連勤は{longest}日")
    elif m == 1:
        now.append(f"今の表では、{what}が2日以上続く所は{len(over)}か所")
    else:
        now.append(f"今の表では、{what}がいちばん長く続くのは{longest}日")
    if over and (work or m > 1):
        now.append(f"上限を超えているのは{len({r['staff'] for r in over})}人（"
                   + "・".join(f"{r['staff']}さん {r['len']}日" for r in over[:3]) + "）")
    elif over:
        now[-1] += "（" + "・".join(f"{r['staff']}さん {max(r['start'], 1)}日から" for r in over[:3]) + "）"
    if hit and m > 1:
        who = list(dict.fromkeys(r["staff"] for r in hit))
        now.append(f"上限ちょうどは{len(who)}人" + (f"・{len(hit)}か所" if len(hit) > len(who) else "")
                   + f"（{_people(who)}）")
    if not hit and not over and m > 1:
        now.append(f"上限の{m}日に届いている人はいません")
    edge = [r for r in hit + over if r["start"] < 1 or r["next_month"]]
    if any(r["start"] < 1 for r in edge):
        now.append("前の月の末から続く日も数えています")
    if any(r["next_month"] for r in edge):
        now.append("月末の夜勤は、翌月1日の明けまで数えています")
    cells = [[r["staff"], d] for r in hit + over for d in range(max(r["start"], 1), r["end"] + 1)]
    cells = _dedupe(cells)
    if not cells:
        glow = None
    elif m == 1:
        glow = f"黄色＝{what}のマス（どれも続けて置けるのは1日まで）"
    else:
        glow = f"黄色＝上限の{m}日に届いている{what}" + ("の日" if not work else "")
    return {"line": line, "now": now, "glow": glow, "cells": cells}


# ------------------------------------------------------------------ 並び（sequence）

def _sequence(orig: Rule, eff: Rule, fac: Facility, staff: list[Staff], month: Month, table) -> dict:
    p = orig.params
    a = _names(fac, _syms(p["if"], fac))
    who = _who_wa(orig.who)
    if "next2_in" in p:
        t = "か".join(_names(fac, [x]) for x in _syms(p["then"], fac))
        b = "か".join(_names(fac, [x]) for x in _syms(p["next2_in"], fac))
        line = f"{who}{a}の次の日が{t}なら、その次の日も{b}"
    elif "next_not_in" in p:
        b = _names(fac, _syms(p["next_not_in"], fac))
        line = f"{who}{a}の次の日に{b}を置かない"
    elif "prev_in" in p:
        b = "か".join(_names(fac, [x]) for x in _syms(p["prev_in"], fac))
        line = f"{who}{a}は{b}の次の日だけ"
    else:
        b = "か".join(_names(fac, [x]) for x in _syms(p.get("next_in", []), fac))
        line = f"{who}{a}の次の日は{b}"
    bad = [i for i in expand(eff, fac, staff, month) if broken(i, table, month)]
    places = []
    cells: list[list] = []
    for i in bad:
        ds = [t.day for t in i.terms]
        name = i.terms[0].staff
        cells += [[name, d] for d in ds if 1 <= d <= month.days]
        first = i.terms[0].day
        places.append(f"{name}さん {first}日" if first >= 1 else f"{name}さん 前の月の末日")
    listing = "・".join(places[:3]) + ("ほか" if len(places) > 3 else "")
    if "next2_in" in p:
        now = [f"今の表で、{a}・{t}の次の日が{b}でない所が{len(bad)}か所（{listing}）" if bad
               else f"今の表では、{a}・{t}の次の日はどれも{b}です"]
    elif "next_not_in" in p:
        now = [f"今の表で、{a}の次の日が{b}の所は{len(bad)}か所" + (f"（{listing}）" if bad else "")]
    elif "prev_in" in p:
        now = [f"今の表で、前の日が{b}でない{a}が{len(bad)}か所（{listing}）" if bad
               else f"今の表では、{a}の前の日はどれも{b}です"]
    else:
        now = [f"今の表で、{a}の次の日が{b}でない所が{len(bad)}か所（{listing}）" if bad
               else f"今の表では、{a}の次の日はどれも{b}です"]
    if not bad:
        now.append("守れていない所はありません")
    cells = _dedupe(cells)
    return {"line": line, "now": now, "glow": "黄色＝守れていない並び" if cells else None, "cells": cells}


# ------------------------------------------------------------------ 曜日と日（weekday・cell）

def _applies(orig: Rule, eff: Rule, fac, staff, month, table, line: str, what: str) -> dict:
    insts = expand(eff, fac, staff, month)
    bad = [i for i in insts if broken(i, table, month)]
    spots = [(i.terms[0].staff, i.terms[0].day) for i in insts]
    now = []
    if len({s for s, _ in spots}) == 1 and len(spots) <= 6:
        now.append("今の表では " + "・".join(f"{d}日 {_names(fac, [cell_value(table, month, s, d)])}"
                                         for s, d in spots if cell_value(table, month, s, d)))
    if bad:
        now.append(f"守れていない所が{len(bad)}か所（"
                   + "・".join(f"{i.terms[0].staff}さん {i.terms[0].day}日" for i in bad[:3]) + "）")
    elif spots:
        now.append(f"{len(spots)}か所とも守れています" if len(spots) > 1 else "守れています")
    else:
        now.append("この月は、このルールが当てはまる日がありません")
    # 決めているマスを光らせる。全員向けの曜日のルールは多すぎるので、守れていない所だけ
    shown = insts if (orig.who or orig.kind == "cell") else bad
    cells = _dedupe([[i.terms[0].staff, i.terms[0].day] for i in shown])
    glow = (f"黄色＝このルールが決めている{what}" if (orig.who or orig.kind == "cell") else "黄色＝守れていない所") \
        if cells else None
    return {"line": line, "now": now, "glow": glow, "cells": cells}


def _weekday(orig: Rule, eff: Rule, fac, staff, month, table) -> dict:
    p = orig.params
    jp = _wd_text(p)
    rest = lambda syms: all(fac.shift(s).kind in ("rest", "leave") for s in syms if s in fac.symbols)  # noqa: E731
    if "not_in" in p:
        syms = _syms(p["not_in"], fac)
        line = f"{_who_wa(orig.who)}{jp}曜を休みにしない" if rest(syms) else \
            f"{_who_wa(orig.who)}{jp}曜に{_names(fac, syms)}を入れない"
    else:
        syms = _syms(p.get("in", []), fac)
        line = f"{_who_no(orig.who)}{jp}曜は" + ("休み" if rest(syms) else _names(fac, syms))
    return _applies(orig, eff, fac, staff, month, table, line, f"{jp}曜")


def _cell(orig: Rule, eff: Rule, fac, staff, month, table) -> dict:
    p = orig.params
    days = _days_text(_days(orig, month))
    if "not_in" in p:
        line = f"{_who_no(orig.who)}{days}は{_names(fac, _syms(p['not_in'], fac))}にしない"
    else:
        line = f"{_who_no(orig.who)}{days}は{_names(fac, _syms(p.get('in', []), fac))}"
    return _applies(orig, eff, fac, staff, month, table, line, "日")


def _unknown(orig: Rule, fac: Facility) -> dict | None:
    """知らない種類。読める所（勤務と数）だけを ① にし、② ③ は出さない。"""
    p = orig.params or {}
    if not p.get("shifts"):
        return None
    try:
        syms = _syms(p["shifts"], fac)
    except (TypeError, KeyError):
        return None
    line = _names(fac, syms)
    lo, hi = p.get("min"), p.get("max")
    if lo is not None or hi is not None:
        line += " " + _rng(lo, hi, _unit(fac, syms))
    elif p.get("n") is not None:
        line += f" {p['n']}人"
    return {"line": line, "now": [], "glow": None, "cells": []}


KINDS = {"coverage": _coverage, "count": _count, "max_run": _max_run, "sequence": _sequence,
         "weekday": _weekday, "cell": _cell}


def rule_detail(rule_id: str, rules: list[Rule], fac: Facility, staff: list[Staff], month: Month,
                table: dict[str, list[str]]) -> dict | None:
    """rules は表を作る時と同じルールの並び（build.month_rules。前提は入れない）。見つからない・当番（layer）は None。"""
    orig = next((r for r in rules if r.id == rule_id), None)
    if orig is None or orig.kind == "layer":
        return None
    fn = KINDS.get(orig.kind)
    if fn is None:
        return _unknown(orig, fac)
    eff = next((r for r in with_overrides(rules) if r.id == rule_id), orig)
    return fn(orig, eff, fac, staff, month, table)
