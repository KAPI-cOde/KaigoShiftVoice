"""翌月へ進む流れ（台本の3幕）。ルールの見直し（画面8）→ 12月の表を返さない案・返す案で作る（画面6）→ 採る。

画面に出す文はコードが作る（札の表は3行。spec.md 画面8・画面6、計画 B8）。
"""
from __future__ import annotations

import calendar

from .build import month_rules
from .giveback import accepted_counts, build_pair
from .nextmonth import copy_next_month, next_ym
from .review import answer, history, review_cards, undo_answer
from .store import Store


def _md(date: str) -> str:
    return f"{int(date[5:7])}月{int(date[8:10])}日"


def card_lines(c, month: int, prev: int) -> dict:
    if c.reason == "expired":
        return {"label": "期限が来た", "title": c.name,
                "lines": [f"{_md(c.until)}で終わりました。{month}月には効きません",
                          f"片付けても、{prev}月の表と引き受けの記録は残ります"],
                "buttons": [["archive", "履歴へ片付ける（おすすめ）"], ["extend", "期間を延ばす"]]}
    return {"label": "先月増えた", "title": c.name, "lines": ["今月も使いますか"],
            "buttons": [["keep", "今月も使う"], ["change", "変える（話す）"]]}


ANSWER_WORDS = {"archive": "片付けた", "extend": "延ばした", "keep": "今月も使う", "change": "変えた"}


class NextFlow:
    def __init__(self, store: Store, year: int, month: int):
        """year, month は今の月（11月）。進む先は翌月（12月）。"""
        self.store, self.year, self.month = store, year, month
        self.ny, self.nm = next_ym(year, month)

    def _ensure(self):
        copy_next_month(self.store, self.year, self.month)

    def review(self) -> dict:
        self._ensure()
        led = self.store.ledger()
        v = review_cards(led, self.ny, self.nm)
        cards = []
        for c in v["cards"]:
            d = card_lines(c, self.nm, self.month)
            d.update({"rule_id": c.rule_id, "reason": c.reason, "said": c.said, "until": c.until,
                      "answer": v["answers"].get(c.rule_id, {}).get("answer")})
            cards.append(d)
        n = len(cards)
        summary = [f"{c['title']} → {ANSWER_WORDS[c['answer']]}" for c in cards if c["answer"]]
        return {"month": self.nm, "cards": cards, "cursor": v["cursor"], "total": n,
                "head": f"{self.nm}月の表を作る前に　見直すルール {n}件中{min(v['cursor'] + 1, n)}件目"
                        f"（残り{max(n - v['cursor'] - 1, 0)}件）" if v["cursor"] < n else
                        f"{self.nm}月の表を作る前に　見直しは済みました",
                "unchanged": f"変更なし {v['unchanged']}件（今回見直すのは、期限が来たルールと先月増えたルールだけ）",
                "once": f"{self.month}月だけの頼み{v['once']}件は、{self.nm}月へ引き継ぎません",
                "done": v["cursor"] >= n, "summary": summary,
                "history": history(led)}

    def answer(self, rule_id: str, what: str, new_until: str | None = None) -> dict:
        answer(self.store, self.ny, self.nm, rule_id, what, new_until=new_until)
        return self.review()

    def undo(self, rule_id: str) -> dict:
        undo_answer(self.store, self.ny, self.nm, rule_id)
        return self.review()

    def build(self, base_date: str | None = None) -> dict:
        """返さない案と返す案を同じ条件で作る。数えるのは今の月の引き受け（基準日は月末）。"""
        self._ensure()
        last = calendar.monthrange(self.year, self.month)[1]
        base_date = base_date or f"{self.year:04d}-{self.month:02d}-{last:02d}"
        ym = f"{self.year:04d}-{self.month:02d}"
        counts = accepted_counts(self.store.acceptances(), ym, base_date)
        dec = self.store.month(self.ny, self.nm)
        fac, staff = self.store.facility(), self.store.staff_on(self.ny, self.nm)
        gb = build_pair(fac, staff, month_rules(self.store, self.ny, self.nm, with_premise=False), dec, counts)
        out = {"status": gb.status, "message": gb.message, "counts": counts, "checks": gb.checks}
        if gb.status != "ok":
            return out
        who = max(counts, key=lambda n: (counts[n], n == "小島")) if counts else None
        up, down = gb.increased, gb.decreased
        moved = sum(a - b for b, a in up.values())
        out.update({
            "main": (f"{self.month}月に{counts[who]}回引き受け → {self.nm}月の土日祝の夜勤 "
                     f"{gb.before[who]}→{gb.after[who]}回") if who else "",
            "who": who,
            "sub": "夜勤の総数は同じ。土日祝の分をほかの人と配分し直す",
            "line2": f"減る{len(down)}人・増える{len(up)}人（1人2回まで）、増減は同じ{moved}回",
            "people": [{"name": n, "before": gb.before[n], "after": gb.after[n], "accepted": counts.get(n, 0)}
                       for n in gb.after],
            "seconds": round(gb.give.seconds + gb.base.seconds, 2),
            "tables": {"base": gb.base.table, "give": gb.give.table},
            "days": dec.days, "weekdays": [dec.weekday(d) for d in range(1, dec.days + 1)],
            "holidays": dec.holidays,
        })
        self._pair = gb
        m = self.store.month(self.ny, self.nm)
        m.layers["_plans"] = {"base": gb.base.table, "give": gb.give.table}
        self.store.save_month(m)
        return out

    def adopt(self, plan: str) -> dict:
        """［12月はこれで作る］。選んだ案を12月の表にする（施設が決める）。"""
        m = self.store.month(self.ny, self.nm)
        plans = m.layers.get("_plans")
        if not plans or plan not in plans:
            return {"error": "先に2つの案を作ってください"}
        m.table = plans[plan]
        m.built_rev = self.store.ledger().rev
        m.layers["_adopted"] = plan
        self.store.save_month(m)
        return {"ok": True, "plan": plan}
