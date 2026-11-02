"""月の表を作る（帳面の決まり＋撮影用の前提）。"""
from __future__ import annotations

from .model import Rule
from .solver import SolveResult, solve_month
from .store import Store


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
    ledger = store.ledger()
    rules = list(ledger.rules_at(on=f"{year:04d}-{month:02d}-01").values())
    if with_premise:
        rules += premise_rules(store.premise(year, month))
    return rules


def build_original(store: Store, year: int, month: int, *, with_premise: bool = True) -> SolveResult:
    fac, staff = store.facility(), store.staff()
    m = store.month(year, month)
    rules = month_rules(store, year, month, with_premise=with_premise)
    return solve_month(fac, staff, rules, m)
