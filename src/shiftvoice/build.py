"""月の表を作る（帳面の決まり＋撮影用の前提）と、段の順（4章・試験 S4）。

段の順
- 段1 本人の希望と運用の都合を外せなくして解く（fixed は常に外せない）
- 作れないと分かった（infeasible）なら 段2 運用だけ守る → 段3 希望だけ守る → 段4 全部外す
- 時間切れ（作れないとは分かっていない）なら 段3 だけ試す。段4 へは自動で進まない。
  見つからなければ「この時間では見つかりませんでした」と言い、もう少し考えるか、どれを譲るかを聞く
"""
from __future__ import annotations

from dataclasses import dataclass, field

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


STAGE_NOTE = {
    2: "本人の希望を守ったままでは作れなかったので、希望を外せる形で作りました（運用の都合は守っています）",
    3: "運用の都合を外せる形で作りました（本人の希望は守っています）",
    4: "全部の決まりを外せる形で作りました（動かせない決まりだけ守っています）",
}


def solve_staged(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
                 **kw) -> StagedResult:
    """段の順で解く。秒は全段の合計で数え、段ごとの状態を残す。"""
    stages: list[dict] = []

    def run(stage: int) -> SolveResult:
        r = solve_month(facility, staff, rules, month, stage=stage, **kw)
        stages.append({"stage": stage, "status": r.status, "seconds": round(r.seconds, 3)})
        return r

    r = run(1)
    if r.status == "ok":
        return StagedResult(r, stages)
    if r.status == "infeasible":
        order = [2, 3, 4]
    else:  # 時間切れは段3だけ。希望を外す段（2・4）へは進まない
        order = [3]
    timed_out = r.status == "timeout"
    for st in order:
        r = run(st)
        if r.status == "ok":
            return StagedResult(r, stages, note=STAGE_NOTE[st])
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


def month_rules(store: Store, year: int, month: int, *, with_premise: bool = True) -> list[Rule]:
    import calendar
    last = calendar.monthrange(year, month)[1]
    ledger = store.ledger()
    rules = list(ledger.rules_at(between=(f"{year:04d}-{month:02d}-01",
                                          f"{year:04d}-{month:02d}-{last:02d}")).values())
    if with_premise:
        rules += premise_rules(store.premise(year, month))
    return rules


def build_original(store: Store, year: int, month: int, *, with_premise: bool = True) -> StagedResult:
    fac, staff = store.facility(), store.staff()
    m = store.month(year, month)
    rules = month_rules(store, year, month, with_premise=with_premise)
    return solve_staged(fac, staff, rules, m)
