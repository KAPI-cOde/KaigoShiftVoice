"""一言の流れ（画面1の裏側）。文字の一言 → 名前を伏せる → 読み取り → 札 → 狭い範囲の直し → 保存。

保存の順と立て直し（13章）
- 発言は読み取りに送る前に「未反映の発言」へ保存する（再起動しても残る）
- 採用したら 帳面 → 月の表（built_rev）→ 職員 → 未反映の発言を消す、の順に書く
- 帳面には書けたのに月の表を書く前に止まった時は、開いた時に帳面から表を直し直す
- 同じ発言（同じ ID）を送り直しても、帳面に二重に足さない（tx の op_id で見分ける）
"""
from __future__ import annotations

import calendar
import json
import datetime as dt
from dataclasses import dataclass, field

from .build import build_original, clip_to_month, month_rules
from .intake import Clarify, Proposal, to_proposal
from .ledger import Ledger
from .model import Rule
from .names import Masker
from .period import read_answer
from .reader import ReadError
from .giveback import accepted_counts
from .model import Acceptance
from .resolve import changed_cells, resolve
from .standby import ASK_WIDER, assign, duty_days, fix_absence
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
    warning: str = ""        # 守れなかった新しいルール・段を下げた知らせ（赤で出す）
    extra: dict = field(default_factory=dict)  # 場面ごとの添え物（待機の人・引き受けの回数など）

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Session:
    def __init__(self, store: Store, year: int, month: int, reader, *, now=None, who: str = "作成者",
                 builder=None, base_date: str | None = None):
        self.store, self.year, self.month, self.reader = store, year, month, reader
        self.builder = builder
        self.now = now or (lambda: dt.datetime.now(JST))
        self.who = who
        self.base_date = base_date  # 引き受けを数える基準日（デモ設定。None は今日）

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
            if u["id"] in applied and u.get("status") == "offer":
                # 案を当てる途中で止まった。記録と表を案どおりにそろえる（何度呼んでも同じ）
                self._commit_offer(u, self.store.ledger(), self.store.month(self.year, self.month))
            elif u["id"] in applied:
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
            # 名簿にない名前を含む一言は送らない。近い名前は手元の名簿から出し、作成者が選ぶまで表を動かさない（13章。T2-02）
            wrong = unknown[0]
            cands = masker.suggest(wrong)
            cands = cands if len(cands) == 1 else []   # 1人に絞れる時だけ候補にする（森田→石田・岡田 のような当て推量を出さない）
            self.store.update_pending(self.year, self.month, u["id"], status="ask_name", unknown=unknown)
            msg = (f"{wrong}さんは名簿にいません。{'・'.join(c + 'さん' for c in cands)}ですか" if cands else
                   f"{wrong}さんは名簿にいません。名簿から選んでください")
            return Reply("ask", u["id"], msg, extra={"ask_name": {"wrong": wrong, "candidates": cands,
                                                                  "roster": [x.name for x in staff]}})
        try:
            items = masker.unmask(self.reader.read(masked))
            if self.builder and any(it.get("type") == "custom" for it in items):
                # 型に収まらない一言（表に無い当番など）は組み立て役へ（8章）
                b = masker.unmask(self.builder.build(masked))
                items = ([{"type": "layer_def", "def": b["layer"]}] if "layer" in b
                         else [{"type": "unclear", "question": b.get("question")}])
        except ReadError as e:
            self.store.update_pending(self.year, self.month, u["id"], status="failed", error=str(e))
            return Reply("error", u["id"], "読み取りに失敗しました。表もルールも変えていません")
        # 一度読んだら残し、AI を呼び直さない（3章 drafts）
        self.store.update_pending(self.year, self.month, u["id"], items=items, status="read")
        u = {**u, "items": items}
        return self._propose(u)

    def choose_name(self, uid: str, wrong: str, right: str) -> Reply:
        """名前の聞き返しで名簿から選んだ。文の名前を置き換えてから読む（選ぶ前は何も送っていない）。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None or u.get("status") != "ask_name":
            return Reply("error", uid, "名前を選ぶ発言が見つかりません")
        if right not in {s.name for s in self.store.staff()} or wrong not in u["text"]:
            return Reply("error", uid, "名簿の名前から選んでください")
        text = u["text"].replace(wrong, right)
        self.store.update_pending(self.year, self.month, uid, text=text, heard=u["text"], status="new",
                                  unknown=None)
        return self._read({**u, "text": text})

    def _leave_caps(self) -> dict[str, int]:
        """人ごとの有給の上限（職員と対象月を持つ回数の決まり。13章）。"""
        fac = self.store.facility()
        lv = next(st.symbol for st in fac.shift_types if st.kind == "leave")
        caps: dict[str, int] = {}
        for r in self.store.ledger().rules_at(between=self._span()).values():
            if (r.kind == "count" and r.params.get("shifts") == [lv] and "max" in r.params
                    and r.who and len(r.who) == 1 and r.flex == "operational"):
                caps[r.who[0]] = r.params["max"]
        return caps

    def _propose(self, u: dict) -> Reply:
        fac, staff = self.store.facility(), self.store.staff()
        m = self.store.month(self.year, self.month)
        try:
            p = to_proposal(u["items"], u["text"], fac, staff, m, leave_caps=self._leave_caps())
        except Clarify as e:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], str(e))
        if p.kind == "cancel":
            return self._cancel(u, p)
        if p.kind == "cells" and p.rules and not m.distributed and all(r["flex"] == "negotiable" for r in p.rules):
            # 「できれば」の頼みは、守れるかを先に試し、守れる時は代わりに動くマスを見せて選んでもらう（T2-08）
            return self._fix_offer(u, p, m, maybe=True)
        if m.distributed and p.kind == "cells":
            offer = self._standby_offer(u, p, m)
            return offer if offer is not None else self._fix_offer(u, p, m)
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
        # 言われた変更が全部もう入っている時だけ「もう入っています」（10章。変更を飛ばす作りは希望が消える道になったのでしない）
        cur = led.rules_at(between=self._span()).values()
        def same(rd):
            return any(r.kind == rd["kind"] and r.params == rd["params"] and (r.who or None) == (rd.get("who") or None)
                       and r.when == rd.get("when") and r.scope == scope and r.flex == rd["flex"] for r in cur)
        if p.rules and not p.staff_changes and all(same(rd) for rd in p.rules) \
                and not any(t.get("op_id") == u["id"] for t in led.txs):
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], f"{self.month}月はもう入っています")
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
        warning = ""
        if added and all(r.kind == "layer" for r in added):
            fac = self.store.facility()
            pins = {int(k): v for k, v in ((self.store.premise(self.year, self.month) or {})
                                            .get("standby_pins", {})).items()}
            for r in added:
                d = r.params["def"]
                m.layers[d["id"]] = {"def": d, "rule_id": r.id,
                                     "assign": assign(d, fac, self.store.staff_on(self.year, self.month),
                                                      m, m.table, pins=pins)}
            self.store.save_ledger(led)
            m.history.append({"op_id": u["id"], "table": m.table, "built_rev": m.built_rev,
                              "said": u["text"], "layers": [r.params["def"]["id"] for r in added]})
            m.built_rev = led.rev
            self.store.save_month(m)
        elif added:
            tmp = Ledger.from_list(led.to_list())
            rules = [clip_to_month(r, *self._span()) for r in tmp.rules_at(between=self._span()).values()]
            fac, staff = self.store.facility(), self.store.staff_on(self.year, self.month)
            ids = {r.id for r in added}
            rr = resolve(fac, staff, rules, m, m.table, [r for r in rules if r.id in ids])
            secs = rr.seconds
            if not rr.ok:
                self.store.resolve_pending(self.year, self.month, u["id"])
                return Reply("failed_solve", u["id"], rr.staged.message or "直せませんでした", seconds=secs)
            changes = rr.changes
            # 守れなかった新しいルールと、段を下げたことは黙らずに知らせる（7章。Astra 2026-11-02 の指摘1）
            names = {r.id: r.name for r in added}
            missed = sorted({names[b["rule"]] for b in rr.staged.result.broken if b["rule"] in names})
            notes = [rr.staged.note] if rr.staged.note else []
            if missed:
                notes.insert(0, f"{'・'.join(missed)}は、ほかのルールとぶつかって守れませんでした")
            warning = "。".join(notes)
            self.store.save_ledger(led)            # 1 帳面
            m.history.append({"op_id": u["id"], "table": m.table, "built_rev": m.built_rev,
                              "said": u["text"]})
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
                     summary=p.summary, seconds=secs, warning=warning)

    def _cancel(self, u: dict, p: Proposal) -> Reply:
        """取り消し。前の頼みの指定を外すだけで、表のマスは動かさない（外した日が公休のままでも誤りではない。13章）。
        当てる先が1つに決まる時だけ当て、無いか複数なら聞き返す。"""
        import re
        led = self.store.ledger()
        m = self.store.month(self.year, self.month)
        if any(t.get("op_id") == u["id"] for t in led.txs):
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("applied", u["id"], p.summary, summary=p.summary)
        rules = led.rules_at(between=self._span())
        drop: dict[str, set[int]] = {}
        for rq in p.removes:
            for w in rq["who"]:
                for d in rq["days"]:
                    hits = [r for r in rules.values() if r.kind == "cell" and r.who == [w] and r.when
                            and d in r.when and (rq["shift"] is None or rq["shift"] in
                                                 r.params.get("in", []) + r.params.get("not_in", []))]
                    if not hits:
                        self.store.resolve_pending(self.year, self.month, u["id"])
                        return Reply("ask", u["id"], f"{w}さんの{d}日の頼みは見つかりませんでした。表もルールも変えていません")
                    if len(hits) > 1:
                        self.store.resolve_pending(self.year, self.month, u["id"])
                        return Reply("ask", u["id"], f"{w}さんの{d}日の頼みが{len(hits)}件あります（"
                                     + "・".join(h.name for h in hits) + "）。どれを取り消すか言ってください")
                    drop.setdefault(hits[0].id, set()).add(d)
        at = self.now().isoformat(timespec="seconds")
        meta = {"said": u["text"], "who": self.who, "at": at, "kind": "cancel"}
        for rid, days in drop.items():
            r = rules[rid]
            rest = [d for d in r.when if d not in days]
            if not rest:
                led.append("remove", rule_id=rid, op_id=u["id"], meta=meta)
            else:
                name = re.sub(r"[\d・]+日", "・".join(map(str, rest)) + "日", r.name, count=1)
                led.append("change", rule_id=rid, rule={"when": rest, "name": name}, op_id=u["id"], meta=meta)
        self.store.save_ledger(led)                # 1 帳面
        m.history.append({"op_id": u["id"], "table": m.table, "built_rev": m.built_rev, "said": u["text"]})
        m.built_rev = led.rev
        self.store.save_month(m)                   # 2 月の表（マスは動かさない。戻す時のために履歴だけ）
        self.store.resolve_pending(self.year, self.month, u["id"])
        return Reply("applied", u["id"], f"{p.summary}。表のマスはそのままです",
                     summary=f"{p.summary}。表のマスはそのままです",
                     rule_ids=list(drop))

    def undo(self) -> Reply:
        """［ひとつ戻す］。最後に採用した一言の札を取り消し（帳面に取り消しの1行）、表を直す前に戻す。"""
        m = self.store.month(self.year, self.month)
        if not m.history:
            return Reply("ask", None, "戻せる一言がありません")
        h = m.history.pop()
        led = self.store.ledger()
        for t in [t for t in led.txs if t.get("op_id") == h["op_id"] and t["op"] != "undo"]:
            led.append("undo", target_rev=t["rev"], op_id=f"undo-{h['op_id']}",
                       meta={"at": self.now().isoformat(timespec="seconds")})
        before = m.table
        self.store.save_ledger(led)
        m.table, m.built_rev = h["table"], led.rev
        for lid in h.get("layers", []):
            m.layers.pop(lid, None)
        if "rest_adjust" in h:   # 配った後の直しは、公休の足し引き・応援・引き受けの記録もまとめて戻す（16章の11）
            m.rest_adjust, m.support = h["rest_adjust"], h["support"]
        self.store.save_month(m)
        if h.get("acceptance_id"):
            self.store.save_acceptances([a for a in self.store.acceptances() if a.id != h["acceptance_id"]])
        return Reply("applied", h["op_id"], f"戻しました（「{h['said']}」）",
                     changes=changed_cells(before, m.table), summary="ひとつ戻す")

    # 配る・待機の人に頼む
    def distribute(self) -> None:
        """表を配った（ここから先の休みの電話は、待機の人に頼む直しになる）。"""
        m = self.store.month(self.year, self.month)
        m.distributed = True
        self.store.save_month(m)

    def _accepted(self, name: str) -> int:
        base = self.base_date or self.now().date().isoformat()
        return accepted_counts(self.store.acceptances(), self.ym, base).get(name, 0)

    def _standby_offer(self, u: dict, p, m) -> Reply | None:
        offs = [r for r in p.rules if r["kind"] == "cell" and r["params"] == {"in": ["公"]}]
        if len(offs) != 1 or len(p.rules) != 1 or len(offs[0]["who"] or []) != 1 or len(offs[0]["when"]) != 1:
            return None
        name, day = offs[0]["who"][0], offs[0]["when"][0]
        layer = next((L for L in m.layers.values() if day in duty_days(L["def"], m)), None)
        if layer is None:
            return None
        fac, staff = self.store.facility(), self.store.staff_on(self.year, self.month)
        rules = month_rules(self.store, self.year, self.month, with_premise=False)
        duty = layer["assign"].get(str(day), [])
        r = fix_absence(fac, staff, rules, m, m.table, name, day, duty)
        if r["status"] == "already_off":
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], r["message"])
        if r["status"] != "ok":
            self.store.update_pending(self.year, self.month, u["id"], status="wider",
                                      absence={"requester": name, "day": day, "layer": layer["def"]["id"],
                                               "tried": list(duty)})
            return Reply("ask", u["id"], ASK_WIDER, extra={"wider": True})
        return self._make_offer(u, m, name, day, r, layer["def"]["id"])

    def _make_offer(self, u: dict, m, name: str, day: int, r: dict, layer_id: str, wider: bool = False) -> Reply:
        """頼む人が決まった案（待機の人、または案Eで広げた休みの人）。［了承をもらって直す］まで表は変えない。"""
        helper = r["duty"]
        offer = {"requester": name, "day": day, "duty": helper, "table": r["table"], "changes": r["changes"],
                 "rest": r["rest"], "rest_adjust": r["rest_adjust"], "support": r["support"],
                 "layer": layer_id, "wider": wider, "base_rev": self.store.ledger().rev,
                 "base_table": json.dumps(m.table),
                 "rules": [{"name": f"{name}さん {day}日 休み", "kind": "cell", "flex": "wish",
                            "params": {"in": ["公"]}, "who": [name], "when": [day]}], "scope": "once"}
        self.store.update_pending(self.year, self.month, u["id"], status="offer", offer=offer)
        n = self._accepted(helper)
        rb, ra = r["rest"][helper]
        who = f"待機ではない、その日休みの{helper}さん" if wider else f"その日の待機の{helper}さん"
        return Reply(
                     "standby_offer", u["id"], f"{who}に頼む案です（{len(r['changes'])}マス）",
                     changes=r["changes"], seconds=r["seconds"],
                     summary=f"公休　{name}さん {r['rest'][name][0]}日 → {r['rest'][name][1]}日　"
                             f"{helper}さん {rb}日 → {ra}日",
                     extra={"duty": helper, "requester": name, "day": day, "accepted": [n, n + 1]})

    def accept(self, uid: str) -> Reply:
        """［了承をもらって直す］（待機の人に頼む案）と［これで直す］（配った後のほかの直し）。
        待機の案の時だけ引き受けの記録が増える（17章 A23 の13）。案を作った後に表か帳面が変わっていたら、
        古い案で上書きせずに作り直す（Astra 2026-11-02 の指摘1）。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None or u.get("status") != "offer":
            return Reply("error", uid, "了承をもらう案が見つかりません")
        o = u["offer"]
        m = self.store.month(self.year, self.month)
        led = self.store.ledger()
        if (led.rev != o.get("base_rev") or json.dumps(m.table) != o.get("base_table")) \
                and not any(t.get("op_id") == uid for t in led.txs):
            if o.get("wider") and u.get("absence"):
                self.store.update_pending(self.year, self.month, uid, status="wider", offer=None)
                r = self.wider(uid)
            else:
                self.store.update_pending(self.year, self.month, uid, status="read")
                r = self._propose({**u, "status": "read"})
            r.message = "表が変わったので、案を作り直しました。" + r.message
            return r
        before_n = self._accepted(o["duty"]) if o.get("duty") else 0
        self._commit_offer(u, led, m)
        if not o.get("duty"):
            return Reply("applied", uid, "直しました", changes=o["changes"], summary=o.get("summary", ""))
        after_n = self._accepted(o["duty"])
        return Reply("applied", uid, f"{o['duty']}さんの了承をもらって直しました", changes=o["changes"],
                     summary=f"{self.month}月の引き受け {before_n}回 → {after_n}回",
                     extra={"duty": o["duty"], "accepted": [before_n, after_n],
                            "note": "引き受け＝了承をもらって表を直した回数"})

    def _commit_offer(self, u: dict, led, m) -> None:
        """案を当てる。何度呼んでも同じ結果（保存の途中で止まった後の立て直しにも使う）。
        保存の順は 帳面 → 引き受けの記録 → 月の表 → 未反映の発言（記録が表より先。Astra の指摘3）。"""
        uid, o = u["id"], u["offer"]
        at = self.now().isoformat(timespec="seconds")
        start, end = self._span()
        if not any(t.get("op_id") == uid for t in led.txs):
            n = max([int(r[1:]) for r in led.rules_at() if r[1:].isdigit()] + [100]) + 1
            for rd in o["rules"]:
                rd = {**rd, "id": f"r{n}", "scope": o.get("scope", "once"),
                      "meta": {**rd.get("meta", {}), "said": u["text"], "who": self.who, "at": at, "reason": None,
                               "until": end, "source": o.get("source", "直した（配った後）"), "uid": uid,
                               "duty": o.get("duty")}}
                n += 1
                until = end if rd["scope"] == "once" else None
                led.append("add", rule=rd, op_id=uid, frm=start, until=until, meta={"said": u["text"], "at": at})
            self.store.save_ledger(led)                                  # 1 帳面
        acc_id = None
        if o.get("duty"):
            acc = Acceptance(id=f"a-{uid}", staff=o["duty"], date=f"{self.ym}-{o['day']:02d}", layer=o["layer"],
                             op_id=uid, tx_rev=led.rev, note=f"{o['requester']}さんの休みの穴埋め")
            acc_id = acc.id
            items = self.store.acceptances()
            if not any(a.id == acc.id for a in items):
                self.store.save_acceptances(items + [acc])               # 2 引き受けの記録
        if not any(h.get("op_id") == uid for h in m.history):
            m.history.append({"op_id": uid, "table": m.table, "built_rev": m.built_rev, "said": u["text"],
                              "rest_adjust": m.rest_adjust, "support": m.support, "acceptance_id": acc_id})
            m.table, m.built_rev = o["table"], led.rev
            m.rest_adjust, m.support = o.get("rest_adjust", m.rest_adjust), o.get("support", m.support)
            self.store.save_month(m)                                     # 3 月の表
        self.store.resolve_pending(self.year, self.month, uid)           # 4 未反映の発言

    def _fix_offer(self, u: dict, p, m, maybe: bool = False) -> Reply:
        """配った後のほかの直し（待機の案にならない頼み）。表は［これで直す］を押すまで変えない（画面3。Astra の指摘2）。"""
        led = Ledger.from_list(self.store.ledger().to_list())
        start, end = self._span()
        added = []
        for i, rd in enumerate(p.rules):
            rd = {**rd, "id": f"t-fix{i}", "scope": "once"}
            added.append(Rule.from_dict(led.append("add", rule=rd, frm=start, until=end)["rule"]))
        rules = [clip_to_month(r, start, end) for r in led.rules_at(between=self._span()).values()]
        fac, staff = self.store.facility(), self.store.staff_on(self.year, self.month)
        rr = resolve(fac, staff, rules, m, m.table, [r for r in rules if r.id.startswith("t-fix")])
        if not rr.ok:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("failed_solve", u["id"], rr.staged.message or "直せませんでした")
        if maybe and not rr.changes and not any(b["rule"].startswith("t-fix") for b in rr.staged.result.broken):
            return self._apply(u, p, "once")     # いまの表のままで守れている。選んでもらう物が無い
        if maybe and any(b["rule"].startswith("t-fix") for b in rr.staged.result.broken):
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], f"{p.summary}は、ほかのルールとぶつかって守れません。表は変えていません")
        offer = {"rules": p.rules, "scope": "once", "table": rr.table, "changes": rr.changes,
                 "summary": p.summary, "base_rev": self.store.ledger().rev, "base_table": json.dumps(m.table),
                 "source": "話した（できれば）" if maybe else "直した（配った後）"}
        self.store.update_pending(self.year, self.month, u["id"], status="offer", offer=offer)
        who = sorted({c["staff"] for c in rr.changes})
        if maybe:
            return Reply("fix_offer", u["id"], f"{p.summary}は守れます。代わりに{len(rr.changes)}マス動きます"
                         f"（{'・'.join(who)}さん）", changes=rr.changes, seconds=rr.seconds, summary=p.summary)
        return Reply("fix_offer", u["id"], f"{p.summary}。{len(rr.changes)}マス動きます（{'・'.join(who)}さん）",
                     changes=rr.changes, seconds=rr.seconds, summary=p.summary)

    def refuse(self, uid: str) -> Reply:
        """［断られた］。表も引き受けの記録も変えず、案Eの聞き返しへ。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None or u.get("status") != "offer":
            return Reply("error", uid, "断られた案が見つかりません")
        o = u["offer"]
        a = u.get("absence") or {"requester": o["requester"], "day": o["day"], "layer": o["layer"], "tried": []}
        a = {**a, "tried": a["tried"] + [o["duty"]]}
        self.store.update_pending(self.year, self.month, uid, status="wider", absence=a, offer=None)
        return Reply("ask", uid, ASK_WIDER, extra={"wider": True})

    def wider(self, uid: str) -> Reply:
        """［ほかの人に頼む案を見る］（案E。D4）。その日に休みの人へ、今月の引き受けが少ない順に1人ずつ頼む案を出す。
        動かすのは頼んだ人とその1人だけ。了承をもらって直せば、その人の引き受けとして記録する。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None or u.get("status") != "wider" or not u.get("absence"):
            return Reply("error", uid, "広げる頼みが見つかりません")
        a = u["absence"]
        m = self.store.month(self.year, self.month)
        fac, staff = self.store.facility(), self.store.staff_on(self.year, self.month)
        rules = month_rules(self.store, self.year, self.month, with_premise=False)
        skip = set(a["tried"]) | {a["requester"]}
        cands = sorted((s.name for s in staff if s.name not in skip and m.table[s.name][a["day"] - 1] == "公"),
                       key=lambda n: (self._accepted(n), n))
        for c in cands:
            r = fix_absence(fac, staff, rules, m, m.table, a["requester"], a["day"], [c])
            if r["status"] == "ok":
                return self._make_offer(u, m, a["requester"], a["day"], r, a["layer"], wider=True)
        self.store.resolve_pending(self.year, self.month, uid)
        return Reply("ask", uid, "その日に休みの人に頼んでも、埋められませんでした。表は変えていません")

    def cancel(self, uid: str) -> Reply:
        """［やめる］。頼みを取り下げる（表も記録も変えない）。"""
        self.store.resolve_pending(self.year, self.month, uid)
        return Reply("ask", uid, "やめました。表は変えていません")

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
            "shift_names": [[st.symbol, st.name] for st in fac.shift_types],
            "table": m.table,
            "cards": [{"id": r.id, "name": r.name, "flex": r.flex, "scope": r.scope, "who": r.who,
                       "when": r.when, "kind": r.kind, "params": r.params, "meta": r.meta}
                      for r in rules.values()],
            "pending": self.store.pending(self.year, self.month),
            "can_undo": bool(m.history),
            "distributed": m.distributed,
            "layers": {k: {"name": v["def"]["name"], "assign": v["assign"], "rule_id": v["rule_id"]}
                       for k, v in m.layers.items()},
            "accepted": accepted_counts(self.store.acceptances(), self.ym,
                                        self.base_date or self.now().date().isoformat()),
            "last_said": m.history[-1]["said"] if m.history else None,
            "symbols": [{"symbol": st.symbol, "name": st.name} for st in fac.shift_types],
            "absence_sim": self.store.absence_sim(self.year, self.month),
        }
