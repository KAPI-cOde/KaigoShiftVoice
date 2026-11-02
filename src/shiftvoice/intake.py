"""読み取りの型を、帳面に足す決まり・職員情報の変更にする。ここで返事を確かめる（AI は候補まで）。

一言は3種類に分かれる（spec.md 画面1）
- cells 今月だけの頼み（日付つき）。マスだけが変わり、札にしない。帳面には今月だけ（once）で入れる
- rule  決まり。札が1枚増える。毎月か今月だけかを言っていなければ聞く（期限つきは聞かない）
- staff 職員の情報。名簿の行に印。言われた月から効く
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import WEEKDAYS, Facility, Month, Staff
from .period import read_period

DATE_TYPES = ("day_off", "set_cell", "avoid_cell", "move")
NOT_YET = ("swap", "pair_apart", "rest_after_night", "around_off", "leave_count", "weigh",
           "explain", "custom")
FLEX_OK = ("fixed", "operational", "wish", "negotiable")


@dataclass
class Proposal:
    kind: str                          # cells / rule / staff
    rules: list[dict] = field(default_factory=list)   # id なしの決まり（帳面に足す時に id を振る）
    staff_changes: list[dict] = field(default_factory=list)
    scope: str | None = None           # rule / once。None は未決（聞く）
    until: str | None = None           # YYYY-MM-DD
    reason: str | None = None
    summary: str = ""


class Clarify(Exception):
    """聞き返す（表も帳面も変えない）。"""


def _names(v, staff_names: set[str]) -> list[str] | None:
    if v is None:
        return None
    vs = [v] if isinstance(v, str) else list(v)
    bad = [x for x in vs if x not in staff_names]
    if bad:
        raise Clarify(f"名簿にない人がいます（{'・'.join(bad)}）。名簿から選んでください")
    return vs


def _days(v, month: Month) -> list[int]:
    try:
        ds = [int(d) for d in (v if isinstance(v, list) else [v])]
    except (TypeError, ValueError):
        raise Clarify("日付が読めませんでした。何日か教えてください")
    if not ds or any(d < 1 or d > month.days for d in ds):
        raise Clarify(f"{month.month}月にない日付です。何日か教えてください")
    return ds


def _sym(v, facility: Facility) -> str:
    if v not in facility.symbols:
        raise Clarify(f"勤務が読めませんでした（{v}）")
    return v


def _num(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or int(v) != v:
        raise Clarify("回数が読めませんでした。何回か教えてください")
    return int(v)


def _flex(v, default: str) -> str:
    return v if v in FLEX_OK else default


def _who(names: list[str] | None) -> str:
    return "・".join(f"{n}さん" for n in names) if names else "全員"


def to_proposal(items: list[dict], text: str, facility: Facility, staff: list[Staff],
                month: Month) -> Proposal:
    names = {s.name for s in staff}
    real = [it for it in items if it.get("type") != "none"]
    if not real:
        raise Clarify("表を変える頼みが見つかりませんでした。もう一度言ってください")
    for it in real:
        if it["type"] == "unclear":
            raise Clarify(it.get("question") or "うまく聞き取れませんでした。もう一度言ってください")
        if it["type"] in NOT_YET:
            raise Clarify("この形の頼みは、まだ扱えません。別の言い方で言ってください")
    kinds = {"cells" if it["type"] in DATE_TYPES else "staff" if it["type"] == "staff_info"
             else "layer" if it["type"] == "layer_def" else "rule" for it in real}
    if len(kinds) > 1:
        raise Clarify("頼みが混ざっています。1つずつ言ってください")
    kind = kinds.pop()
    p = Proposal(kind=kind)
    sums = []
    for it in real:
        t = it["type"]
        reason = it.get("reason") or None
        p.reason = p.reason or reason
        if t == "layer_def":
            from .standby import DefError, explain, validate_def
            try:
                d = validate_def(it.get("def"), facility)
            except DefError as e:
                raise Clarify(str(e))
            p.rules.append({"name": d["name"], "kind": "layer", "flex": "operational",
                            "params": {"def": d}, "who": None, "when": None,
                            "meta": {"explain": explain(d)}})
            sums.append(f"{d['name']}（" + "・".join(explain(d)) + "）")
        elif t == "day_off":
            who = _names(it.get("staff"), names)
            ds = _days(it.get("days"), month)
            p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 休み", "kind": "cell",
                            "flex": "wish", "params": {"in": ["公"]}, "who": who, "when": ds})
        elif t in ("set_cell", "avoid_cell"):
            who = _names(it.get("staff"), names)
            ds = _days(it.get("days"), month)
            sym = _sym(it.get("shift"), facility)
            sname = facility.shift(sym).name
            on_req = facility.shift(sym).on_request_only
            if t == "set_cell":
                p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 {sname}", "kind": "cell",
                                "flex": "fixed" if on_req else _flex(it.get("strength"), "wish"),
                                "params": {"in": [sym]}, "who": who, "when": ds})
            else:
                p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日は{sname}にしない",
                                "kind": "cell", "flex": "wish", "params": {"not_in": [sym]},
                                "who": who, "when": ds})
        elif t == "move":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            a, b = _days(it.get("from_day"), month)[0], _days(it.get("to_day"), month)[0]
            sname = facility.shift(sym).name
            p.rules.append({"name": f"{_who(who)} {a}日は{sname}でない", "kind": "cell", "flex": "fixed",
                            "params": {"not_in": [sym]}, "who": who, "when": [a], "meta": {"moved_to": b}})
            p.rules.append({"name": f"{_who(who)} {b}日 {sname}（{a}日から移す）", "kind": "cell",
                            "flex": "fixed", "params": {"in": [sym]}, "who": who, "when": [b], "meta": {"moved_from": a}})
        elif t == "forbid_sequence":
            who = _names(it.get("staff"), names)
            a, b = _sym(it.get("first"), facility), _sym(it.get("next"), facility)
            p.rules.append({"name": f"{'' if not who else _who(who) + ' '}{facility.shift(a).name}の次の日に"
                                    f"{facility.shift(b).name}を置かない",
                            "kind": "sequence", "flex": _flex(it.get("strength"), "negotiable"),
                            "params": {"if": [a], "next_not_in": [b]}, "who": who, "when": None})
        elif t == "count_limit":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            lo, hi = _num(it.get("min")), _num(it.get("max"))
            if lo is None and hi is None:
                raise Clarify("何回までか教えてください")
            if lo is not None and hi is not None and lo > hi:
                raise Clarify("回数の下限が上限より大きくなっています。もう一度言ってください")
            until_day = it.get("until_day")
            when = None
            label = "月"
            if until_day is not None:
                ud = _days(until_day, month)[0]
                when = list(range(1, ud + 1))
                p.until = f"{month.year:04d}-{month.month:02d}-{ud:02d}"
                label = f"{month.month}月1〜{ud}日の累計"
            rng = (f"{lo}〜{hi}回" if lo is not None and hi is not None else
                   f"{hi}回まで" if hi is not None else f"{lo}回以上")
            params = {"shifts": [sym]}
            if lo is not None:
                params["min"] = lo
            if hi is not None:
                params["max"] = hi
            p.rules.append({"name": f"{_who(who)} {facility.shift(sym).name}は{label}{rng}", "kind": "count",
                            "flex": _flex(it.get("strength"), "operational"), "params": params,
                            "who": who, "when": when})
        elif t == "weekday_pref":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            wds = [w for w in (it.get("weekdays") or []) if w in WEEKDAYS]
            if not wds:
                raise Clarify("曜日が読めませんでした。何曜日か教えてください")
            avoid = it.get("avoid", True) is not False
            jp = "".join("月火水木金土日"[WEEKDAYS.index(w)] for w in wds)
            p.rules.append({"name": f"{_who(who)} {jp}曜は{facility.shift(sym).name}{'に入れない' if avoid else 'にする'}",
                            "kind": "weekday", "flex": "wish",
                            "params": {"weekdays": wds, ("not_in" if avoid else "in"): [sym]},
                            "who": who, "when": None})
        elif t == "consecutive_limit":
            who = _names(it.get("staff"), names)
            mx = _num(it.get("max"))
            if not mx:
                raise Clarify("何日まで続けてよいか教えてください")
            p.rules.append({"name": f"{_who(who)} 連勤は{mx}日まで", "kind": "max_run",
                            "flex": _flex(it.get("strength"), "operational"),
                            "params": {"shifts": "work", "max": mx}, "who": who, "when": None})
        elif t == "staff_info":
            who = _names(it.get("staff"), names) or []
            fm = _num(it.get("from_month"))
            if not who or not fm or fm > 12:
                raise Clarify("誰の、何月からの変更か教えてください")
            year = month.year + (1 if fm < month.month else 0)
            add = [_sym(s, facility) for s in it.get("can_add") or []]
            rem = [_sym(s, facility) for s in it.get("can_remove") or []]
            for n in who:
                p.staff_changes.append({"staff": n, "from": f"{year:04d}-{fm:02d}", "can_add": add,
                                        "can_remove": rem, "said": text})
            sums.append(f"{_who(who)} {fm}月から" + "・".join(facility.shift(s).name for s in add) + "もできる")
    if kind == "cells":
        p.scope = "once"
    elif kind in ("rule", "layer"):
        p.scope = "rule" if p.until else read_period(text)
    else:  # 職員の情報は言われた月から。聞かない
        p.scope = "rule"
    p.summary = "、".join(sums) if sums else "、".join(r["name"] for r in p.rules if "moved_to" not in r.get("meta", {}))
    return p
