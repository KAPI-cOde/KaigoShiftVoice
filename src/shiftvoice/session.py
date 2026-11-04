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
import re
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
from .rules import same_slot
from .review import history as _history
from .standby import ASK_WIDER, NO_MOVE, assign, duty_days, fix_absence, oncall_layer, use_move
from .store import Store

JST = dt.timezone(dt.timedelta(hours=9))
ASK_PERIOD = "毎月ですか、今月だけですか"
READ_FAILED = "読み取りに失敗しました。表もルールも変えていません"
# 鍵が無くて Claude を呼べない時。送り直しても同じなので、何ができて何が要るかを言う（2026-11-05 まっさらな取得の確かめで、
# 鍵なしの一言が「読み取りに失敗しました」と［送り直す］になり、審査員が止まると分かった）
NO_KEY = ("Claude の鍵が入っていないので、一言では直せません（Claude へは送っていません。表もルールも変えていません）。"
          "鍵なしでも、表を見ることと、右上の［自分の施設で始める］で来月の表を作ることはできます。"
          "鍵の入れ方は README の「鍵の入れ方」にあります")
ANSWER_FIRST = "先に上の質問に答えてください（毎月／今月だけ）"
ANSWER_FIRST_REPLACE = "先に上の質問に答えてください（置き換える／別に足す）"
# 休みを移す日の候補のうち、画面へ送る物（表の中身・足し引き・応援は送らない）
MOVE_SHOWN = ("day", "kind", "line", "parts", "label", "warn", "changes", "message", "summary", "vs_first")

# 聞き返しへの答えそのもの。句読点と空白を除いた全文が「相づち＋答えの言葉1つ＋言い終わり」の時だけ答えにする。
# 中身の言葉が1字でも残る一言は答えにしない。前は文の中の「毎月」を拾い、「連勤は4日までにしよう。毎月」を
# 前の問いの答えに使って連勤のルールを黙って消し、答えでない一言は黙って捨てていた（2026-11-04 本物の Claude で再現）。
# 言い方を広く拾わない。迷う言い方は答えにせず、先に上の質問に答えてと言う（ボタンで答えられる）
_NOISE = re.compile(r"[\s、。，．,.!！?？…・「」]")
_FILLER = r"(?:はい|うん|ええ|えっと|えーと|えー|あ|じゃあ)?"
_PERIOD_ONLY = re.compile(_FILLER + r"(?:毎月|今月だけ|今月のみ|今回だけ|今回のみ|今月|今回)"
                          r"(?:で|です|でお願いします|でいい|でいいです|でいいよ|にして|ね|よ){0,2}")
_REPLACE_ONLY = {"replace": re.compile(_FILLER + r"置き換え(?:る|て|ます)(?:ください|ね|よ)?"),
                 "add": re.compile(_FILLER + r"別に足(?:す|して|します)(?:ください|ね|よ)?")}


COVER_ID = "a-cover-"   # 残業・早出などでの穴埋めの記録の ID の頭（待機の記録「a-発言ID」と見分ける）


def _days_ago(text: str) -> int:
    """日を言っていない穴埋めの報告が、今日から何日前のことか（「昨日」は1、「おととい」は2、ほかは今日）。"""
    if re.search(r"おととい|一昨日", text):
        return 2
    if re.search(r"昨日|昨夜|ゆうべ", text):
        return 1
    return 0


def period_answer(text: str) -> str | None:
    """「毎月ですか、今月だけですか」への答えそのものなら "rule" / "once"、そうでなければ None。"""
    return read_answer(text) if _PERIOD_ONLY.fullmatch(_NOISE.sub("", text)) else None


def replace_answer(text: str) -> str | None:
    """「置き換えますか、別に足しますか」への答えそのもの（ボタンの名前どおり）なら "replace" / "add"。"""
    t = _NOISE.sub("", text)
    return next((k for k, p in _REPLACE_ONLY.items() if p.fullmatch(t)), None)


@dataclass
class Reply:
    kind: str                 # applied / ask_period / ask_replace / ask / error / failed_solve
    uid: str | None = None
    message: str = ""
    changes: list[dict] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    summary: str = ""
    seconds: float = 0.0
    warning: str = ""        # 守れなかった新しいルール・段を下げた知らせ（赤で出す）
    extra: dict = field(default_factory=dict)  # 場面ごとの添え物（待機の人・引き受けの回数など）
    sent: str | None = None  # その一言で Claude へ送った文（名前を番号に伏せた後。呼ばなかった時は None）
    sent_known: bool = False  # 一言の返事で、sent を画面に出してよい時 True（False なら何も出さない）

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _shadowed(r: Rule, rules: dict) -> str | None:
    """同じ人・同じ所を決めるルールがもう1件あって、こちらが効いていない時、一覧に添える一文。
    千葉さんの月6回まで・月7回までのように2件あると、どちらが効くか分からないと読み手2人が迷った（2026-11-04）。
    運用の都合の回数どうしは、新しい方が言った側だけ前のに代わる（rules._replace_same）ので、新しい方が効く。
    それ以外は両方が守られるので、厳しい方が効く（敵対レビュー 2026-11-04。前は厳しい方が効くと書くだけで、
    言い直してゆるめた時に逆を言っていた）。"""
    from .review import _stricter
    from .rules import may_cover
    order = list(rules)

    def replaced(a: Rule, b: Rule) -> bool:   # b が a に代わる（_replace_same と同じ条件）
        ok = lambda x: x.kind == "count" and not x.when and x.flex == "operational"
        return ok(a) and ok(b) and order.index(b.id) > order.index(a.id)
    for o in rules.values():
        if o.id == r.id or o.scope != r.scope or (o.who or None) != (r.who or None) or not may_cover(r, o):
            continue
        if r.kind == "count" and r.flex == o.flex == "operational" and not r.when and not o.when:
            if replaced(r, o):
                return f"後から言った「{o.name}」に置き換わっていて、こちらは効いていません"
            continue
        if _stricter(o, r) is True:
            return f"「{o.name}」もあり、厳しい方が効きます"
    return None


def _next_id(led) -> int:
    """次のルールの番号。外したルールの番号も使い回さない（言い直しで外した r101 の後に、新しい頼みが同じ r101 に
    なっていた。2026-11-03 本物の Haiku で確かめた時に見つけた）。"""
    ids = {t["rule"]["id"] for t in led.txs if t["op"] == "add" and isinstance(t.get("rule"), dict)} | set(led.rules_at())
    return max([int(r[1:]) for r in ids if r[1:].isdigit()] + [100]) + 1


class Session:
    def __init__(self, store: Store, year: int, month: int, reader, *, now=None, who: str = "作成者",
                 builder=None, base_date: str | None = None):
        self.store, self.year, self.month, self.reader = store, year, month, reader
        self.builder = builder
        self.now = now or (lambda: dt.datetime.now(JST))
        self.who = who
        self.base_date = base_date  # 引き受けを数える基準日（デモ設定。None は今日）
        self._sent: dict[str, str | None] = {}  # 一言ごとに Claude へ送った文（伏せた後）

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
            if u["id"] in applied and u.get("status") == "offer" and (u.get("offer") or {}).get("cancel") is not None \
                    and not (u.get("offer") or {}).get("replace"):
                # 日付の無い取り消しの確かめは、帳面から外すだけで表は変えない。帳面に入った後で止まったら片付けるだけ
                # （表を持たない案を _commit_offer に回すと KeyError で月が開けなかった。敵対レビュー 2026-11-03）
                self.store.resolve_pending(self.year, self.month, u["id"])
            elif u["id"] in applied and u.get("status") == "offer":
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
        return self._stamp(self._read(u))

    def _stamp(self, r: Reply) -> Reply:
        """返事に、その一言で Claude へ送った文を添える（送っていなければ None）。"""
        if r.uid in self._sent:
            r.sent, r.sent_known = self._sent[r.uid], True
        return r

    def _recall(self, u: dict) -> None:
        if "sent_text" in u:
            self._sent[u["id"]] = u["sent_text"]

    def retry(self, uid: str) -> Reply:
        """［送り直す］。同じ発言 ID で読み直す。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None:
            return Reply("error", uid, "送り直す発言が見つかりません")
        self._recall(u)
        if u.get("items") is not None:
            return self._stamp(self._safe_propose(u))
        return self._stamp(self._read(u))

    def _read(self, u: dict) -> Reply:
        fac, staff = self.store.facility(), self.store.staff()
        masker = Masker(staff, fac)
        masked, unknown = masker.mask(u["text"])
        if unknown:
            # 名簿にない名前を含む一言は送らない。近い名前は手元の名簿から出し、作成者が選ぶまで表を動かさない（13章。T2-02）
            wrong = unknown[0]
            cands = masker.suggest(wrong)
            cands = cands if len(cands) == 1 else []   # 1人に絞れる時だけ候補にする（森田→石田・岡田 のような当て推量を出さない）
            self._sent[u["id"]] = None
            self.store.update_pending(self.year, self.month, u["id"], status="ask_name", unknown=unknown,
                                      sent_text=None)
            if wrong in masker.leftover:   # 呼び方・名前の頭が伏せきれずに残った。名前を選ぶと、その名前に置き換えて読む
                cands = [masker.leftover[wrong]]
                msg = f"「{wrong}」は{cands[0]}さんのことですか（表もルールも変えていません）"
            else:
                msg = (f"{wrong}さんは名簿にいません。{'・'.join(c + 'さん' for c in cands)}ですか" if cands else
                       f"{wrong}さんは名簿にいません。名簿から選んでください")
            return Reply("ask", u["id"], msg, extra={"ask_name": {"wrong": wrong, "candidates": cands,
                                                                  "roster": [x.name for x in staff]}})
        sent = masked if getattr(self.reader, "calls_claude", True) else None
        self._sent[u["id"]] = sent
        self.store.update_pending(self.year, self.month, u["id"], sent_text=sent)
        try:
            items = masker.unmask(self.reader.read(masked))
            if self.builder and any(it.get("type") == "custom" for it in items):
                # 型に収まらない一言（表に無い当番など）は組み立て役へ（8章）
                b = masker.unmask(self.builder.build(masked))
                items = ([{"type": "layer_def", "def": b["layer"]}] if "layer" in b
                         else [{"type": "unclear", "question": b.get("question")}])
        except ReadError as e:
            self.store.update_pending(self.year, self.month, u["id"], status="failed", error=str(e))
            if getattr(self.reader, "calls_claude", True) is False:   # 鍵なし（NoKeyReader）
                return Reply("error", u["id"], NO_KEY, extra={"no_key": True})
            return Reply("error", u["id"], READ_FAILED)
        # 一度読んだら残し、AI を呼び直さない（3章 drafts）
        self.store.update_pending(self.year, self.month, u["id"], items=items, status="read")
        u = {**u, "items": items}
        return self._safe_propose(u)

    def _safe_propose(self, u: dict) -> Reply:
        """読み取りの後で想定外の例外が出ても、画面に「通信が切れた」ように見せない。読み取りの失敗と同じ返事にする
        （取り込んで作った施設で、有給の記号が無くて落ちた。敵対レビュー 2026-11-03）。"""
        try:
            return self._propose(u)
        except Exception as e:  # noqa: BLE001 想定外の例外だけ。聞き返し（Clarify）は _propose の中で返事になる
            self.store.update_pending(self.year, self.month, u["id"], status="failed",
                                      error=f"{type(e).__name__}: {e}")
            return Reply("error", u["id"], READ_FAILED)

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
        return self._stamp(self._read({**u, "text": text}))

    def _leave_caps(self) -> dict[str, int]:
        """人ごとの有給の上限（職員と対象月を持つ回数の決まり。13章）。"""
        fac = self.store.facility()
        lv = next((st.symbol for st in fac.shift_types if st.kind == "leave"), None)
        caps: dict[str, int] = {}
        if lv is None:   # 有給の記号が無い施設（取り込んで作った施設など）
            return caps
        for r in self.store.ledger().rules_at(between=self._span()).values():
            if (r.kind == "count" and r.params.get("shifts") == [lv] and "max" in r.params
                    and r.who and len(r.who) == 1 and r.flex == "operational"):
                caps[r.who[0]] = r.params["max"]
        return caps

    def _propose(self, u: dict) -> Reply:
        fac, staff = self.store.facility(), self.store.staff()
        m = self.store.month(self.year, self.month)
        try:
            p = to_proposal(u["items"], u["text"], fac, staff, m, leave_caps=self._leave_caps(),
                            rules=list(self.store.ledger().rules_at(between=self._span()).values()))
        except Clarify as e:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], str(e))
        if p.kind == "cancel":
            return self._cancel(u, p)
        if p.kind == "cover":
            return self._cover_offer(u, p)
        if p.kind == "cells" and p.replace_from and not m.distributed:
            r = self._replace_offer(u, p, m)
            if r is not None:
                return r
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
        """「毎月ですか、今月だけですか」への返事（話し言葉でも選択肢でも）。
        「置き換えますか、別に足しますか」への返事（"replace" / "add"）もここで受ける。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None:
            return Reply("error", uid, "聞き返した発言が見つかりません")
        self._recall(u)
        if u.get("status") == "ask_replace":
            dup = text if text in ("replace", "add") else replace_answer(text)
            if dup is None:
                return self._stamp(self._answer_first(u))
            self.store.update_pending(self.year, self.month, uid, dup=dup)
            return self._stamp(self._safe_propose({**u, "dup": dup}))
        scope = text if text in ("rule", "once") else period_answer(text)
        if scope is None:
            return self._stamp(self._answer_first(u))
        self.store.update_pending(self.year, self.month, uid, scope=scope)
        return self._stamp(self._safe_propose({**u, "scope": scope}))

    def _answer_first(self, u: dict) -> Reply:
        """聞き返しの間に、答えでない一言が来た。表もルールも未反映の発言も変えず、Claude にも送らず、
        先に上の質問に答えてと言う。問いは残すので、画面はボタンを出したまま、打った文を入力欄に残す（印 answer_first）。"""
        if u.get("status") == "ask_replace":
            rules = self.store.ledger().rules_at()
            names = "」「".join(rules[i].name for i in u.get("dup_old") or [] if i in rules)
            q = (f"前からのルール「{names}」があります。置き換えますか、別に足しますか" if names
                 else "置き換えますか、別に足しますか")
            return Reply("ask_replace", u["id"], ANSWER_FIRST_REPLACE, extra={"answer_first": True, "question": q})
        return Reply("ask_period", u["id"], ANSWER_FIRST, extra={"answer_first": True})

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
        # 毎月のルールで、同じ所を決める前からのルールがあれば、置き換えるか別に足すかを聞く（ルールを積もらせない）
        olds = [r for r in cur if scope == "rule" and any(same_slot(rd, r) for rd in p.rules)]
        if olds and u.get("dup") is None and not any(t.get("op_id") == u["id"] for t in led.txs):
            self.store.update_pending(self.year, self.month, u["id"], status="ask_replace", scope=scope,
                                      dup_old=[r.id for r in olds])
            names = "」「".join(r.name for r in olds)
            return Reply("ask_replace", u["id"], f"前からのルール「{names}」があります。置き換えますか、別に足しますか",
                         summary=p.summary, extra={"old": [r.name for r in olds]})
        if not any(t.get("op_id") == u["id"] for t in led.txs):
            n = _next_id(led)
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
            if u.get("dup") == "replace":
                for oid in u.get("dup_old", []):
                    if oid in led.rules_at():
                        led.append("remove", rule_id=oid, op_id=u["id"], meta={"kind": "replaced", "at": at})
        changes: list[dict] = []
        secs = 0.0
        warning = ""
        if added and all(r.kind == "layer" for r in added):
            fac = self.store.facility()
            pins = {int(k): v for k, v in ((self.store.premise(self.year, self.month) or {})
                                            .get("standby_pins", {})).items()}
            for r in added:
                d = r.params["def"]
                if d["id"] in m.layers and m.layers[d["id"]]["def"].get("name") != d["name"]:   # 別の当番と ID が重なる時
                    d = r.params["def"] = {**d, "id": f'{d["id"]}-{len(m.layers) + 1}'}
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
        summary = p.summary + ("（前のルールと置き換えました）" if u.get("dup") == "replace" and u.get("dup_old") else "")
        return Reply("applied", u["id"], summary, changes=changes, rule_ids=[r.id for r in added],
                     summary=summary, seconds=secs, warning=warning)

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
        if any(rq["days"] is None for rq in p.removes):
            return self._cancel_offer(u, p, rules, led)
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

    def _cancel_offer(self, u: dict, p: Proposal, rules: dict, led) -> Reply:
        """日付の無い取り消し（「さっきの〇〇さんの休み、やっぱりなし」）。その人がこの月に話した、休みなどの
        マスの頼み（帳面のマスのルール）が1つに決まる時だけ、表を変える前に取り消すかを確かめる。
        0件・2件以上・取り込んだ希望しか無い時は、今どおり日付を聞く（迷ったら聞き返す）。"""
        rq = p.removes[0]
        w, sym = (rq["who"] or [None])[0], rq["shift"]
        hits = [r for r in rules.values() if r.kind == "cell" and (w is None or r.who == [w]) and r.when
                and str(r.meta.get("source", "")).startswith("話した")
                and (sym is None or sym in r.params.get("in", []) + r.params.get("not_in", []))]
        fac = self.store.facility()
        what = "" if not sym else "休み" if fac.shift(sym).kind == "rest" else fac.shift(sym).name
        if w is None:
            # 人も日も無い取り消し（「さっきのやっぱりなし」）。この月に話したマスの頼みが1件で、1人の頼みの時だけ
            if not hits:
                self.store.resolve_pending(self.year, self.month, u["id"])
                return Reply("ask", u["id"], "取り消す頼みが見つかりませんでした。誰の、何日の頼みか言ってください。"
                             "表もルールも変えていません")
            if len(hits) > 1 or len(hits[0].who or []) != 1:
                self.store.resolve_pending(self.year, self.month, u["id"])
                return Reply("ask", u["id"], f"今月話した頼みが{len(hits)}件あります（"
                             + "・".join(h.name for h in hits) + "）。誰の、何日の分を取り消すか言ってください。"
                             "表もルールも変えていません")
            w = hits[0].who[0]
        if not hits:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], f"{w}さんの、何日の{what or '頼み'}を取り消しますか。表もルールも変えていません")
        if len(hits) > 1:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], f"{w}さんの{what or ''}の頼みが{len(hits)}件あります（"
                         + "・".join(h.name for h in hits) + "）。何日の分を取り消すか言ってください")
        r = hits[0]
        days = list(r.when)
        if not what:   # 勤務を言っていない取り消しは、外す頼みから言い方を決める（「8日の内田さん 8日 休み」と出ていた）
            rs = (r.params.get("in") or [""])[0]
            what = (("休み" if fac.shift(rs).kind == "rest" else fac.shift(rs).name)
                    if rs in fac.symbols else f"頼み（{r.name}）")
        label = f"{w}さんの{'・'.join(map(str, days))}日の{what}"
        msg = f"{label}を取り消しますか。表のマスはそのままです"
        offer = {"cancel": [{"who": [w], "days": days, "shift": sym}], "rules": [], "changes": [],
                 "summary": f"{label}を取り消す。表のマスはそのまま", "done": f"{label}の指定を外しました",
                 "message": msg, "base_rev": led.rev}   # 開き直した時も同じ文で出す
        self.store.update_pending(self.year, self.month, u["id"], status="offer", offer=offer)
        return Reply("fix_offer", u["id"], msg, summary=offer["summary"])

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
        # 配った時の表を残す。主任が「配った表と違う表を印刷してしまう」のを恐れるので、配った表から何マス直したかを
        # 画面と印刷の紙に出す（2026-11-04 受け止めの主任役が2回挙げた）
        m.dist_table = {n: list(r) for n, r in (m.table or {}).items()}
        m.dist_at = self.now().isoformat(timespec="minutes")
        self.store.save_month(m)

    def _today(self) -> str:
        """引き受けを数える基準日（デモ設定。無ければ今日の日付）。"""
        return self.base_date or self.now().date().isoformat()

    def _accepted(self, name: str) -> int:
        return accepted_counts(self.store.acceptances(), self.ym, self._today(), include_returned=True).get(name, 0)

    # 残業・早出などでの穴埋めを記録する（表は変えない）
    def _cover_dup(self, staff: str, date: str, uid: str, items=None) -> bool:
        """同じ人・同じ日の引き受けの記録（待機・配った後の直し・残業のどれでも）がもうあるか。この発言の記録は数えない。"""
        items = self.store.acceptances() if items is None else items
        return any(a.staff == staff and a.date == date and a.id != f"{COVER_ID}{uid}" for a in items)

    def _cover_offer(self, u: dict, p: Proposal) -> Reply:
        """すでに起きた穴埋め（「田村さんが残業して、遅番の穴を埋めてくれた」）。表のマスは変えず、確かめのカードを出す。
        ［記録する］で引き受けの記録に足し、翌月の返す計算でも1回として数える（同僚の介護員の答え 2026-11-04）。
        日を言っていなければ今日（基準日）、「昨日」なら前の日。開いている月の外の日と、まだ来ていない日は記録せずに聞く。
        同じ人・同じ日の記録がもうあれば、足さずにそう言う。"""
        c, uid = p.covers[0], u["id"]
        today = dt.date.fromisoformat(self._today())
        d = (today - dt.timedelta(days=_days_ago(u["text"])) if c["day"] is None
             else dt.date(self.year, self.month, c["day"]))
        md = f"{d.month}月{d.day}日"
        msg = None
        if (d.year, d.month) != (self.year, self.month):
            msg = (f"{md}は{self.month}月の表の外なので、記録できません。{self.month}月の日なら「14日は」のように"
                   "日を付けて言ってください。表も記録も変えていません")
        elif d > today:
            msg = f"{md}はまだ先です。何日のことか教えてください。表も記録も変えていません"
        elif self._cover_dup(c["staff"], d.isoformat(), uid):
            msg = f"{c['staff']}さんの{md}の引き受けは、もう記録があります。記録は増やしていません"
        if msg:
            self.store.resolve_pending(self.year, self.month, uid)
            return Reply("ask", uid, msg)
        label = f"{c['staff']}さん {md} {c['how']}で穴埋め"
        offer = {"cover": {"staff": c["staff"], "date": d.isoformat(), "how": c["how"]}, "summary": label,
                 "message": f"{label} → 引き受けとして記録します"}   # 開き直した時も同じ文で出す
        self.store.update_pending(self.year, self.month, uid, status="offer", offer=offer)
        return Reply("cover_offer", uid, offer["message"], summary=label, extra={"cover": offer["cover"]})

    def _commit_cover(self, u: dict) -> Reply:
        """［記録する］。引き受けの記録に1件足す。表と帳面は変えない。案を出した後に同じ人・同じ日の記録が入っていたら足さない。
        保存の順は 記録 → 月の戻す履歴（［ひとつ戻す］で記録を消すため）→ 未反映の発言。何度呼んでも1件。"""
        uid, c = u["id"], u["offer"]["cover"]
        md = f"{int(c['date'][5:7])}月{int(c['date'][8:10])}日"
        items = self.store.acceptances()
        if self._cover_dup(c["staff"], c["date"], uid, items):
            self.store.resolve_pending(self.year, self.month, uid)
            return Reply("ask", uid, f"{c['staff']}さんの{md}の引き受けは、もう記録があります。記録は増やしていません")
        aid = f"{COVER_ID}{uid}"
        if not any(a.id == aid for a in items):
            self.store.save_acceptances(items + [Acceptance(
                id=aid, staff=c["staff"], date=c["date"], layer=c["how"], op_id=uid,
                tx_rev=self.store.ledger().rev, note=u["text"])])                # 1 引き受けの記録
        m = self.store.month(self.year, self.month)
        if not any(h.get("op_id") == uid for h in m.history):
            m.history.append({"op_id": uid, "table": m.table, "built_rev": m.built_rev, "said": u["text"],
                              "acceptance_id": aid})
            self.store.save_month(m)                                         # 2 戻す履歴（表は変えない）
        self.store.resolve_pending(self.year, self.month, uid)               # 3 未反映の発言
        msg = f"{c['staff']}さんの引き受けとして記録しました（{md} {c['how']}で穴埋め）。表のマスはそのままです"
        return Reply("applied", uid, msg, summary=msg)

    def _accepted_items(self, m) -> list[dict]:
        """画面の引き受けの記録の行。この月の、今日（基準日）までの記録。待機は当番の名前、残業などは埋め方で言う。"""
        today = self._today()
        names = {k: v["def"]["name"] for k, v in m.layers.items() if isinstance(v, dict) and "def" in v}
        out = []
        for a in sorted(self.store.acceptances(), key=lambda a: (a.date, a.staff)):
            if a.date[:7] != self.ym or a.date > today:
                continue
            cover = a.id.startswith(COVER_ID)
            label = (f"{a.layer}で穴埋め" if cover else "配った後の直しで勤務" if a.layer == "fix"
                     else f"{names.get(a.layer, '待機当番')}で穴埋め")
            if a.moved_to:   # 公休を移した日（案の返事の行「11/1 引き受け（休みは10日へ）」とそろえる）
                label += f"（休みは{int(a.moved_to[8:10])}日へ）"
            out.append({"staff": a.staff, "date": a.date, "label": label, "cover": cover})
        return out

    def _standby_offer(self, u: dict, p, m) -> Reply | None:
        offs = [r for r in p.rules if r["kind"] == "cell" and r["params"] == {"in": ["公"]}]
        if len(offs) != 1 or len(p.rules) != 1 or len(offs[0]["who"] or []) != 1 or len(offs[0]["when"]) != 1:
            return None
        name, day = offs[0]["who"][0], offs[0]["when"][0]
        layer = oncall_layer(m, day)   # 勤務の中で受け持つ係（入浴係など）は休みの電話に使わない
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
        """頼む人が決まった案（待機の人、または案Eで広げた休みの人）。［了承をもらって直す］まで表は変えない。
        頼まれた人の公休は減らさず、別の日へ移す（公休移動。2026-11-04）。移す日の候補（3つまで）を案に持ち、
        1つ目（おすすめ）で出す。［休みを移す日を変える］で選んだ候補は、［了承をもらって直す］の時に受け取る。
        今月の中に移せる日が無い時だけ、今までの形（公休が1日減る）にして、そう言う。"""
        helper = r["duty"]
        who = f"待機ではない、その日休みの{helper}さん" if wider else f"その日の待機の{helper}さん"

        def message(changes) -> str:
            return f"{who}に頼む案です（表で変わるのは{len(changes)}マス）"

        def summary(rest, moved_to) -> str:
            if moved_to:
                return f"{helper}さん 公休{rest[helper][0]}日のまま（休みは{moved_to}日へ移す）"
            return (f"公休　{name}さん {rest[name][0]}日 → {rest[name][1]}日　"
                    f"{helper}さん {rest[helper][0]}日 → {rest[helper][1]}日")

        moves = [{"day": mv["day"], "kind": mv["kind"], "makeup": mv["makeup"], "line": mv["line"],
                  "parts": mv["parts"], "label": mv["label"], "vs_first": mv.get("vs_first", ""), "warn": mv["warn"], "table": mv["table"],
                  "changes": mv["changes"], "rest": mv["rest"], "rest_adjust": mv["rest_adjust"],
                  "support": mv["support"], "message": message(mv["changes"]), "summary": summary(mv["rest"], mv["day"])}
                 for mv in r.get("moves") or []]
        no_move = None if moves else NO_MOVE.format(n=r["rest"][helper][1])
        offer = {"requester": name, "day": day, "duty": helper, "table": r["table"], "changes": r["changes"],
                 "rest": r["rest"], "rest_adjust": r["rest_adjust"], "support": r["support"],
                 "moved_to": r.get("moved_to"), "moves": moves, "pick": 0, "no_move": no_move,
                 "message": message(r["changes"]), "summary": summary(r["rest"], r.get("moved_to")),
                 "layer": layer_id, "wider": wider, "base_rev": self.store.ledger().rev,
                 "base_table": json.dumps(m.table),
                 "rules": [{"name": f"{name}さん {day}日 休み", "kind": "cell", "flex": "wish",
                            "params": {"in": ["公"]}, "who": [name], "when": [day]}], "scope": "once"}
        self.store.update_pending(self.year, self.month, u["id"], status="offer", offer=offer)
        return self._offer_reply(u["id"], offer, seconds=r["seconds"])

    def _offer_reply(self, uid: str, o: dict, seconds: float = 0.0) -> Reply:
        """待機の人に頼む案の返事。案に書いた中身から作る（休みを移す日を選び直した時も同じ形で出す）。
        extra の moves は画面に出す物だけ（表の中身は送らない）。"""
        n = self._accepted(o["duty"])
        return Reply("standby_offer", uid, o["message"], changes=o["changes"], seconds=seconds, summary=o["summary"],
                     extra={"duty": o["duty"], "requester": o["requester"], "day": o["day"], "accepted": [n, n + 1],
                            "moves": [{k: mv.get(k) for k in MOVE_SHOWN} for mv in o.get("moves") or []],
                            "pick": o.get("pick", 0), "no_move": o.get("no_move")})

    def _pick(self, o: dict, pick) -> dict | None:
        """休みを移す日の候補を選んだ案。番号が候補に無ければ None（表は変えない）。"""
        try:
            i = int(pick)
        except (TypeError, ValueError):
            return None
        moves = o.get("moves") or []
        if not 0 <= i < len(moves):
            return None
        if i == o.get("pick", 0):
            return o
        mv = moves[i]
        return {**o, **use_move(mv), "pick": i, "message": mv["message"], "summary": mv["summary"]}

    def move(self, uid: str, pick) -> Reply:
        """［休みを移す日を変える］で候補を選んだ。表はまだ変えず、案をその候補にして出し直す。
        ［了承をもらって直す］を押すと、この候補で直る（案に書いておくので、開き直しても選んだ候補のまま）。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None or u.get("status") != "offer" or not (u.get("offer") or {}).get("duty"):
            return Reply("error", uid, "休みを移す日を選ぶ案が見つかりません。表は変えていません")
        o = self._pick(u["offer"], pick)
        if o is None:
            return Reply("error", uid, "選んだ休みの日が見つかりません。表は変えていません")
        if o is not u["offer"]:
            self.store.update_pending(self.year, self.month, uid, offer=o)
        return self._offer_reply(uid, o)

    def _records(self, name: str) -> list[dict]:
        """その人の、この月の引き受けの記録（発生日の順）。数えるのは画面の「〇月の引き受け」と同じ物（基準日まで）。
        画面の1行は「11/1 引き受け（休みは10日へ）」。休みを移した日が無い記録は「11/8 引き受け」。"""
        base = self.base_date or self.now().date().isoformat()
        out = []
        for a in sorted((a for a in self.store.acceptances()
                         if a.staff == name and a.date[:7] == self.ym and a.date <= base), key=lambda a: a.date):
            line = f"{int(a.date[5:7])}/{int(a.date[8:10])} 引き受け"
            if a.moved_to:
                line += f"（休みは{int(a.moved_to[8:10])}日へ）"
            out.append({"date": a.date, "moved_to": a.moved_to, "line": line})
        return out

    def accept(self, uid: str, pick: int | None = None) -> Reply:
        """［了承をもらって直す］（待機の人に頼む案）と［これで直す］（配った後のほかの直し）。
        待機の案の時だけ引き受けの記録が増える（17章 A23 の13。残業などの穴埋めの［記録する］も、ここから
        _commit_cover へ回る）。案を作った後に表か帳面が変わっていたら、
        古い案で上書きせずに作り直す（Astra 2026-11-02 の指摘1）。
        pick は［休みを移す日を変える］で選んだ候補の番号（0 がおすすめ。待機の案だけ。省くと案に書いた候補）。"""
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        if u is None or u.get("status") != "offer":
            return Reply("error", uid, "了承をもらう案が見つかりません")
        o = u["offer"]
        if o.get("cover") is not None:   # 穴埋めの記録。表を持たない案なので、下の作り直しの確かめには通さない
            return self._commit_cover(u)
        m = self.store.month(self.year, self.month)
        led = self.store.ledger()
        if o.get("cancel") is not None and not o.get("replace"):
            # 日付の無い取り消しを確かめた。案を作った後に帳面が変わっていたら作り直す（古い案で外さない）
            if led.rev != o.get("base_rev") and not any(t.get("op_id") == uid for t in led.txs):
                self.store.update_pending(self.year, self.month, uid, status="read", offer=None)
                r = self._propose({**u, "status": "read"})
                r.message = "表が変わったので、案を作り直しました。" + r.message
                return r
            return self._cancel(u, Proposal(kind="cancel", removes=o["cancel"], summary=o["done"]))
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
        if pick is not None and o.get("duty") and o.get("moves"):
            # ［休みを移す日を変える］で選んだ候補にしてから当てる（案に書いてから当てるので、途中で止まっても同じ候補で立て直す）
            o2 = self._pick(o, pick)
            if o2 is None:
                return Reply("error", uid, "選んだ休みの日が見つかりません。表は変えていません")
            if o2 is not o:
                o = o2
                self.store.update_pending(self.year, self.month, uid, offer=o)
                u = {**u, "offer": o}
        before_n = self._accepted(o["duty"]) if o.get("duty") else 0
        self._commit_offer(u, led, m)
        if not o.get("duty"):
            return Reply("applied", uid, "直しました", changes=o["changes"], summary=o.get("summary", ""))
        after_n = self._accepted(o["duty"])
        moved = f"（休みは{o['moved_to']}日へ）" if o.get("moved_to") else ""
        return Reply("applied", uid, f"{o['duty']}さんの了承をもらって直しました{moved}", changes=o["changes"],
                     summary=f"{self.month}月の引き受け {before_n}回 → {after_n}回",
                     extra={"duty": o["duty"], "accepted": [before_n, after_n], "moved_to": o.get("moved_to"),
                            "note": "引き受け＝休みの日に頼まれて入った回数。翌月の表で、有給と短い連勤で報います",
                            "records": [x["line"] for x in self._records(o["duty"])]})

    def _commit_offer(self, u: dict, led, m) -> None:
        """案を当てる。何度呼んでも同じ結果（保存の途中で止まった後の立て直しにも使う）。
        保存の順は 帳面 → 引き受けの記録 → 月の表 → 未反映の発言（記録が表より先。Astra の指摘3）。"""
        uid, o = u["id"], u["offer"]
        at = self.now().isoformat(timespec="seconds")
        start, end = self._span()
        if not any(t.get("op_id") == uid for t in led.txs):
            for d in o.get("replace") or []:   # 言い直しで外す前の頼み（同じ op_id なので、ひとつ戻すと一緒に戻る）
                meta = {"said": u["text"], "who": self.who, "at": at, "kind": "cancel"}
                if d.get("rest"):
                    led.append("change", rule_id=d["rule_id"], rule={"when": d["rest"], "name": d["name"]},
                               op_id=uid, meta=meta)
                else:
                    led.append("remove", rule_id=d["rule_id"], op_id=uid, meta=meta)
            n = _next_id(led)
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
                             op_id=uid, tx_rev=led.rev, note=f"{o['requester']}さんの休みの穴埋め",
                             moved_to=f"{self.ym}-{o['moved_to']:02d}" if o.get("moved_to") else None)
            acc_id = acc.id
            items = self.store.acceptances()
            if not any(a.id == acc.id for a in items):
                self.store.save_acceptances(items + [acc])               # 2 引き受けの記録
        elif m.distributed and o.get("base_table"):
            # 配った後の直しで、休みから勤務に変わった人も記録する（締め切りの後の負担は次の月へ持ち越す。2026-11-03）
            fac = self.store.facility()
            rest = {st.symbol for st in fac.shift_types if st.kind in ("rest", "leave")}
            before = json.loads(o["base_table"])
            said_who = {w for rd in o["rules"] for w in (rd.get("who") or [])}
            items = self.store.acceptances()
            new = []
            for c in o["changes"]:
                n, d = c["staff"], c["day"]
                if n in said_who or before[n][d - 1] not in rest or o["table"][n][d - 1] in rest:
                    continue
                aid = f"a-{uid}-{n}-{d}"
                if not any(a.id == aid for a in items):
                    new.append(Acceptance(id=aid, staff=n, date=f"{self.ym}-{d:02d}", layer="fix", op_id=uid,
                                          tx_rev=led.rev, note="配った後の直しで勤務が入った"))
            if new:
                self.store.save_acceptances(items + new)                 # 2 引き受けの記録
        if not any(h.get("op_id") == uid for h in m.history):
            m.history.append({"op_id": uid, "table": m.table, "built_rev": m.built_rev, "said": u["text"],
                              "rest_adjust": m.rest_adjust, "support": m.support, "acceptance_id": acc_id})
            m.table, m.built_rev = o["table"], led.rev
            m.rest_adjust, m.support = o.get("rest_adjust", m.rest_adjust), o.get("support", m.support)
            self.store.save_month(m)                                     # 3 月の表
        self.store.resolve_pending(self.year, self.month, uid)           # 4 未反映の発言

    def _replace_offer(self, u: dict, p, m) -> Reply | None:
        """言い直し（「赤坂さんじゃなくて井上さん、10日が休みで」）。言い直された人の、同じ日に話したマスの頼みが
        1件に決まる時だけ、それを外して新しい頼みを入れる案を、表を変える前に確かめる。
        無ければ None（今どおり入れる）。2件以上なら聞き返す（どれを外すか決めない）。"""
        import re
        rd = p.rules[0]
        days, w = set(rd["when"]), p.replace_from[0]
        led0 = self.store.ledger()
        rules = led0.rules_at(between=self._span())
        hits = [r for r in rules.values() if r.kind == "cell" and r.who and w in r.who and r.when
                and days & set(r.when) and str(r.meta.get("source", "")).startswith("話した")]
        if not hits:
            return None
        if len(hits) > 1 or hits[0].who != [w]:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("ask", u["id"], f"{w}さんの{'・'.join(map(str, sorted(days)))}日の頼みが{len(hits)}件あります（"
                         + "・".join(h.name for h in hits) + "）。どれを外すか言ってください。表もルールも変えていません")
        old = hits[0]
        gone = sorted(days & set(old.when))
        rest = [d for d in old.when if d not in days]
        name = re.sub(r"[\d・]+日", "・".join(map(str, rest)) + "日", old.name, count=1) if rest else old.name
        drop = [{"rule_id": old.id, "rest": rest, "name": name}]
        led = Ledger.from_list(led0.to_list())
        start, end = self._span()
        if rest:
            led.append("change", rule_id=old.id, rule={"when": rest, "name": name})
        else:
            led.append("remove", rule_id=old.id)
        added = [Rule.from_dict(led.append("add", rule={**rd, "id": "t-fix0", "scope": "once"},
                                           frm=start, until=end)["rule"])]
        rules2 = [clip_to_month(r, start, end) for r in led.rules_at(between=self._span()).values()]
        fac, staff = self.store.facility(), self.store.staff_on(self.year, self.month)
        rr = resolve(fac, staff, rules2, m, m.table, [r for r in rules2 if r.id == "t-fix0"])
        if not rr.ok:
            self.store.resolve_pending(self.year, self.month, u["id"])
            return Reply("failed_solve", u["id"], rr.staged.message or "直せませんでした")
        sym = (old.params.get("in") or old.params.get("not_in") or [""])[0]
        what = (("休み" if fac.shift(sym).kind == "rest" else fac.shift(sym).name)
                if sym and "in" in old.params else f"頼み（{old.name}）")
        new_sym = rd["params"].get("in", [""])[0] if "in" in rd["params"] else ""
        new_what = ("休み" if new_sym and fac.shift(new_sym).kind == "rest" else
                    fac.shift(new_sym).name if new_sym else rd["name"])
        nw = rd["who"][0]
        dd = "・".join(map(str, sorted(days)))
        label = f"{w}さんの{'・'.join(map(str, gone))}日の{what}を外して、{nw}さんの{dd}日を{new_what}にします"
        who = sorted({c["staff"] for c in rr.changes})
        msg = f"{label}。{len(rr.changes)}マス動きます" + (f"（{'・'.join(who)}さん）" if who else "")
        offer = {"rules": p.rules, "scope": "once", "table": rr.table, "changes": rr.changes, "replace": drop,
                 "summary": label, "message": msg, "base_rev": led0.rev, "base_table": json.dumps(m.table),
                 "source": "話した"}
        self.store.update_pending(self.year, self.month, u["id"], status="offer", offer=offer)
        return Reply("fix_offer", u["id"], msg, changes=rr.changes, seconds=rr.seconds, summary=label)

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
        u = next((x for x in self.store.pending(self.year, self.month) if x["id"] == uid), None)
        cover = bool(u and (u.get("offer") or {}).get("cover"))
        self.store.resolve_pending(self.year, self.month, uid)
        return Reply("ask", uid, "やめました。記録は増やしていません" if cover else "やめました。表は変えていません")

    # 画面へ渡す
    def restore(self, rule_id: str) -> Reply:
        """片付けたルールを一覧から戻す。今の月の表は変えず、次に表を作る時から効く。"""
        from .ledger import LedgerError
        from .review import restore
        try:
            name = restore(self.store, rule_id, self.now().isoformat(timespec="seconds"))
        except LedgerError as e:
            return Reply("ask", None, str(e))
        # 期限が過ぎたルール（期限が来て片付けた物）は、戻しても効かない。そう言って、続けるなら期間を言ってもらう
        # （前は「次から効きます」とだけ出して、効かなかった。敵対レビュー 2026-11-04）
        r = self.store.ledger().rules_at().get(rule_id)
        until = (r.meta.get("until") if r else None)
        from .nextmonth import next_ym
        ny, nm = next_ym(self.year, self.month)
        if until and until < f"{ny:04d}-{nm:02d}-01":   # 次に作る表（翌月）に1日も効かない
            msg = (f"「{name}」を戻しました。ただし期限（{int(until[5:7])}月{int(until[8:10])}日まで）が過ぎているので、"
                   "このままでは効きません。続けるなら、いつまでかを一言で言ってください")
        else:
            msg = f"「{name}」を戻しました。次に表を作る時から効きます"
        return Reply("applied", None, msg, summary=msg)

    def _rule_stats(self, rules: dict) -> dict:
        """一覧の上に出す、ルールの量と増え方と、ルールの整理（cleanup.py）の候補の数。"""
        from .cleanup import view
        mine = [r for r in rules.values() if r.scope == "rule"]
        start = self._span()[0]
        return {"total": len(mine), "added": sum(1 for r in mine if r.meta.get("said") and r.meta.get("from") == start),
                "candidates": view(self.store, self.year, self.month)["count"]}

    def cleanup(self) -> dict:
        from .cleanup import view
        return view(self.store, self.year, self.month)

    def cleanup_apply(self, archive: list[str], keep: list[str]) -> dict:
        from .cleanup import apply
        from .ledger import LedgerError
        try:
            return apply(self.store, self.year, self.month, list(archive), list(keep), self.now())
        except LedgerError as e:
            return {"error": str(e)}

    def why(self, name: str, day: int) -> dict:
        """マスを押した時の「なぜこのマスか」（why.py）。表の操作はしない。"""
        from .why import why_cell
        m = self.store.month(self.year, self.month)
        if not m.table or name not in m.table or not 1 <= day <= m.days:
            return {"error": "そのマスはありません"}
        rules = month_rules(self.store, self.year, self.month, with_premise=False)
        return why_cell(rules, self.store.facility(), self.store.staff_on(self.year, self.month), m, m.table, name, day)

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
                       "when": r.when, "kind": r.kind, "params": r.params, "meta": r.meta,
                       "shadowed_by": _shadowed(r, rules)}
                      for r in rules.values()],
            "pending": self.store.pending(self.year, self.month),
            "archived": [h for h in _history(self.store.ledger())][-30:],
            "rule_stats": self._rule_stats(rules),
            "can_undo": bool(m.history),
            "distributed": m.distributed,
            "layers": {k: {"name": v["def"]["name"], "assign": v["assign"], "rule_id": v["rule_id"]}
                       for k, v in m.layers.items() if isinstance(v, dict) and "def" in v},   # 翌月の案（_plans・_adopted）は当番でない
            "accepted": accepted_counts(self.store.acceptances(), self.ym, self._today(), include_returned=True),
            "accepted_items": self._accepted_items(m),
            "last_said": m.history[-1]["said"] if m.history else None,
            "symbols": [{"symbol": st.symbol, "name": st.name} for st in fac.shift_types],
            "absence_sim": self.store.absence_sim(self.year, self.month),
            "returned_note": self._returned_note(m),
            "returned_people": self._returned_people(m),
            "dist_at": m.dist_at,
            "dist_changed": (sum(a != b for n, row in (m.table or {}).items()
                                 for a, b in zip(row, m.dist_table.get(n, row)))
                             if m.distributed and m.dist_table is not None else None),
        }

    def _returned_people(self, m) -> dict[str, str]:
        """返す案を採った月の、返した人ごとの1行（名前の横に出す。例「有給 0→1日・連勤 5→4日」）。
        採った後の表では黄色が消えるので、誰に何を返したかをここで見せる（2026-11-04 受け止めの2人）。"""
        plans = m.layers.get("_plans")
        if m.layers.get("_adopted") != "give" or not isinstance(plans, dict) or not plans.get("acc_ids"):
            return {}
        out = {}
        for p in plans.get("people") or []:
            parts = []
            if p.get("leave_after", 0) > p.get("leave_before", 0):
                parts.append(f"有給 {p['leave_before']}→{p['leave_after']}日")
            if p.get("run_after", 0) < p.get("run_before", 0):
                parts.append(f"連勤 {p['run_before']}→{p['run_after']}日")
            if parts:
                out[p["name"]] = "・".join(parts)
        return out

    def _returned_note(self, m) -> str:
        """前の月に引き受けた人へ有給と短い連勤を返す案を採った月だけ、表の題の下に出す1行。"""
        plans = m.layers.get("_plans")
        if m.layers.get("_adopted") != "give" or not isinstance(plans, dict) or not plans.get("acc_ids"):
            return ""
        # 返した物と引き受けの月は、案を作った時の記録から言う（片方だけ返した案や、前の月より前の引き受けもある。
        # 2026-11-04 Astra の指摘）。記録の無い古い案は、物と月を言わない
        what = {("leave", "run"): "有給と短い連勤", ("leave",): "有給", ("run",): "短い連勤"}.get(
            tuple(plans.get("stage") or ()))
        months = [int(x[5:7]) for x in plans.get("acc_months") or [] if len(x) >= 7]
        who = ("・".join(f"{n}月" for n in months) + "に引き受けた人に") if months else "引き受けた人に"
        return f"{who}、{what}を返した{self.month}月の表です" if what else f"{who}報いた{self.month}月の表です"
