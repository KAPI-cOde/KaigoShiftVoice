"""読み取りの型を、帳面に足す決まり・職員情報の変更にする。ここで返事を確かめる（AI は候補まで）。

一言は3種類に分かれる（spec.md 画面1）
- cells 今月だけの頼み（日付つき）。マスだけが変わり、札にしない。帳面には今月だけ（once）で入れる
- rule  決まり。札が1枚増える。毎月か今月だけかを言っていなければ聞く（期限つきは聞かない）
- staff 職員の情報。名簿の行に印。言われた月から効く
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field

from .model import WEEKDAYS, Facility, Month, Staff
from .period import read_period

DATE_TYPES = ("day_off", "set_cell", "avoid_cell", "move")
NOT_YET = ("swap", "pair_apart", "rest_after_night", "around_off", "weigh",
           "explain", "custom")
FLEX_OK = ("fixed", "operational", "wish", "negotiable")


@dataclass
class Proposal:
    kind: str                          # cells / rule / staff / cancel
    rules: list[dict] = field(default_factory=list)   # id なしの決まり（帳面に足す時に id を振る）
    staff_changes: list[dict] = field(default_factory=list)
    removes: list[dict] = field(default_factory=list)  # 取り消す頼み（人・日・勤務）。当てる先は帳面で探す
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


def _leave_sym(facility: Facility) -> str:
    return next(st.symbol for st in facility.shift_types if st.kind == "leave")


FIXED_WORDS = ("必ず", "かならず", "絶対", "ぜったい", "どうしても", "外せない", "動かせない")
MAYBE_WORDS = ("できれば", "出来れば", "できるだけ", "出来るだけ", "なるべく", "可能なら", "できたら", "出来たら")
CARE_WORDS = ("きつい", "あげて")      # 配慮の言い方（指示文の negotiable の決め）
OWN_WORDS = ("希望", "本人")


def _cell_strength(v, text: str, t: str) -> str:
    """休み・勤務の指定の強さ。読み取りの値を基本にし、ぶれやすい向きだけ一言の言葉で直す。
    - 休みの fixed は、一言に必ず系の言葉がある時だけ（「法事で休み」を動かせない扱いにしない）
    - negotiable は、できれば系か配慮の言葉がある時だけ（「日勤だと助かる」を T2-08 の流れに入れない）"""
    v = _flex(v, "wish")
    if v == "fixed" and t == "day_off" and not any(w in text for w in FIXED_WORDS):
        return "wish"
    if v == "negotiable" and not any(w in text for w in MAYBE_WORDS + CARE_WORDS):
        return "wish"
    return v


def _count_strength(v, text: str, single: bool) -> str:
    """回数の強さ。必ず系の言葉があれば fixed（件が1つの時だけ）。本人の希望・できればと言った時は読み取りの値を残す。
    ほかは施設の運用（operational）にそろえる（「今月は夜勤0で」が wish だと全員向けの下限に負ける）。"""
    if single and any(w in text for w in FIXED_WORDS):
        return "fixed"
    if v in ("wish", "negotiable") and any(w in text for w in OWN_WORDS + MAYBE_WORDS):
        return v
    return "operational"


def _tidy(items: list[dict], month: Month) -> list[dict]:
    """読み取りのぶれを手元で直す。
    - 曜日の頼みと一緒に出た、日付の無い日付の型は落とす（「木曜は早番にしないで」が avoid_cell と weekday_pref の2件になる）
    - move の元の日が無ければ、表でその人のその勤務が1日だけの時に補う（「研修、13日に変わった」）"""
    def key(v):
        return tuple(v) if isinstance(v, list) else (v,)
    wd = {(key(it.get("staff")), it.get("shift")) for it in items if it.get("type") == "weekday_pref"}
    out = []
    for it in items:
        t = it.get("type")
        if t in ("set_cell", "avoid_cell", "day_off") and not it.get("days") and \
                (key(it.get("staff")), it.get("shift")) in wd:
            continue   # 同じ人・同じ勤務の曜日の頼みの写し
        if t == "move" and it.get("from_day") is None and month.table:
            st = it.get("staff")
            st = st[0] if isinstance(st, list) and len(st) == 1 else st
            row = month.table.get(st) if isinstance(st, str) else None
            hits = [i + 1 for i, x in enumerate(row or []) if x == it.get("shift") and i + 1 != it.get("to_day")]
            if len(hits) == 1:
                it = {**it, "from_day": hits[0]}
        out.append(it)
    return out


def to_proposal(items: list[dict], text: str, facility: Facility, staff: list[Staff],
                month: Month, leave_caps: dict[str, int] | None = None) -> Proposal:
    """leave_caps は人ごとの有給の上限（帳面の回数の決まりから。日数のない有給の頼みに使う。13章）。"""
    names = {s.name for s in staff}
    items = _tidy(items, month)
    real = [it for it in items if it.get("type") != "none"]
    if not real:
        raise Clarify("表を変える頼みが見つかりませんでした。もう一度言ってください")
    for it in real:
        if it["type"] == "unclear":
            raise Clarify(it.get("question") or "うまく聞き取れませんでした。もう一度言ってください")
        if it["type"] in NOT_YET:
            raise Clarify("この形の頼みは、まだ扱えません。別の言い方で言ってください")
    kinds = {"cells" if it["type"] in DATE_TYPES else "staff" if it["type"] == "staff_info"
             else "layer" if it["type"] == "layer_def" else "cancel" if it["type"] == "cancel"
             else "rule" for it in real}
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
            fl = _cell_strength(it.get("strength"), text, t) if len(real) == 1 else _flex(it.get("strength"), "wish")
            p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 休み"
                                    + {"fixed": "（必ず）", "negotiable": "（できれば）"}.get(fl, ""),
                            "kind": "cell", "flex": fl, "params": {"in": ["公"]}, "who": who, "when": ds})
        elif t in ("set_cell", "avoid_cell"):
            who = _names(it.get("staff"), names)
            ds = _days(it.get("days"), month)
            sym = _sym(it.get("shift"), facility)
            sname = facility.shift(sym).name
            on_req = facility.shift(sym).on_request_only
            if t == "set_cell":
                p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 {sname}", "kind": "cell",
                                "flex": "fixed" if on_req else _cell_strength(it.get("strength"), text, t)
                                if len(real) == 1 else _flex(it.get("strength"), "wish"),
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
            a = _sym(it.get("first"), facility)
            nx = it.get("next")
            bs = list(dict.fromkeys(_sym(x, facility) for x in (nx if isinstance(nx, list) else [nx])))
            p.rules.append({"name": f"{'' if not who else _who(who) + ' '}{facility.shift(a).name}の次の日に"
                                    f"{'・'.join(facility.shift(b).name for b in bs)}を置かない",
                            "kind": "sequence", "flex": _flex(it.get("strength"), "negotiable"),
                            "params": {"if": [a], "next_not_in": bs}, "who": who, "when": None})
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
                            "flex": _count_strength(it.get("strength"), text, len(real) == 1), "params": params,
                            "who": who, "when": when})
        elif t == "weekday_pref":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            wds = [w for w in (it.get("weekdays") or []) if w in WEEKDAYS]
            if not wds:
                raise Clarify("曜日が読めませんでした。何曜日か教えてください")
            avoid = it.get("avoid", True) is not False
            jp = "".join("月火水木金土日"[WEEKDAYS.index(w)] for w in wds)
            um = _num(it.get("until_month"))
            if um and 1 <= um <= 12:   # 「12月いっぱい」。その月の末日まで（翌月の写しに残り、その次の月には残らない）
                import calendar
                uy = month.year + (1 if um < month.month else 0)
                p.until = f"{uy:04d}-{um:02d}-{calendar.monthrange(uy, um)[1]:02d}"
            upto = f"（{um}月まで）" if um and 1 <= um <= 12 else ""
            p.rules.append({"name": f"{_who(who)} {jp}曜は{facility.shift(sym).name}{'に入れない' if avoid else 'にする'}{upto}",
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
        elif t == "cancel":
            # 取り消しは指定を外すだけ。反対の勤務を命じない（13章。T2-03）
            who = _names(it.get("staff"), names)
            ds = _days(it.get("days"), month)
            if not who or not ds:
                raise Clarify("誰の、何日の頼みを取り消すか教えてください")
            sym = _sym(it.get("shift"), facility) if it.get("shift") else None
            p.removes.append({"who": who, "days": ds, "shift": sym})
            what = f"{facility.shift(sym).name}の" if sym and sym != "公" else "休みの" if sym == "公" else ""
            sums.append(f"{_who(who)}の{'・'.join(map(str, ds))}日の{what}指定を外しました")
        elif t == "leave_count":
            # 日数がなければその人の上限まで、日付は任せる下限だけの決まり（13章。T2-06）
            who = _names(it.get("staff"), names)
            if not who or len(who) != 1:
                raise Clarify("誰の有給か教えてください")
            n = _num(it.get("count"))
            if n is None:
                cap = (leave_caps or {}).get(who[0])
                if cap is None:
                    raise Clarify(f"{who[0]}さんの有給は何日ですか")
                if cap == 0:
                    raise Clarify(f"{who[0]}さんの有給の上限は0日になっています。何日にするか教えてください")
                n = cap
            elif n <= 0:
                raise Clarify("有給の日数を教えてください")
            lv = _leave_sym(facility)
            p.rules.append({"name": f"{who[0]}さん 有給{n}日（日付は任せる）", "kind": "count", "flex": "wish",
                            "params": {"shifts": [lv], "min": n}, "who": who, "when": None})
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
    # 一言に名簿の人が出ているのに、返事が人を省いて「全員」に効く形なら、当てずに聞き返す（レビュー b の指摘。
    # 「岡田さん、15と16は休み」が全員の休みになる道を塞ぐ）
    # 人が出ているかは、伏せる処理と同じ見方（名前・呼び方・名前の頭）で見る（再レビュー b の指摘）
    from .names import Masker
    # 全員向けのルールは「利用者さん」「みなさん」を含むことが多いので、「さん」だけでは人が出たとみなさない
    # （再レビュー2回目 a の指摘）。マスの頼みは下で、人が無ければいつでも聞き返す
    # ただし「おかださん」のように名簿の表記に無いかなの呼びかけは伏せられないので、「さん」などの前の語が
    # 名前でない語（利用者・みな など）でなければ、人が出たとみなす（周8 レビュー b の指摘）
    from .names import NOT_NAMES
    not_name = NOT_NAMES | {"みな", "みんな", "お客", "医者", "先生"}
    said = bool(re.search(r"職員\d{2}", Masker(staff, facility).mask(text)[0])) or any(
        not any(w.endswith(n) for n in not_name)
        for w in re.findall(r"([ぁ-ゖァ-ヺー一-龥々]{1,6}?)(?:さん|くん|ちゃん)", text))
    # マスの頼み（休み・勤務・移動）と取り消しは、人が無いまま全員に効かせない。「全員15日休み」は頼みとして
    # ありえず、さんの無いかなの名前や「皆」を含む一言でもすり抜けないように（再レビュー2回目 b の指摘）
    cells_nobody = any(not r.get("who") for r in p.rules if r["kind"] == "cell") or any(
        not x.get("who") for x in p.removes)
    rules_nobody = any(not r.get("who") for r in p.rules if r["kind"] not in ("layer", "cell"))
    if cells_nobody or (said and not re.search(r"全員|みんな|皆(?!勤)", text) and rules_nobody):
        raise Clarify("誰の頼みか読み取れませんでした。表もルールも変えていません。名前を入れて、もう一度言ってください")
    if kind == "cells":
        p.scope = "once"
    elif kind == "cancel":
        p.scope = "once"   # 取り消しは前の頼みを外すだけ。聞かない
    elif kind in ("rule", "layer"):
        p.scope = "rule" if p.until else read_period(text)
    else:  # 職員の情報は言われた月から。聞かない
        p.scope = "rule"
    p.summary = "、".join(sums) if sums else "、".join(r["name"] for r in p.rules if "moved_to" not in r.get("meta", {}))
    return p
