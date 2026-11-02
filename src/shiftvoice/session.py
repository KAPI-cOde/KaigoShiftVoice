"""一言の流れ（画面1の裏側）。文字の一言 → 名前を伏せる → 読み取り → 札 → 狭い範囲の直し → 保存。

保存の順と立て直し（13章）
- 発言は読み取りに送る前に「未反映の発言」へ保存する（再起動しても残る）
- 採用したら 帳面 → 月の表（built_rev）→ 職員 → 未反映の発言を消す、の順に書く
- 帳面には書けたのに月の表を書く前に止まった時は、開いた時に帳面から表を直し直す
- 同じ発言（同じ ID）を送り直しても、帳面に二重に足さない（tx の op_id で見分ける）
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

from .build import build_original, month_rules
from .intake import Clarify, Proposal, to_proposal
from .ledger import Ledger
from .model import Rule
from .names import Masker
from .period import read_answer
from .reader import ReadError
from .resolve import changed_cells, resolve
from .store import Store

JST = dt.timezone(dt.timedelta(hours=9))
ASK_PERIOD = "毎月ですか、今月だけですか"


@dataclass
class Reply:
    kind: str                 # applied / ask_period / ask / error / failed_solve
    uid: str | None = None
    message: str = ""
    changes: list[dict] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    summary: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Session:
    def __init__(self, store: Store, year: int, month: int, reader, *, now=None, who: str = "作成者"):
        self.store, self.year, self.month, self.reader = store, year, month, reader
        self.now = now or (lambda: dt.datetime.now(JST))
        self.who = who

    # 開く
    @property
    def ym(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def _span(self) -> tuple[str, str]:
        last = calendar.monthrange(self.year, self.month)[1]
        return f"{self.ym}-01", f"{self.ym}-{last:02d}"

    def open(self) -> dict:
        """表が無ければ元の表を作る。保存の途中で止まっていれば帳面から直し直す。"""
        m = self.store.month(self.year, self.month)
        led = self.store.ledger()
        if m.table is None:
            r = build_original(self.store, self.year, self.month)
            if not r.ok:
                raise RuntimeError(r.message or "元の表が作れませんでした")
            m.table = r.result.table
            m.built_rev = led.rev
            self.store.save_month(m)
        applied = {t.get("op_id") for t in led.txs}
        pend = self.store.pending(self.year, self.month)
        if any(u["id"] in applied for u in pend) and (m.built_rev or 0) < led.rev:
            new = [Rule.from_dict(t["rule"]) for t in led.txs
                   if t["rev"] > (m.built_rev or 0) and t["op"] == "add"]
            rr = self._resolve(m, new)
            if rr.ok:
                m.table, m.built_rev = rr.table, led.rev
                self.store.save_month(m)
        for u in pend:
            if u["id"] in applied:
                self.store.resolve_pending(self.year, self.month, u["id"])
        return self.state()

    def _resolve(self, m, new_rules):
        fac, staff = self.store.facility(), self.store.staff_on(self.year, self.month)
        rules = month_rules(self.store, self.year, self.month, with_premise=False)
        ids = {r.id for r in new_rules}
        new = [r for r in rules if r.id in ids]
        return resolve(fac, staff, rules, m, m.table, new)

    # 話す
    def say(self, text: str) -> Reply:
        text = text.strip()
        u = self.store.add_pending(self.year, self.month, text, at=self.now().isoformat(timespec="seconds"),
                                   status="new")
        return self._read(u)

    def retry(self, uid: str) -> Reply:
        """［送り直す］。同じ発言 ID で読み直す。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None:
            return Reply("error", uid, "送り直す発言が見つかりません")
        if u.get("items") is not None:
            return self._propose(u)
        return self._read(u)

    def _read(self, u: dict) -> Reply:
        fac, staff = self.store.facility(), self.store.staff()
        masker = Masker(staff, fac)
        masked, unknown = masker.mask(u["text"])
        if unknown:
            self.store.update_pending(self.year, self.month, u["id"], status="ask_name", unknown=unknown)
            return Reply("ask", u["id"], f"名簿にない名前です（{'・'.join(unknown)}）。名簿から選んでください")
        try:
            items = masker.unmask(self.reader.read(masked))
        except ReadError as e:
            self.store.update_pending(self.year, self.month, u["id"], status="failed", error=str(e))
            return Reply("error", u["id"], "読み取りに失敗しました。表もルールも変えていません。［送り直す］を押してください")
        # 一度読んだら残し、AI を呼び直さない（3章 drafts）
        self.store.update_pending(self.year, self.month, u["id"], items=items, status="read")
        u = {**u, "items": items}
        return self._propose(u)

    def _propose(self, u: dict) -> Reply:
        fac, staff = self.store.facility(), self.store.staff()
        m = self.store.month(self.year, self.month)
        try:
            p = to_proposal(u["items"], u["text"], fac, staff, m)
        except Clarify as e:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], str(e))
        scope = u.get("scope") or p.scope
        if scope is None:
            self.store.update_pending(self.year, self.month, u["id"], status="ask_period")
            return Reply("ask_period", u["id"], ASK_PERIOD, summary=p.summary)
        return self._apply(u, p, scope)

    def answer(self, uid: str, text: str) -> Reply:
        """「毎月ですか、今月だけですか」への返事（話し言葉でも選択肢でも）。"""
        scope = text if text in ("rule", "once") else read_answer(text)
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None:
            return Reply("error", uid, "聞き返した発言が見つかりません")
        if scope is None:
            return Reply("ask_period", uid, ASK_PERIOD)
        self.store.update_pending(self.year, self.month, uid, scope=scope)
        return self._propose({**u, "scope": scope})

    def _apply(self, u: dict, p: Proposal, scope: str) -> Reply:
        m = self.store.month(self.year, self.month)
        led = self.store.ledger()
        start, end = self._span()
        at = self.now().isoformat(timespec="seconds")
        added: list[Rule] = []
        if not any(t.get("op_id") == u["id"] for t in led.txs):
            n = max([int(r[1:]) for r in led.rules_at() if r[1:].isdigit()] + [100]) + 1
            for rd in p.rules:
                rd = dict(rd)
                rd["id"] = f"r{n}"
                n += 1
                rd["scope"] = scope
                rd["meta"] = {**rd.get("meta", {}), "said": u["text"], "who": self.who, "at": at,
                              "reason": p.reason, "until": p.until or (end if scope == "once" else None),
                              "source": "話した", "uid": u["id"]}
                until = p.until or (end if scope == "once" else None)
                tx = led.append("add", rule=rd, op_id=u["id"], frm=start, until=until,
                                meta={"said": u["text"], "who": self.who, "at": at})
                added.append(Rule.from_dict(tx["rule"]))
        changes: list[dict] = []
        secs = 0.0
        if added:
            tmp = Ledger.from_list(led.to_list())
            rules = list(tmp.rules_at(between=self._span()).values())
            fac, staff = self.store.facility(), self.store.staff_on(self.year, self.month)
            ids = {r.id for r in added}
            rr = resolve(fac, staff, rules, m, m.table, [r for r in rules if r.id in ids])
            secs = rr.seconds
            if not rr.ok:
                self.store.resolve_pending(self.year, self.month, u["id"])
                return Reply("failed_solve", u["id"], rr.staged.message or "直せませんでした", seconds=secs)
            changes = rr.changes
            self.store.save_ledger(led)            # 1 帳面
            m.table, m.built_rev = rr.table, led.rev
            self.store.save_month(m)               # 2 月の表
        if p.staff_changes:
            staff = self.store.staff()
            for c in p.staff_changes:
                s = next(x for x in staff if x.name == c["staff"])
                if not any(x.get("from") == c["from"] and x.get("can_add") == c["can_add"] for x in s.changes):
                    s.changes.append({k: v for k, v in c.items() if k != "staff"} | {"at": at})
            self.store.save_staff(staff)           # 3 職員
        self.store.resolve_pending(self.year, self.month, u["id"])  # 4 未反映の発言
        return Reply("applied", u["id"], p.summary, changes=changes, rule_ids=[r.id for r in added],
                     summary=p.summary, seconds=secs)

    # 画面へ渡す
    def state(self) -> dict:
        m = self.store.month(self.year, self.month)
        led = self.store.ledger()
        rules = led.rules_at(between=self._span())
        fac, staff = self.store.facility(), self.store.staff()
        return {
            "year": self.year, "month": self.month, "days": m.days, "holidays": m.holidays,
            "weekdays": [m.weekday(d) for d in range(1, m.days + 1)],
            "staff": [{"name": s.name, "unit": s.unit, "changes": s.changes} for s in staff],
            "table": m.table,
            "cards": [{"id": r.id, "name": r.name, "flex": r.flex, "scope": r.scope, "who": r.who,
                       "when": r.when, "kind": r.kind, "params": r.params, "meta": r.meta}
                      for r in rules.values()],
            "pending": self.store.pending(self.year, self.month),
            "symbols": [{"symbol": st.symbol, "name": st.name} for st in fac.shift_types],
        }
