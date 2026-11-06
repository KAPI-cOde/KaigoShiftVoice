"""月の表を作る（帳面の決まり＋撮影用の前提）と、段の順（4章・試験 S4）。

段の順
- 段1 本人の希望と運用の都合を外せなくして解く（fixed は常に外せない）
- 作れないと分かった（infeasible）なら 段3 希望だけ守る（運用の都合を外す）→ 段2 運用だけ守る（希望を外す）→ 段4 全部外す。
  運用の都合を先に外し、本人の希望休は最後まで守る（作者の答え 2026-11-06。前は希望を先に外していた）
- 時間切れ（作れないとは分かっていない）なら 段3 だけ試す。段4 へは自動で進まない。
  見つからなければ「この時間では見つかりませんでした」と言い、もう少し考えるか、どれを譲るかを聞く
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .model import Facility, Month, Rule, Staff
from .solver import SolveResult, solve_month
from .store import Store

TIMEOUT_MESSAGE = "この時間では見つかりませんでした。もう少し考えますか、どれを譲るかを決めますか"
# C3（簡素な対応。17章 A23）。芯は探さず、作れない・時間切れのどちらも一文で聞き返す
C3_MESSAGE = "このルールを入れると、この時間では見つかりませんでした。外しますか、ゆるめますか"


@dataclass
class StagedResult:
    result: SolveResult            # 最後に使った段の結果
    stages: list[dict] = field(default_factory=list)  # 段ごとの状態（段・状態・秒）
    message: str | None = None     # 作成者へ聞く一文（表が無い時だけ）
    note: str | None = None        # 表はできたが段を下げた時の知らせ

    @property
    def ok(self) -> bool:
        return self.result.status == "ok"

    @property
    def seconds(self) -> float:
        return sum(s["seconds"] for s in self.stages)


# 体の目安（次の勤務までの間が短い並びの罰）を外して作った時の知らせ。ルールは段1のまま全部守っている
SHORT_REST_NOTE = "時間内に、次の勤務までの間まで整えきれなかったので、間の目安を外して作りました（ルールは全部守っています）"

STAGE_NOTE = {
    2: "本人の希望を守ったままでは作れなかったので、希望を外せる形で作りました（運用の都合は守っています）",
    3: "運用の都合を外せる形で作りました（本人の希望は守っています）",
    4: "全部の決まりを外せる形で作りました（動かせない決まりだけ守っています）",
}


def broken_text(broken: list[dict], names: set[str], limit: int = 6) -> str:
    """段を下げて作った時に、実際に外れた本人の希望と運用の都合を一文で言う。人ごとの数のルールは、人と数まで言う。"""
    groups: dict[str, list[dict]] = {}
    for b in broken:
        if b["flex"] in ("operational", "wish"):
            groups.setdefault(b["name"], []).append(b)
    parts = []
    for name, items in groups.items():
        who = [b for b in items if b["key"] in names]
        if len(who) != len(items):
            parts.append(f"{name}が{len(items)}か所")
            continue
        ones = [f"{b['key']}さん{b['value']}" for b in who[:limit]] + ([f"ほか{len(who) - limit}人"] if len(who) > limit else [])
        same = items[0]["lo"] is not None and items[0]["lo"] == items[0]["hi"]
        parts.append(f"{name}{'（' + str(items[0]['lo']) + '）' if same else ''}が{'・'.join(ones)}")
    return "外れたのは、" + "、".join(parts) if parts else ""


def impossible_wishes(facility: Facility, rules: list[Rule], month: Month) -> tuple[list[Rule], list[str]]:
    """前の月の末の夜勤の明けと重なる1日の希望休と、動かせないマスと重なる希望休は、どう組んでも叶わない。残すと表全体が
    作れないと分かって段を下げ、関係のない人の希望休まで外れた（2026-11-06 通しの試し。1人の1日で10件外れた）。その日だけ外す。"""
    out, notes = [], []
    ake = {t.symbol for t in facility.shift_types if t.kind == "ake"}
    for r in rules:
        if not (r.kind == "cell" and r.flex == "wish" and r.when and r.who and len(r.who) == 1):
            out.append(r)
            continue
        who, want = r.who[0], set(r.params.get("in") or [])
        tail = month.prev_tail.get(who)
        bad: set[int] = set()
        if 1 in r.when and tail and tail[-1] in facility.symbols and facility.shift(tail[-1]).next_is_ake and not want & ake:
            bad.add(1)
            notes.append(f"{who}さんの1日は前の月の夜勤の明けなので、希望休を入れられません")
        for o in rules:   # 後から動かせない形で頼んだマス（希望休の日の「10日の〇〇さん、日8に」）。頼んだ方を残す
            if o.kind == "cell" and o.flex == "fixed" and o.who == [who] and o.params.get("in") and not want & set(o.params["in"]):
                for d in set(o.when or []) & set(r.when) - bad:
                    bad.add(d)
                    notes.append(f"{who}さんの{d}日は「{o.name}」を守るので、希望休を外しました")
        if set(r.when) - bad:
            out.append(replace(r, when=[d for d in r.when if d not in bad]) if bad else r)
    return out, notes


def solve_staged(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
                 **kw) -> StagedResult:
    """段の順で解く。叶わない希望休の知らせは、どの段で作れても添える。"""
    rules, notes = impossible_wishes(facility, rules, month)
    out = _solve_staged(facility, staff, rules, month, **kw)
    if notes:
        out.note = "。".join(notes + ([out.note] if out.note else []))
    return out


# 運用の都合の回数のルールを外す時の、1人あたりのずれの上限。小さい方から試す。3の次がすぐ無制限だと、公休9日の施設で
# 14日の希望休が1件あるだけで2人が公休26日・28日になった（Astra 2026-11-06 の指摘2）
DEV_CAPS = (1, 2, 3, 5, 8, 13, None)


def _solve_staged(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
                  **kw) -> StagedResult:
    """段の順で解く。秒は全段の合計で数え、段ごとの状態を残す。"""
    stages: list[dict] = []

    def run(stage: int) -> SolveResult:
        secs = 0.0
        for cap in DEV_CAPS if stage >= 3 else (None,):
            r = solve_month(facility, staff, rules, month, stage=stage, dev_cap=cap, **kw)
            secs += r.seconds
            if r.status == "ok":
                break
        stages.append({"stage": stage, "status": r.status, "seconds": round(secs, 3)})
        return r

    r = run(1)
    if r.status == "ok":
        return StagedResult(r, stages)
    # 体の目安（次の勤務までの間が短い並びの罰）を入れた施設で、段1が時間内に見つからない時は、ルールを緩める前に
    # 目安を外して段1で作り直す。ルールを守るのが先（2026-11-04、職員48人の試算で、目安のせいで段3へ下がり
    # 運用の都合を外していた。目安なしの表を出発点に磨く2段の作り方も試したが、48人では磨ききれず、32人では遅くなった）
    quality = facility.settings.get("quality", {})
    if r.status == "timeout" and quality.get("short_rest"):
        lite = replace(facility, settings={**facility.settings, "quality": {**quality, "short_rest": 0}})
        r2 = solve_month(lite, staff, rules, month, stage=1, **kw)
        stages.append({"stage": 1, "status": r2.status, "seconds": round(r2.seconds, 3), "without": "short_rest"})
        if r2.status == "ok":
            return StagedResult(r2, stages, note=SHORT_REST_NOTE)
        facility = lite   # 目安なしでも段1で作れない時は、目安を外したまま今までの段の順へ
        r = r2
    if r.status == "infeasible":
        order = [3, 2, 4]   # 運用の都合を先に外し、本人の希望は最後まで守る（作者の答え 2026-11-06）
    else:  # 時間切れは段3だけ。希望を外す段（2・4）へは進まない
        order = [3]
    timed_out = r.status == "timeout"
    for st in order:
        r = run(st)
        if r.status == "ok":
            detail = broken_text(r.broken, {s.name for s in staff})
            return StagedResult(r, stages, note=STAGE_NOTE[st] + ("。" + detail if detail else ""))
        timed_out = timed_out or r.status == "timeout"
    return StagedResult(r, stages, message=TIMEOUT_MESSAGE if timed_out else C3_MESSAGE)


def premise_rules(premise: dict | None) -> list[Rule]:
    """撮影用の前提を、元の表を作る時だけの決まりにする（帳面には入れない）。"""
    if not premise:
        return []
    out = []
    for i, p in enumerate(premise.get("pins", []), 1):
        out.append(Rule(id=f"p-pin{i}", ver=1, name=f"前提 {p['who']}さん {p['day']}日は{p['symbol']}",
                        kind="cell", flex="fixed", params={"in": [p["symbol"]]},
                        who=[p["who"]], when=[p["day"]], scope="once", meta={"premise": True}))
    for i, c in enumerate(premise.get("conditions", []), 1):
        out.append(Rule(id=f"p-cond{i}", ver=1, name=c["name"], kind=c["kind"], flex="fixed",
                        params=c["params"], who=c.get("who"), when=c.get("when"), scope="once",
                        meta={"premise": True}))
    return out


def clip_to_month(rule: Rule, start: str, end: str) -> Rule:
    """効く期間（from・until）が月の途中で始まる・終わる時は、その日までだけに効かせる。
    適用する日は対象の表の日付で判定する（13章）。期限つきの回数は、その月の初めから終わりの日までの累計になる"""
    frm, until = rule.meta.get("from"), rule.meta.get("until")
    last = int(end[-2:])
    lo = int(frm[-2:]) if frm and start < frm <= end else 1
    hi = int(until[-2:]) if until and start <= until < end else last
    if lo == 1 and hi == last:
        return rule
    days = [d for d in (rule.when or range(1, last + 1)) if lo <= d <= hi]
    import copy
    r = copy.deepcopy(rule)
    r.when = days
    return r


def month_rules(store: Store, year: int, month: int, *, with_premise: bool = True) -> list[Rule]:
    import calendar
    last = calendar.monthrange(year, month)[1]
    ledger = store.ledger()
    start, end = f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"
    rules = [clip_to_month(r, start, end) for r in ledger.rules_at(between=(start, end)).values()]
    if with_premise:
        rules += premise_rules(store.premise(year, month))
    return rules


def build_original(store: Store, year: int, month: int, *, with_premise: bool = True) -> StagedResult:
    fac, staff = store.facility(), store.staff_on(year, month)
    m = store.month(year, month)
    rules = month_rules(store, year, month, with_premise=with_premise)
    return solve_staged(fac, staff, rules, m)
