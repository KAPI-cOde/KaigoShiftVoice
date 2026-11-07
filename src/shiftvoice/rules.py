"""決まりを1次式の不等式（場面）の束に展開する。

ソルバーも表の点検も、同じ場面の一覧を使う。だからソルバーの「守った・外した」と、
表で数えた「破れている」が食い違わない（試験 S3）。

場面 = 項の並び（(人, 日, 記号の組), 係数）と、下限・上限。
日が 0 以下は前の月の末5日（0＝前の月の末日）で、表から定数として読む。
"""
from __future__ import annotations

import json

from collections import Counter
from dataclasses import dataclass

from .model import Facility, Month, Rule, Staff


@dataclass(frozen=True)
class Term:
    staff: str
    day: int                 # 1..days。0 以下は前の月
    symbols: tuple[str, ...]
    coef: int = 1


@dataclass
class Instance:
    rule: Rule
    key: str                 # 場面の名前（例 "東/12"・"小島"・"小島/3-4"）
    terms: list[Term]
    lo: int | None
    hi: int | None


def _sides(new: dict, old: dict) -> set[str]:
    """新しい回数のルールが言った側（下限・上限）と、言っていない側でも新しい値とぶつかる古い側。"""
    out = {side for side in ("min", "max") if new.get(side) is not None}
    if new.get("per_month"):
        out = {"min", "max"}   # 「その月の土日祝の数」は両方の側を言っている（数は月ごとに決まる）
    if "max" in out and "min" not in out and old.get("min") is not None and new["max"] < old["min"]:
        out.add("min")
    if "min" in out and "max" not in out and old.get("max") is not None and new["min"] > old["max"]:
        out.add("max")
    return out


def _replace_same(rules: list[Rule]) -> list[Rule]:
    """同じ記号の組を同じ相手に数える、帳面の回数のルールが2本ある時は、新しい方（後ろ）が古い方に代わる。
    相手が同じとは、全員どうし（全員向けと、取り込みで作った何人かまとめた公休の数を含む）か、同じ人どうし。
    新しい方が言った側だけ古い方から外す。毎月か今月だけかは帳面の期間のまま（今月だけの方は翌月に残らない）。
    （取り込みの知らせが勧める「公休は月10日」が、元の公休の数とぶつかって効かなかった。敵対レビュー 2026-11-03）
    当てるのは運用の都合（operational）どうしで、月の全部に効くものだけ。帳面に無いルール（返す案の g-・前提の p-）は
    当てない（帳面のルールは rules_at が meta に from を入れる）。"""
    import copy

    def ok(r: Rule) -> bool:
        return r.kind == "count" and not r.when and r.flex == "operational" and "from" in r.meta

    out = list(rules)
    for i, new in enumerate(rules):
        if not ok(new):
            continue
        key = tuple(sorted(new.params.get("shifts") or []))
        for j in range(i):
            old = out[j]
            if not ok(old) or tuple(sorted(old.params.get("shifts") or [])) != key:
                continue
            sides = _sides(new.params, old.params)
            if not new.who:
                if old.who and len(old.who) == 1:
                    continue   # 1人だけのルールは全員向けより優先する（with_overrides）。新しい全員向けでも消さない
                old = copy.copy(old)
                old.params = {k: v for k, v in old.params.items() if k not in sides}
            else:
                hit = sorted(set(new.who) & set(old.who or []))
                if not hit:
                    continue   # 古い方が全員向けなら、下の名指しの優先で外れる
                old = copy.copy(old)
                old.meta = {**old.meta, **{f"except_{side}": sorted(set(old.meta.get(f"except_{side}", ())) | set(hit))
                                           for side in sides}}
            out[j] = old
    return out


def restated_count(rd: dict, r: Rule) -> bool:
    """新しく言った回数のルール rd（帳面に入れる前の dict）が、その月の中で前からの回数のルール r に代わるか
    （_replace_same と同じ条件で、同じ人どうし）。今月だけの数は毎月の数を帳面から消さないが、その月は新しい方が効く。"""
    return (rd.get("kind") == r.kind == "count" and not rd.get("when") and not r.when
            and rd.get("flex") == r.flex == "operational" and bool(rd.get("who"))
            and sorted(rd["who"]) == sorted(r.who or []) and "from" in r.meta
            and sorted(rd.get("params", {}).get("shifts") or []) == sorted(r.params.get("shifts") or []))


def _syms_of(v) -> set[str]:
    return {v} if isinstance(v, str) else set(v or [])


def fair_on(rules: list[Rule]) -> list[Rule]:
    """なるべく平等に入れる有給（回数のルールの fair）を働かせた並び。一から表を作る時だけ通す。ふだんは何もしないルールで、
    一言の直し・急な休みの案・返す案・静かなルールの確かめでは、有給は開かず、表にある有給は carry_rules が残す
    （どの計算でも働いていた時は、一言で公休が有給に変わる・電話の案で勤務の日が有給に変わる・返す案が作れない、が起きた）"""
    import copy
    out = []
    for r in rules:
        if r.kind == "count" and r.params.get("fair"):
            r = copy.copy(r)
            r.meta = {**r.meta, "fair_on": True}
        out.append(r)
    return out


def thin_on(rules: list[Rule]) -> list[Rule]:
    """手薄なユニットの平均化（thin_even）を働かせた並び。平等にそろえる2回目の計算だけで通す（1回目にも入れると、1回目は
    作れなかった時の控えなのに計算が長く探す所まで進み、待ちが4分半になった。17人の12月）。"""
    import copy
    out = []
    for r in rules:
        if r.kind == "thin_even":
            r = copy.copy(r)
            r.meta = {**r.meta, "thin_on": True}
        out.append(r)
    return out


def is_fair_on(r: Rule) -> bool:
    return r.kind == "count" and bool(r.params.get("fair")) and bool(r.meta.get("fair_on"))


def thin_counts(rule: Rule, staff: list[Staff], month: Month, table: dict[str, list[str]]) -> dict[str, int]:
    """手薄なユニット（その日に none の勤務の人がいないユニット）で work の勤務に入った、人ごとの回数。数える人は全員（0回も）。"""
    none, work = _syms_of(rule.params["none"]), _syms_of(rule.params["work"])
    units: dict[str, list[str]] = {}
    for s in staff:
        units.setdefault(s.unit, []).append(s.name)
    out = {s.name: 0 for s in staff}
    for names in units.values():
        for d in range(month.days):
            if not any(n in table and table[n][d] in none for n in names):
                for n in names:
                    out[n] += n in table and table[n][d] in work
    return out


def _params_overlap(k: str, p: dict, q: dict) -> bool:
    """同じ種類の2つのルールの中身が、同じ勤務・曜日・並びに重なるか（人は見ない）。"""
    if k in ("count", "max_run"):
        return bool(_syms_of(p.get("shifts")) & _syms_of(q.get("shifts")))
    if k == "weekday":
        return (bool(set(p.get("weekdays") or []) & set(q.get("weekdays") or []))
                and ("in" in p) == ("in" in q)
                and bool(_syms_of(p.get("in") or p.get("not_in")) & _syms_of(q.get("in") or q.get("not_in"))))
    if k == "sequence":
        side = lambda d: next((x for x in ("next2_in", "next_in", "next_not_in", "prev_in") if x in d), None)
        return (bool(_syms_of(p.get("if")) & _syms_of(q.get("if"))) and side(p) == side(q)
                and bool(_syms_of(p.get(side(p))) & _syms_of(q.get(side(q)))))
    if k == "coverage":
        return (bool(_syms_of(p.get("shifts")) & _syms_of(q.get("shifts")))
                and p.get("scope") == q.get("scope") and sorted(p.get("weekdays") or []) == sorted(q.get("weekdays") or [])
                and sorted(p.get("units") or []) == sorted(q.get("units") or []))   # 東だけの人数と全ユニットの人数は別の所
    return False


def same_slot(new: dict, old: Rule) -> bool:
    """新しく言った毎月のルール（帳面に入れる前の dict）が、前からある毎月のルールと同じ所を決めているか。
    同じ所＝同じ種類・同じ相手（同じ人か、どちらも全員）で、数える勤務・曜日・並びが重なる。
    中身も強さも同じ物は「もう入っています」で別に止める。中身が同じで強さ（必ず・できれば）だけ違う言い直しは
    同じ所として聞く（二重に入って、弱めたつもりの前のルールが残るのを止める。Astra 2026-11-03 の指摘2）。
    ここは言い直しや、似たルールが積もるのを入口で止めるため（docs/rule-cleanup.md の2）。日を決めた頼み（when）と当番は当てない。"""
    if old.scope != "rule" or new.get("kind") != old.kind or old.kind in ("layer", "cell"):
        return False
    if new.get("when") or old.when:
        return False
    if sorted(new.get("who") or []) != sorted(old.who or []):
        return False
    p, q = new.get("params") or {}, old.params
    if p == q:
        return new.get("flex") is not None and new.get("flex") != old.flex
    if old.kind == "count" and new.get("flex") == "operational" and old.flex == "operational":
        return False   # 運用の都合の回数どうしは、新しい方が言った側だけ前のに代わる（_replace_same）。聞かない
    return _params_overlap(old.kind, p, q)


def may_cover(a: Rule, b: Rule) -> bool:
    """b が a と同じことを決めて a を守っていそうか（同じ種類で中身が重なり、b が a の人を含む）。
    静かなルールの判定が、b があったおかげかもしれない時に使う（quiet.py）。"""
    if a.kind != b.kind or a.id == b.id or b.kind in ("layer", "cell"):
        return False
    if b.who and not (set(b.who) & set(a.who or b.who)):
        return False
    return a.params == b.params or _params_overlap(a.kind, a.params, b.params)


def with_overrides(rules: list[Rule]) -> list[Rule]:
    """人を名指しした月の回数のルールは、同じ勤務の全員向け（と、より多くの人をまとめた）回数のルールより優先する。
    （「小川さん、今月は夜勤ゼロで」が「夜勤の回数 3〜5回」とぶつかって守られなかった。2026-11-02）
    名指しのルールが言った側（下限か上限）だけ、その人を全員向けの同じ側から外す（片側だけの頼みで、
    もう片側の守りまで外さない）。期間の一部だけのルール（29日まで など）・本人の希望・
    全員向けが動かせない決まり（公休の数 など）の時は外さない。
    その前に、同じ相手に2本ある帳面の回数のルールは新しい方を残す（_replace_same）。"""
    import copy
    rules = _weekday_dated(_run_exceptions(_coverage_dates(_coverage_units(_coverage_days(_replace_same(rules))))))
    named: dict[tuple, list[tuple[Rule, str, dict]]] = {}   # 勤務 → [(元のルール, 人, {"min": 値, "max": 値})]
    for r in rules:
        if r.kind == "count" and r.who and not r.when and r.flex in ("operational", "fixed"):
            for w in r.who:
                v = {side: r.params[side] for side in ("min", "max") if r.params.get(side) is not None}
                if r.params.get("per_month"):
                    v = {"min": None, "max": None}   # 数は月ごとに決まる（month_days_off）。両方の側を上書きする
                if v:
                    named.setdefault(tuple(sorted(r.params.get("shifts") or [])), []).append((r, w, v))
    if not named:
        return rules
    out = []
    for r in rules:
        src = (named.get(tuple(sorted(r.params.get("shifts") or [])))
               if r.kind == "count" and not r.when and r.flex != "fixed" else None)
        if src and r.who:
            # 何人かをまとめたルール（取り込みで作った公休の数など）は、その中の人をより少ない人数で名指ししたルールに譲る
            src = [(s, w, v) for s, w, v in src if s is not r and w in r.who and len(s.who) < len(r.who)]
        if src:
            d: dict[str, dict] = {}
            for _s, w, v in src:
                d.setdefault(w, {}).update(v)
            gmin, gmax = r.params.get("min"), r.params.get("max")
            ex_min, ex_max = set(r.meta.get("except_min", ())), set(r.meta.get("except_max", ()))
            for w, v in d.items():
                # 言った側は上書き。言っていない側も、名指しの値とぶつかる時（上限0 と 下限3 など）は外す
                if "min" in v or ("max" in v and gmin is not None and v["max"] < gmin):
                    ex_min.add(w)
                if "max" in v or ("min" in v and gmax is not None and v["min"] > gmax):
                    ex_max.add(w)
            r = copy.copy(r)
            r.meta = {**r.meta, "except_min": sorted(ex_min), "except_max": sorted(ex_max)}
        out.append(r)
    return out


def dated_same_as_latest(rd: dict, cur) -> bool | None:
    """日付を決めた人数の頼み rd（when のある coverage）が、帳面の同じ勤務・同じ数え方で日の重なる日付の人数のうち、
    いちばん後に入ったものと同じ中身か。日付の人数は新しい方が勝つ（_coverage_dates）ので、古いものと同じでも
    「もう入っています」にしない（「2人→3人→2人」と言い直すと、最後の2人が入らず3人のままだった。Astra 2026-11-07）。
    rd が日付の人数でなければ None（呼ぶ側の今までの比べ方を使う）"""
    if rd.get("kind") != "coverage" or not rd.get("when") or (rd.get("params") or {}).get("weekdays"):
        return None

    # 数え方（施設全体・ユニットごと）が違っても、後に言った日付の人数は前の数え方をゆるめる（_coverage_dates）ので、
    # 同じ勤務なら数え方によらず、いちばん後のものと比べる（「施設全体3人」→「各ユニット0人」→「施設全体3人」で、
    # 最後の3人が入らず各ユニット0人が残った。Astra 2026-11-07）
    syms = frozenset(_syms_of(rd["params"].get("shifts")))
    days = set(rd["when"])
    same_slot = [r for r in cur if r.kind == "coverage" and r.when and not r.params.get("weekdays")
                 and set(r.when) & days and frozenset(_syms_of(r.params.get("shifts"))) == syms]
    last = same_slot[-1] if same_slot else None
    return bool(last) and last.params == rd["params"] and last.when == rd["when"] and last.flex == rd.get("flex", last.flex)


def _coverage_dates(rules: list[Rule]) -> list[Rule]:
    """日付を決めた人数のルール（「28日は行事なので早番を3人」「1日はU2の早番はいなくていい」。when のある coverage）の日は、
    同じ勤務の毎日の人数・曜日の人数・前からの日付の人数のルールが、日付のルールに譲る。譲る側と数は expand で決める
    （_dated_bounds・_dated_total。両立しない側だけ、日付のルールの数までゆるめる。外しきらない）。
    ここでは、譲るルールの meta の dated_over に、日付のルールの中身と日を書くだけ。日付のルールどうしは新しい方が勝つ。
    （2026-11-07 実際の施設の表を画面の口で作った通しの試し。施設は早番を0人・2人にする日を設けていて、言えないと
    段4まで下がり、希望休が51日中41日しか守れなかった。前の作り f0f5141 は Astra の穴6つで取り消した）"""
    import copy
    dated = [(i, r) for i, r in enumerate(rules) if r.kind == "coverage" and r.when and not r.params.get("weekdays")]
    if not dated:
        return rules

    def slot(r: Rule) -> tuple:
        p = r.params
        return (frozenset(_syms_of(p.get("shifts"))), p.get("scope"), tuple(sorted(p.get("units") or [])))
    # 同じ勤務・同じ数え方（施設全体か、どのユニットか）の日付の人数は、日ごとに古い順に重ねて、その日の数（下限・上限）に
    # まとめる。新しく言った側が古い側に代わり、古い側が新しい数と両立しなければ消す。（Astra 2026-11-07 朝。「施設全体3人」の
    # 後の「施設全体1人」で古い3人が残った・「0人」の後の「3人以上」で古い0人の上限が残り0人の表が通った・「3人以上」と
    # 「4人まで」を別に言うと合わせて効かなかった・「3人から4人」の下限が効かなかった）
    # 下限と上限は、それぞれいつ言ったか（並び）も持つ。別の数え方の日付のルール（「U1だけ1人」）をゆるめるのは、それより
    # 後に言った側だけ（「各ユニット0人」→「U1だけ1人」→「各ユニット2人まで」で、古い0人が U1 の1人をゆるめた。Astra）
    eff: dict[tuple, tuple] = {}   # (数え方, 日) → (下限, 下限の並び, 上限, 上限の並び, 勤務)
    for i, r in dated:
        rl, rh = _bounds(r.params)
        for d in r.when:
            lo, li, hi, hj, _ = eff.get((slot(r), d), (None, -1, None, -1, None))
            if rl is not None:
                lo, li = rl, i
                if hi is not None and hi < rl:
                    hi, hj = None, i
            if rh is not None:
                hi, hj = rh, i
                if lo is not None and lo > rh:
                    lo, li = None, i
            eff[(slot(r), d)] = (lo, li, hi, hj, r.params.get("shifts"))
    out = []
    for j, g in enumerate(rules):
        over = []
        if g.kind == "coverage":
            gsyms = frozenset(_syms_of(g.params.get("shifts")))
            for (sl, d), (lo, li, hi, hj, shifts) in eff.items():
                if sl[0] != gsyms or (g.when and d not in g.when):
                    continue
                if g.when and sl != slot(g):   # 別の数え方の日付のルールには、自分より後に言った側だけ譲る
                    lo, hi = (lo if li > j else None), (hi if hj > j else None)
                    if lo is None and hi is None:
                        continue
                p = {"shifts": shifts, "scope": sl[1], "lo": lo, "hi": hi}
                if sl[2]:
                    p["units"] = list(sl[2])
                over.append({"params": p, "days": [d]})
        if over:
            g = copy.copy(g)
            g.meta = {**g.meta, "dated_over": over}
        out.append(g)
    return out


def _bounds(p: dict) -> tuple[int | None, int | None]:
    if "op" not in p:   # その日の日付の人数をまとめた数（_coverage_dates）
        return p.get("lo"), p.get("hi")
    n = p["n"]
    return {"==": (n, n), ">=": (n, None), "<=": (None, n)}[p["op"]]


def _dated_bounds(q: dict, r: dict, gname: str, lo, hi, g_units: list[str], all_units: list[str]):
    """日付のルール r（の中身）の日に、人数のルール q の場面 gname（ユニットの名前か「全体」）の (下限, 上限) をゆるめる。
    ゆるめるのは r と両立しない側だけで、数は r の数まで（外しきらない。r が運用の都合で外れても、q の強さで r の数が残る）。
    - ユニットどうし（穴1）。r が名指ししたユニットだけ。名指しが重ならなければ何もしない
    - 施設全体どうし（穴2）。_cov_yield で両立しない側だけ
    - r がユニットごとで q が施設全体。r のユニットの下限の合計が q の上限を超える時だけ上限を外す。r が全部のユニットで、
      上限の合計が q の下限に届かない時は、q の下限を r の下限の合計まで下げる
    - r が施設全体で q がユニットごと（穴3）。q のユニットの下限の合計が r の上限を超える時は、ユニットの下限を外す。
      q が全部のユニットで上限の合計が r の下限に届かない時は、ユニットの上限を外す。どちらも施設全体の数は _dated_total が持つ"""
    rlo, rhi = _bounds(r)
    floor = rlo if rlo is not None else rhi   # 「1人まで」の日の下限は1人まで下げる（外しきると0人が通った。Astra）

    def loosen(lo, hi, side, v):
        if side == "min":
            return (None if lo is None or v is None else min(lo, v)), hi
        return lo, (None if hi is None or v is None else max(hi, v))

    r_each, q_each = r.get("scope") == "each_unit", q.get("scope") == "each_unit"
    r_units = r.get("units") or all_units
    if r_each == q_each:
        if r_each and gname not in r_units:
            return lo, hi
        qlo, qhi = _bounds(q)   # 両立しない側だけ（_cov_yield と同じ。日付の数が q の数より小さければ下限、大きければ上限）
        ref_lo, ref_hi = floor, (rhi if rhi is not None else rlo)
        if qlo is not None and ref_lo is not None and ref_lo < qlo:
            lo, hi = loosen(lo, hi, "min", floor)
        if qhi is not None and ref_hi is not None and ref_hi > qhi:
            lo, hi = loosen(lo, hi, "max", rhi)
        return lo, hi
    if r_each:   # q は施設全体
        k = len(r_units)
        if rlo is not None and hi is not None and k * rlo > hi:
            hi = None
        if set(r_units) >= set(all_units) and rhi is not None and lo is not None and k * rhi < lo:
            lo = min(lo, k * floor)
        return lo, hi
    k = len(g_units)   # r は施設全体、q はユニットごと
    qlo, qhi = _bounds(q)
    if qlo is not None and rhi is not None and k * qlo > rhi:
        lo = None
    if set(g_units) >= set(all_units) and qhi is not None and rlo is not None and k * qhi < rlo:
        hi = None
    return lo, hi


def _dated_total(q: dict, r: dict, g_units: list[str], all_units: list[str]) -> tuple | None:
    """r が施設全体で q がユニットごとの時、_dated_bounds がユニットの側を外した分を、施設全体の (下限, 上限) として q の強さで
    残す（r が運用の都合で外れても、その日の施設全体が0人にならない。穴3）。外していなければ None。"""
    if q.get("scope") != "each_unit" or r.get("scope") == "each_unit":
        return None
    rlo, rhi = _bounds(r)
    qlo, qhi = _bounds(q)
    k = len(g_units)
    lo = (rlo if rlo is not None else rhi) if qlo is not None and rhi is not None and k * qlo > rhi else None
    hi = rhi if set(g_units) >= set(all_units) and qhi is not None and rlo is not None and k * qhi < rlo else None
    return (lo, hi) if lo is not None or hi is not None else None


def _weekday_dated(rules: list[Rule]) -> list[Rule]:
    """人の曜日の頼み（「赤坂さん、土日は事務所に入れない」）は、同じ人の日付を決めた頼み（「18日（日）は事務所の日直」）の
    日には効かせない。日付の頼みの方が新しく細かい作成者の決めなので。前は2つがぶつかって表が作れず、段を下げて関係のない人の
    頼みまで外れた（2026-11-07 実際の施設の表を画面の口で作った2回目の通しの試し）"""
    import copy
    asked: dict[str, dict[int, set[str]]] = {}   # 人 → 日 → その日にしてほしい勤務（日付を決めた「その勤務にする」頼み）
    for r in rules:
        if r.kind == "cell" and r.who and r.when and r.params.get("in") and r.flex in ("fixed", "wish", "operational") \
                and not r.meta.get("derived"):
            for w in r.who:
                for d in r.when:
                    cur = asked.setdefault(w, {}).get(d)
                    asked[w][d] = set(r.params["in"]) if cur is None else cur & set(r.params["in"])
    if not asked:
        return rules
    out = []
    for g in rules:
        if g.kind == "weekday" and g.who and any(w in asked for w in g.who):
            q = g.params
            clash = {}
            for w in g.who:
                # 日付の頼みと本当にぶつかる日だけ譲る（「1日は夜勤じゃなく」で、関係のない「日曜は早番に入れない」を消さない。Astra）
                days = [d for d, want in asked.get(w, {}).items()
                        if ("not_in" in q and want and want <= set(_syms_of(q["not_in"])))
                        or ("in" in q and not want & set(_syms_of(q["in"])))]
                if days:
                    clash[w] = sorted(days)
            if clash:
                g = copy.copy(g)
                g.meta = {**g.meta, "dated": clash}
        out.append(g)
    return out


def _run_exceptions(rules: list[Rule]) -> list[Rule]:
    """人を名指しした、全員の上限よりゆるい続く日の上限（「千葉さん、今月だけ連勤6日まで」）は、その人を全員の上限から外す。
    前は全員の「連勤5日まで」がもう守っていると見て足さず、希望休と公休の数で6連勤が避けられない人のいる月が作れなかった
    （2026-11-07 実際の施設の表を画面の口で作った通しの試し。施設は10月だけ6連勤を許していた）。名指しの方がきびしい時は両方効く。"""
    import copy
    loose: dict[str, dict[str, int]] = {}   # 勤務の鍵 → 人 → 名指しの上限
    key = lambda p: json.dumps(p.get("shifts"), ensure_ascii=False, sort_keys=True)
    for r in rules:
        if (r.kind == "max_run" and r.who and not r.when and r.params.get("max") is not None and r.flex != "negotiable"
                and r.meta.get("exception")):
            # 「できれば6日まで」の弱い頼みでは、全員の上限（動かせない連勤の上限など）から外さない（Astra）
            for w in r.who:
                loose.setdefault(key(r.params), {})[w] = r.params["max"]
    if not loose:
        return rules
    out = []
    for g in rules:
        if g.kind == "max_run" and not g.who and g.params.get("max") is not None and key(g.params) in loose:
            ex = sorted(w for w, mx in loose[key(g.params)].items() if mx > g.params["max"])
            if ex:
                g = copy.copy(g)
                g.meta = {**g.meta, "except_who": sorted(set(g.meta.get("except_who", ())) | set(ex))}   # 言って除いた人は残す
        out.append(g)
    return out


def on_days(p: dict, month: Month, d: int) -> bool:
    """人数のルールが掛かる日か。曜日（weekdays）を持てばその曜日、祝日も（holidays）ならその月の祝日も。
    曜日を持たなければ毎日。"""
    if not p.get("weekdays"):
        return True
    return month.weekday(d) in p["weekdays"] or bool(p.get("holidays")) and d in month.holidays


def _coverage_days(rules: list[Rule]) -> list[Rule]:
    """曜日つきの人数のルールが、毎日の同じ勤務の人数のルールの上限より多く頼んだ時（「土日は早番を2人」と
    「早番の人数 1人」）、その曜日だけ毎日のルールの上限を外す。下限は外さない（人を減らす頼みは、ぶつかって
    守れないと知らせる方に倒す）。"""
    import copy
    days = [r for r in rules if r.kind == "coverage" and r.params.get("weekdays")]
    if not days:
        return rules
    out = []
    for g in rules:
        p = g.params
        if g.kind == "coverage" and not p.get("weekdays") and not g.when and p["op"] in ("==", "<="):
            # 日付を決めた人数（when）は、曜日の人数でゆるめない。その日だけの数なので、曜日の人数より細かい（Astra の
            # 2026-11-07 の指摘4。「11月1日（日）だけ各1人」の上限が「日曜は各2人」で外れ、各2人の表で通った）
            offs = []
            for r in days:
                q = r.params
                if sorted(q["shifts"] if isinstance(q["shifts"], list) else [q["shifts"]]) != \
                        sorted(p["shifts"] if isinstance(p["shifts"], list) else [p["shifts"]]) \
                        or q.get("scope") != p.get("scope") \
                        or sorted(q.get("units") or []) != sorted(p.get("units") or []):
                    continue
                if q["n"] > p["n"] and q["op"] in ("==", ">=", "<="):
                    offs.append({"weekdays": q["weekdays"], "holidays": bool(q.get("holidays"))})
            if offs:
                g = copy.copy(g)
                g.meta = {**g.meta, "hi_off": offs}
        out.append(g)
    return out


def _cov_key(p: dict) -> tuple:
    return (tuple(sorted(_syms_of(p.get("shifts")))), tuple(sorted(p.get("weekdays") or [])), bool(p.get("holidays")))


def _cov_yield(new: dict, old: dict) -> set[str]:
    """古い人数のルールが、新しい人数のルールに譲る側（min・max）。新しい値と両立しない側だけ譲る。両立する側は残す。
    「2人以上」に「3人まで」を足しても2人以上は残り、「西と南は1人」に「西は2人」を足しても、新しい方が運用の都合で
    外れた日に西が1人を下回らない（1人の下限は2人と両立する）。"""
    out = set()
    if old["op"] in ("==", ">=") and new["n"] < old["n"]:
        out.add("min")
    if old["op"] in ("==", "<=") and new["n"] > old["n"]:
        out.add("max")
    return out


_STRENGTH = ["negotiable", "operational", "wish", "fixed"]   # 弱い順


def _coverage_units(rules: list[Rule]) -> list[Rule]:
    """ユニットを名指しした人数のルール（「東の早番は毎日2人」。params の units）は、同じ勤務・同じ曜日の、ユニットごとの
    別のルールより優先する（人を名指しした回数のルールと同じ考え。with_overrides）。
    - 全ユニット向けのルール（「早番は各ユニットで1人」）は、名指しのユニットだけ譲る
    - 別の名指しのルールは、後ろ（新しい方）に、重なるユニットだけ譲る。取り込みでまとめた「西と南は1人」の西だけを
      「西は2人」に変えられる（Astra の指摘3）。ルールごと置き換えると南の人数まで消えるので、1本のルールは残す
    譲るのは新しい値と両立しない側だけ（_cov_yield）。上限だけの頼みで共通の下限まで消さない（Astra の指摘2）。
    譲ったユニットは meta の skip_min・skip_max に書き、expand が数えない。"""
    import copy
    out = list(rules)
    for i, new in enumerate(rules):
        p = new.params
        if new.kind != "coverage" or p.get("scope") != "each_unit" or not p.get("units") or new.when:
            continue   # 日付を決めた人数は、その日だけゆるめる（_coverage_dates）
        for j, g in enumerate(out):
            q = g.params
            if j == i or g.kind != "coverage" or q.get("scope") != "each_unit" or _cov_key(q) != _cov_key(p) or g.when:
                continue
            if j > i:
                continue   # 新しいルールは、古い名指しのルールに譲らない（下で、古い名指しの方が新しい全ユニット向けに譲る）
            hit = set(p["units"]) & set(q.get("units") or p["units"])
            if not hit:
                continue
            g = copy.copy(g)
            g.meta = {**g.meta, **{f"skip_{side}": sorted(set(g.meta.get(f"skip_{side}", ())) | hit)
                                   for side in _cov_yield(p, q)}}
            out[j] = g
    # 後から言った全ユニット向けのルール（「どのユニットも、夜勤は毎日1人まで」）には、前からの名指しのルール（取り込みで
    # 作った「U1・U3は夜勤2人まで」）が譲る。前は名指しの方が新しさに関わらず勝ち、新しいルールが U2 にしか効かず、
    # ルールの説明は「超えた日は無い」と出た（2026-11-07 の通しの試し）
    for i, new in enumerate(rules):
        p = new.params
        if new.kind != "coverage" or p.get("scope") != "each_unit" or p.get("units") or new.when:
            continue
        for j in range(i):
            g = out[j]
            q = g.params
            if g.kind != "coverage" or q.get("scope") != "each_unit" or not q.get("units") or _cov_key(q) != _cov_key(p) \
                    or g.when:
                continue
            if _STRENGTH.index(new.flex) < _STRENGTH.index(g.flex):
                continue   # 弱い新しいルール（できれば）では、強い前のルール（動かせない人数）を譲らせない（Astra）
            sides = _cov_yield(p, q)
            if not sides:
                continue
            g = copy.copy(g)
            g.meta = {**g.meta, **{f"skip_{side}": sorted(set(g.meta.get(f"skip_{side}", ())) | set(q["units"]))
                                   for side in sides}}
            out[j] = g
    return out


def _who(rule: Rule, staff: list[Staff]) -> list[Staff]:
    if not rule.who:
        return staff
    names = set(rule.who)
    return [s for s in staff if s.name in names]


def _days(rule: Rule, month: Month) -> list[int]:
    return list(rule.when) if rule.when else list(range(1, month.days + 1))


# 事務の人の公休は、その月の土日祝の数に、年末年始（12/29〜1/3）を休み扱いで足した数（作者 2026-11-07）
YEAR_END = frozenset({(12, 29), (12, 30), (12, 31), (1, 1), (1, 2), (1, 3)})


def month_days_off(rule: Rule, month: Month) -> int | None:
    """「その月の土日祝の数」で数える回数のルール（params の per_month）の、その月の数。ほかのルールは None。"""
    p = rule.params or {}
    if p.get("per_month") != "weekend_holidays":
        return None
    return sum(1 for d in _days(rule, month)
               if month.is_weekend_or_holiday(d) or (p.get("year_end") and (month.month, d) in YEAR_END))


def fit_count(lo: int | None, hi: int | None, seen: dict, people: int, days: int,
              units: dict[str, int] | None = None) -> tuple[int | None, int | None]:
    """取り込みで見つけた回数の幅（夜勤の回数）を、数える日数と入れる人の数に合わせて広げる。
    seen は貼った月の {"total": 入れる人の回数の合計, "days": 日数}。その日数の回数（total × days ÷ seen の日数）に、
    入れる人の上限の合計が届かない時は上限を、下限の合計が超える時は下限を、届く所まで動かす。狭めはしない。
    （回数は月の日数で変わらないので、30日の月に6人が5回ずつだと31日の月は上限の合計30回 < 31晩、31日の月に5〜6回だと
    28日の2月は下限の合計30回 > 28晩になり、運用の都合を外す段へ下がっていた。Codex の指摘 2026-11-05）
    seen に "units"（ユニット → 貼った月のそのユニットの回数の合計）があり、units（ユニット → 入れる人の数）を渡された時は、
    ユニットごとにも同じように見て、上限はいちばん要るユニットに届くまで上げ、下限はいちばん少ないユニットを超えない所まで
    下げる（夜勤の人数がユニットごとのルールの時。東6人・西5人の2ユニットで、施設全体の11人だけで見ると、西の上限の合計が
    晩の数に届かなかった。Astra の指摘 2026-11-05）。入れる人が0人のユニットは見ない。"""
    if not seen.get("days"):
        return lo, hi
    groups = [(seen["total"], people)]
    if units:
        groups += [(t, units.get(u, 0)) for u, t in (seen.get("units") or {}).items()]
    for done, k in groups:
        if k <= 0:
            continue
        total = done * days
        fewest, most = total // seen["days"], -(-total // seen["days"])   # その日数の回数（割り切れない時は広い側へ）
        if lo is not None:
            lo = min(lo, fewest // k)
        if hi is not None:
            hi = max(hi, -(-most // k))
    return lo, hi


def _syms(spec, facility: Facility) -> tuple[str, ...]:
    if spec == "work":
        return tuple(facility.workday_symbols())
    if isinstance(spec, str):
        return (spec,)
    return tuple(spec)


def expand(rule: Rule, facility: Facility, staff: list[Staff], month: Month) -> list[Instance]:
    """ルールの場面。その日だけ外したマス（meta の except_cells）は、計算・外れの確かめ・詰まり所のどれでも数えない"""
    return _except_cells(rule, month, _expand(rule, facility, staff, month))


def _except_cells(rule: Rule, month: Month, out: list[Instance]) -> list[Instance]:
    """「名前/YYYY-MM-DD/記号」のマス（穴を埋める提案を受けた時に、その人のこのルールをその日だけ外した所）。
    回数のルールはそのマスを数えず、ほかの種類はそのマスにその記号を含む場面を外す（遅番の次の日の早番を1日だけ認めても、
    その日の遅番から次の日への並びは守る）"""
    ex = rule.meta.get("except_cells")
    if not ex:
        return out
    ym = f"{month.year:04d}-{month.month:02d}-"
    cells: dict[tuple[str, int], str] = {}
    for c in ex:
        name, date, sym = c.rsplit("/", 2)
        if date.startswith(ym):
            cells[(name, int(date[8:10]))] = sym
    if not cells:
        return out

    def hit(t: Term) -> bool:
        return cells.get((t.staff, t.day)) in t.symbols

    res = []
    for inst in out:
        if not any(hit(t) for t in inst.terms):
            res.append(inst)
        elif rule.kind == "count":
            res.append(Instance(inst.rule, inst.key, [t for t in inst.terms if not hit(t)], inst.lo, inst.hi))
    return res


def _expand(rule: Rule, facility: Facility, staff: list[Staff], month: Month) -> list[Instance]:
    p = rule.params
    k = rule.kind
    out: list[Instance] = []
    people = _who(rule, staff)
    if k == "coverage":
        syms = _syms(p["shifts"], facility)
        def unit_on(s, d):  # 応援の日は、その日だけ入るユニットで数える
            return month.support.get(f"{s.name}/{d}", s.unit)

        # ユニットごとの人数は、名指しのユニット（units）があればそこだけ。ユニットごとに違う人数（東は2人・西と南は1人）を
        # 持つため。名指しのルールに譲った側は数えない（skip_min・skip_max。_coverage_units）
        skip_lo, skip_hi = set(rule.meta.get("skip_min", ())), set(rule.meta.get("skip_max", ()))
        units = [u for u in facility.units if not p.get("units") or u in p["units"]]

        for d in _days(rule, month):
            if not on_days(p, month, d):
                continue   # 曜日つきの人数（「土日は日勤を2人」）はその曜日だけ
            hi_off = any(on_days(o, month, d) for o in rule.meta.get("hi_off", ()))
            dated = [o["params"] for o in rule.meta.get("dated_over", ()) if d in o["days"]]   # _coverage_dates
            groups = ([(u, [s for s in people if unit_on(s, d) == u]) for u in units]
                      if p.get("scope") == "each_unit" else [("全体", people)])
            for gname, members in groups:
                terms = [Term(s.name, d, syms) for s in members]
                lo, hi = _bounds(p)
                if hi_off or gname in skip_hi:
                    hi = None   # 曜日つきの人数が上限を超えて頼んだ日（with_overrides）か、名指しのルールに譲った上限
                if gname in skip_lo:
                    lo = None
                for r in dated:   # 日付を決めた人数の日は、両立しない側だけ日付の数までゆるめる
                    lo, hi = _dated_bounds(p, r, gname, lo, hi, units, list(facility.units))
                if lo is None and hi is None:
                    continue
                out.append(Instance(rule, f"{gname}/{d}", terms, lo, hi))
            for r in dated:   # 施設全体の日付の人数でユニットの側を外した日は、施設全体の数をこのルールの強さで残す
                tot = _dated_total(p, r, units, list(facility.units))
                if tot:
                    out.append(Instance(rule, f"全体/{d}", [Term(s.name, d, syms) for s in people], *tot))
    elif k == "count":
        syms = _syms(p["shifts"], facility)
        if p.get("capable_only"):  # その勤務に入れる人だけ（例 夜勤の回数は夜勤に入れる人だけ）
            people = [s for s in people if any(x in s.can for x in syms)]
        rest = {st.symbol for st in facility.shift_types if st.kind == "rest"}
        adjust = month.rest_adjust if set(syms) <= rest else {}
        lo0, hi0 = p.get("min"), p.get("max")
        n_month = month_days_off(rule, month)
        if n_month is not None:
            lo0 = hi0 = n_month
        if p.get("seen"):   # 取り込みで見つけた回数の幅は、月ごとに日数と入れる人の数に合わせて広げる（言った側だけ）
            lo0, hi0 = fit_count(lo0, hi0, p["seen"], len(people), len(_days(rule, month)),
                                 Counter(s.unit for s in people))   # ユニットごとの入れる人の数（その勤務に入れる人で数える）
        for s in people:
            terms = [Term(s.name, d, syms) for d in _days(rule, month)]
            delta = adjust.get(s.name, 0)  # 公休の足し引き（配った後の直しの例外。12章）
            lo = lo0 + delta if lo0 is not None else None
            hi = hi0 + delta if hi0 is not None else None
            if s.name in rule.meta.get("except_min", ()):   # 名指しのルールが下限を言った人（with_overrides）
                lo = None
            if s.name in rule.meta.get("except_max", ()):
                hi = None
            if lo is None and hi is None:
                continue
            out.append(Instance(rule, s.name, terms, lo, hi))
    elif k == "sequence" and "next2_in" in p:
        # 2日先の並び。if の記号の翌日が then なら、その次の日は next2_in（明けを休みと書く施設の「夜勤・休み・休み」）。
        # x[d,if] + x[d+1,then] - x[d+2,next2] <= 1。前の月の末の2日にもまたがる（月の外の日は前の月の表の値）
        a, t, c = _syms(p["if"], facility), _syms(p["then"], facility), _syms(p["next2_in"], facility)
        first = -1 if not rule.when else 1
        for s in people:
            for d in range(first, month.days - 1):
                if d >= 1 and rule.when and d not in rule.when:
                    continue
                terms = [Term(s.name, d, a, 1), Term(s.name, d + 1, t, 1), Term(s.name, d + 2, c, -1)]
                out.append(Instance(rule, f"{s.name}/{d}", terms, None, 1))
    elif k == "sequence":
        # if の記号の翌日（または前日）の中身
        a = _syms(p["if"], facility)
        for s in people:
            for d in _days(rule, month):
                if "next_in" in p:      # x[d,if] <= x[d+1,next]（翌日が月の外なら課さない）
                    if d + 1 > month.days:
                        continue
                    terms = [Term(s.name, d, a, 1), Term(s.name, d + 1, _syms(p["next_in"], facility), -1)]
                    out.append(Instance(rule, f"{s.name}/{d}", terms, None, 0))
                if "next_not_in" in p:  # x[d,if] + x[d+1,next] <= 1
                    if d + 1 > month.days:
                        continue
                    terms = [Term(s.name, d, a), Term(s.name, d + 1, _syms(p["next_not_in"], facility))]
                    out.append(Instance(rule, f"{s.name}/{d}", terms, None, 1))
                if "prev_in" in p:      # x[d,if] <= x[d-1,prev]
                    terms = [Term(s.name, d, a, 1), Term(s.name, d - 1, _syms(p["prev_in"], facility), -1)]
                    out.append(Instance(rule, f"{s.name}/{d}", terms, None, 0))
        # 月またぎ。前の月の末日の if の後ろ（1日）も見る
        if "next_in" in p or "next_not_in" in p:
            for s in people:
                if 1 not in _days(rule, month) and rule.when:
                    continue
                if "next_in" in p:
                    terms = [Term(s.name, 0, a, 1), Term(s.name, 1, _syms(p["next_in"], facility), -1)]
                    out.append(Instance(rule, f"{s.name}/0", terms, None, 0))
                if "next_not_in" in p:
                    terms = [Term(s.name, 0, a), Term(s.name, 1, _syms(p["next_not_in"], facility))]
                    out.append(Instance(rule, f"{s.name}/0", terms, None, 1))
    elif k == "thin_even":
        pass   # 場面を持たない。一から作る時の目的だけ（solver._thin_even）
    elif k == "unit_apart":
        # その勤務の人を1つのユニットに固めない。どのユニットの外にも1人はいる（2人以上なら2つ以上のユニットに分かれる）
        syms = _syms(p["shifts"], facility)
        for d in _days(rule, month):
            for u in facility.units:
                outside = [s for s in people if month.support.get(f"{s.name}/{d}", s.unit) != u]
                if outside:
                    out.append(Instance(rule, f"{u}の外/{d}", [Term(s.name, d, syms) for s in outside], 1, None))
    elif k == "unit_after":
        # 前の日に after の勤務の人（明け番）がいるユニットは yes の勤務を1人、いないユニットは no の勤務を1人。
        # yes(d) − after(d−1) = 0、no(d) + after(d−1) = 1。ユニットの after が2人の日は作れない（夜勤は各ユニット1人までの施設の形）
        after, yes, no = _syms(p["after"], facility), _syms(p["yes"], facility), _syms(p["no"], facility)

        def in_unit(u, d):   # 応援の日は、その日に入るユニットで数える
            return [s for s in people if month.support.get(f"{s.name}/{d}", s.unit) == u]

        for u in dict.fromkeys(s.unit for s in people):
            for d in _days(rule, month):
                if d == 1 and not month.prev_tail:
                    continue   # 前の月の末が分からなければ、1日の明け番は分からない
                today, before = in_unit(u, d), in_unit(u, d - 1)
                out.append(Instance(rule, f"{u}（明け番あり）/{d}", [Term(s.name, d, yes) for s in today]
                                    + [Term(s.name, d - 1, after, -1) for s in before], 0, 0))
                out.append(Instance(rule, f"{u}（明け番なし）/{d}", [Term(s.name, d, no) for s in today]
                                    + [Term(s.name, d - 1, after, 1) for s in before], 1, 1))
    elif k == "max_run":
        syms = _syms(p["shifts"], facility)
        m = p["max"]
        tail = len(next(iter(month.prev_tail.values()), []))
        skip = set(rule.meta.get("except_who", ()))   # 名指しのゆるい上限の人（_run_exceptions）
        for s in people:
            if s.name in skip:
                continue
            # 窓 [start, start+m]（m+1 日）に m を超えて入れない。前の月の末日も含める
            for start in range(1 - tail, month.days - m + 1):
                days = list(range(start, start + m + 1))
                if days[-1] < 1:
                    continue  # 前の月だけの窓は数えない
                terms = [Term(s.name, d, syms) for d in days]
                out.append(Instance(rule, f"{s.name}/{days[0]}-{days[-1]}", terms, None, m))
        # 月末の夜勤は翌月1日の明けまで続く。月末の m 日がすべて勤務で末日が夜勤だと、翌月1日で上限を超える
        # （月の中だけを見て作ると、翌月の表が作れなくなる。2026-11-02 に12月で起きた）
        ake = [st.symbol for st in facility.shift_types if st.kind == "ake"]
        night = [st.symbol for st in facility.shift_types if st.next_is_ake]
        if ake and night and ake[0] in syms and month.days >= m:
            last = list(range(month.days - m + 1, month.days + 1))
            for s in people:
                terms = [Term(s.name, d, syms) for d in last] + [Term(s.name, month.days, night)]
                out.append(Instance(rule, f"{s.name}/{last[0]}-翌月1", terms, None, m))
    elif k == "weekday":
        wd = set(p["weekdays"])
        dated = rule.meta.get("dated", {})   # 同じ人の日付を決めた頼みのある日（_weekday_dated）。曜日の頼みはその日に譲る
        for s in people:
            for d in _days(rule, month):
                if month.weekday(d) not in wd or d in dated.get(s.name, ()):
                    continue
                if "not_in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["not_in"], facility))], None, 0))
                if "in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["in"], facility))], 1, None))
    elif k == "cell":
        for s in people:
            for d in _days(rule, month):
                if "in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["in"], facility))], 1, None))
                if "not_in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["not_in"], facility))], None, 0))
    elif k == "layer":
        pass  # 待機などの層。表のマスには式をかけない（割り当ては standby.assign）
    else:
        raise ValueError(f"まだ扱わない種類 {k}")
    return out


def is_day_off_wish(rule: Rule, facility: Facility) -> bool:
    """希望休か（本人が日付を決めて頼んだ休み。本人の希望の強さで、人と日が決まり、置ける記号が休み・休暇だけのマスの頼み）。
    明けを「公」と書く施設で希望休の前の日を夜勤にしない頼み（build.impossible_wishes が足す）も、希望休に数える。"""
    if rule.kind != "cell" or rule.flex != "wish" or not rule.who or not rule.when:
        return False
    if rule.meta.get("derived") == "rest_not_ake":
        return True
    rest = {t.symbol for t in facility.shift_types if t.kind in ("rest", "leave")}
    want = set(rule.params.get("in") or [])
    return bool(want) and want <= rest


def cell_value(table: dict[str, list[str]], month: Month, staff: str, day: int) -> str | None:
    if day >= 1:
        row = table.get(staff)   # 名簿にいて表に行が無い人（先の月から加わる職員）は、どの勤務にも数えない
        return row[day - 1] if row is not None else None
    tail = month.prev_tail.get(staff, [])
    idx = len(tail) - 1 + day  # day=0 → 末日
    return tail[idx] if 0 <= idx < len(tail) else None


def evaluate(inst: Instance, table: dict[str, list[str]], month: Month) -> int:
    total = 0
    for t in inst.terms:
        v = cell_value(table, month, t.staff, t.day)
        if v is not None and v in t.symbols:
            total += t.coef
    return total


def broken(inst: Instance, table: dict[str, list[str]], month: Month) -> bool:
    v = evaluate(inst, table, month)
    return (inst.lo is not None and v < inst.lo) or (inst.hi is not None and v > inst.hi)


def check_table(rules: list[Rule], facility: Facility, staff: list[Staff], month: Month,
                table: dict[str, list[str]]) -> list[dict]:
    """表で破れている場面の一覧（ソルバーと同じ式で数える）。"""
    out = []
    for r in with_overrides(rules):
        for inst in expand(r, facility, staff, month):
            if broken(inst, table, month):
                out.append({"rule": r.id, "name": r.name, "flex": r.flex, "key": inst.key,
                            "value": evaluate(inst, table, month), "lo": inst.lo, "hi": inst.hi})
    return out
