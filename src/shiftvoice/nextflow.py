"""翌月へ進む流れ（台本の3幕）。ルールの見直し（画面8）→ 12月の表を返さない案・返す案で作る（画面6）→ 採る。

画面に出す文はコードが作る（札の表は3行。spec.md 画面8・画面6、計画 B8）。
"""
from __future__ import annotations

import calendar

from .build import month_rules
from .giveback import _months_back, accepted_counts, build_pair
from .giveback import settings as gb_settings
from .intake import Clarify, to_proposal
from .ledger import LedgerError
from .names import Masker
from .nextmonth import copy_next_month, next_ym
from .reader import ReadError
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
    # 当番（待機など）の中身を話して直すのは、まだ作っていない。［変える］を出さない
    buttons = [["keep", "今月も使う"]] + ([] if c.kind == "layer" else [["change", "変える（話す）"]])
    return {"label": "先月増えた", "title": c.name, "lines": ["今月も使いますか"], "buttons": buttons}


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
        def word(c):
            a = v["answers"][c["rule_id"]]
            return ANSWER_WORDS[c["answer"]] + (f"（{a['new_name']}）" if a.get("new_name") else "")
        summary = [f"{c['title']} → {word(c)}" for c in cards if c["answer"]]
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

    def change(self, rule_id: str, text: str, reader) -> dict:
        """［変える（話す）］。先月増えた札を、話した（書いた）一言の中身に置き換える（M8）。
        読み取りは1幕の一言と同じ（名前を伏せて送る・型はコードが確かめる）。読めない時は札を変えない。"""
        self._ensure()
        text = (text or "").strip()
        if not text:
            return {"error": "変え方を話すか、文字で入れてください"}
        fac, staff = self.store.facility(), self.store.staff()
        masker = Masker(staff, fac)
        masked, unknown = masker.mask(text)
        if unknown:
            return {"error": f"名簿にない名前です（{'・'.join(unknown)}）。ルールは変えていません"}
        try:
            items = masker.unmask(reader.read(masked))
            p = to_proposal(items, text, fac, staff, self.store.month(self.ny, self.nm))
        except ReadError:
            return {"error": "読み取りに失敗しました。ルールは変えていません"}
        except Clarify as e:
            return {"error": str(e)}
        if p.kind != "rule" or len(p.rules) != 1:
            return {"error": "このルールの変え方を1つだけ言ってください。ルールは変えていません"}
        try:
            answer(self.store, self.ny, self.nm, rule_id, "change", new_rule=p.rules[0], said=text,
                   new_until=p.until)
        except LedgerError as e:
            return {"error": str(e)}
        return self.review()

    def undo(self, rule_id: str) -> dict:
        undo_answer(self.store, self.ny, self.nm, rule_id)
        return self.review()

    def build(self, base_date: str | None = None) -> dict:
        """返さない案と返す案を同じ条件で作る。数えるのは返していない引き受け（さかのぼるのは施設の設定の
        window_months か月。基準日は今の月の末）。"""
        self._ensure()
        last = calendar.monthrange(self.year, self.month)[1]
        base_date = base_date or f"{self.year:04d}-{self.month:02d}-{last:02d}"
        ym = f"{self.year:04d}-{self.month:02d}"
        fac, staff = self.store.facility(), self.store.staff_on(self.ny, self.nm)
        window = gb_settings(fac)["window_months"]
        items = self.store.acceptances()
        counts = accepted_counts(items, ym, base_date, window)
        dec = self.store.month(self.ny, self.nm)
        gb = build_pair(fac, staff, month_rules(self.store, self.ny, self.nm, with_premise=False), dec, counts)
        out = {"status": gb.status, "message": gb.message, "counts": counts, "checks": gb.checks}
        if gb.status != "ok":
            return out
        who = max(gb.people, key=lambda p: (p["accepted"], p["name"] == "小島"))
        parts = []
        if who["leave_after"] > who["leave_before"]:
            parts.append(f"有給 {who['leave_before']}→{who['leave_after']}日")
        if who["run_after"] < who["run_before"]:
            parts.append(f"いちばん長い連勤 {who['run_before']}→{who['run_after']}日")
        o = gb.others
        changed = sum(x != y for n in gb.base.table for x, y in zip(gb.base.table[n], gb.give.table[n]))
        out.update({
            "main": f"{who['name']}さん　{self.month}月に{who['accepted']}回引き受け → {self.nm}月は" + "・".join(parts),
            "who": who["name"],
            "sub": "ほかの人の有給と連勤は増やさない。人数とルールはそのまま",
            "line2": f"引き受けた{len(gb.people)}人に、" + {("leave", "run"): "有給を1日と短い連勤", ("leave",): "有給を1日",
                                                          ("run",): "短い連勤"}[tuple(gb.stage)]
                     + f"。返さない案から変わるのは{changed}マス",
            "people": gb.people,
            "others": o,
            "changed": changed,
            "leave_avg": round(gb.leave_avg, 1),
            "stage": list(gb.stage),
            "seconds": round(gb.give.seconds + gb.base.seconds, 2),
            "tables": {"base": gb.base.table, "give": gb.give.table},
            "days": dec.days, "weekdays": [dec.weekday(d) for d in range(1, dec.days + 1)],
            "holidays": dec.holidays,
        })
        self._pair = gb
        m = self.store.month(self.ny, self.nm)
        months = set(_months_back(ym, window))
        m.layers["_plans"] = {"base": gb.base.table, "give": gb.give.table, "rev": self.store.ledger().rev,
                              "tail": m.prev_tail,
                              "acc_ids": [x.id for x in items if x.date[:7] in months and x.date <= base_date
                                          and not x.returned_in and x.staff in counts]}
        self.store.save_month(m)
        return out

    def adopt(self, plan: str) -> dict:
        """［12月はこれで作る］。選んだ案を12月の表にする（施設が決める）。"""
        m = self.store.month(self.ny, self.nm)
        plans = m.layers.get("_plans")
        if not plans or plan not in ("base", "give"):
            return {"error": "先に2つの案を作ってください"}
        cur = self.store.month(self.year, self.month).table
        if plans.get("rev") != self.store.ledger().rev or plans.get("tail") != {n: r[-5:] for n, r in cur.items()}:
            # 案を作った後にルールか前の月の末が変わった。古い案は採らない（Astra の指摘7）
            return {"error": "案を作った後にルールか11月の表が変わりました。もう一度［12月の表を作る］を押してください"}
        m.table = plans[plan]
        m.built_rev = self.store.ledger().rev
        m.layers["_adopted"] = plan
        if plan == "give":  # 返した引き受けに印を付ける（返さない案を採ったら、次の月へ持ち越す）
            ids = set(plans.get("acc_ids") or [])
            items = self.store.acceptances()
            for x in items:
                if x.id in ids:
                    x.returned_in = f"{self.ny:04d}-{self.nm:02d}"
            self.store.save_acceptances(items)
        self.store.save_month(m)
        return {"ok": True, "plan": plan}
