"""変更の帳面。決まりの正はここ（書き足すだけで消さない）。札の一覧は帳面から作る。

1件の tx は add / change / remove / exclude と、取り消し（undo）。通し番号 rev を振る。
確定した月はその時の rev で読むので、後の変更で確定した月の意味が変わらない。
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field

from .model import Rule

OPS = ("add", "change", "remove", "exclude", "undo")


class LedgerError(Exception):
    pass


@dataclass
class Ledger:
    txs: list[dict] = field(default_factory=list)

    @property
    def rev(self) -> int:
        return self.txs[-1]["rev"] if self.txs else 0

    def append(self, op: str, *, rule: dict | None = None, rule_id: str | None = None,
               ver: int | None = None, target_rev: int | None = None, op_id: str | None = None,
               frm: str | None = None, until: str | None = None, meta: dict | None = None) -> dict:
        """1件足す。当てられない tx は足さずに LedgerError（全部当たるか、1つも当てない）。"""
        if op not in OPS:
            raise LedgerError(f"知らない操作 {op}")
        tx = {"rev": self.rev + 1, "op": op, "op_id": op_id, "from": frm, "until": until,
              "meta": meta or {}}
        if op == "add":
            if not rule or "id" not in rule:
                raise LedgerError("add には rule が要る")
            if rule["id"] in self.rules_at():
                raise LedgerError(f"同じ id の決まりがある {rule['id']}")
            r = dict(rule)
            r.setdefault("ver", 1)
            Rule.from_dict(r)  # 形の点検
            tx["rule"] = r
        elif op in ("change", "remove", "exclude"):
            cur = self.rules_at().get(rule_id or "")
            if cur is None:
                raise LedgerError(f"当てる先の決まりがない {rule_id}")
            if ver is not None and cur.ver != ver:
                raise LedgerError(f"版が違う {rule_id} {cur.ver}≠{ver}")
            tx["rule_id"] = rule_id
            tx["ver"] = cur.ver
            if op == "change":
                if not rule:
                    raise LedgerError("change には新しい中身が要る")
                tx["rule"] = dict(rule)
            if op == "exclude":
                tx["rule"] = dict(rule or {})  # 例 {"staff": ["杉山"]}
        elif op == "undo":
            if target_rev is None or not any(t["rev"] == target_rev for t in self.txs):
                raise LedgerError("取り消す tx がない")
            tx["target_rev"] = target_rev
        self.txs.append(tx)
        return tx

    def _active(self, at_rev: int) -> list[dict]:
        """取り消し（と取り消しの取り消し）を解いて、効いている tx を返す。"""
        txs = [t for t in self.txs if t["rev"] <= at_rev]
        cancelled: dict[int, bool] = {}
        for t in reversed(txs):  # 後の取り消しが先の取り消しを打ち消す
            if t["op"] == "undo" and not cancelled.get(t["rev"], False):
                cancelled[t["target_rev"]] = not cancelled.get(t["target_rev"], False)
        return [t for t in txs if t["op"] != "undo" and not cancelled.get(t["rev"], False)]

    def rules_at(self, at_rev: int | None = None, on: str | None = None) -> dict[str, Rule]:
        """rev の時点の札の一覧。on（YYYY-MM-DD）を渡すと、その日に効いている物だけ。"""
        at_rev = self.rev if at_rev is None else at_rev
        rules: dict[str, dict] = {}
        spans: dict[str, tuple[str | None, str | None]] = {}
        excluded: dict[str, list[str]] = {}
        for t in self._active(at_rev):
            if t["op"] == "add":
                rules[t["rule"]["id"]] = copy.deepcopy(t["rule"])
                spans[t["rule"]["id"]] = (t.get("from"), t.get("until"))
            elif t["op"] == "change" and t["rule_id"] in rules:
                r = rules[t["rule_id"]]
                r.update(copy.deepcopy(t["rule"]))
                r["ver"] = r.get("ver", 1) + 1
                if t.get("until") is not None or t.get("from") is not None:
                    spans[t["rule_id"]] = (t.get("from"), t.get("until"))
            elif t["op"] == "remove":
                rules.pop(t["rule_id"], None)
            elif t["op"] == "exclude" and t["rule_id"] in rules:
                excluded.setdefault(t["rule_id"], []).extend(t["rule"].get("staff", []))
        out: dict[str, Rule] = {}
        for rid, r in rules.items():
            frm, until = spans.get(rid, (None, None))
            if on is not None and ((frm and on < frm) or (until and on > until)):
                continue
            rr = Rule.from_dict(r)
            rr.meta = {**rr.meta, "from": frm, "until": until}
            if rid in excluded and rr.who:
                rr.who = [w for w in rr.who if w not in excluded[rid]]
            out[rid] = rr
        return out

    def to_list(self) -> list[dict]:
        return copy.deepcopy(self.txs)

    @classmethod
    def from_list(cls, txs: list[dict]) -> "Ledger":
        return cls(txs=copy.deepcopy(txs))


def today() -> str:
    return dt.date.today().isoformat()
