"""ルールの見直し（画面8）のデータの土台と操作の裏側。毎月、表を作る前に開く。

見直す札（今週作る理由は2つだけ）
- 期限が来た  until が前の月の中にある毎月の札（「〈日〉で終わりました。〈翌月〉には効きません」）
- 先月増えた  前の月の表を作る間に話して増えた毎月の札（期限が来た札と両方に当たる時は「期限が来た」で1回だけ）
ほか
- 変更なし    たたんで数だけ
- 今月だけの頼み  前の月の今月だけの札。翌月へ引き継がない（帳面の期間で自然に効かない）。数だけ知らせる

操作はすべて変更の帳面の tx（片付ける=remove・戻す=undo・期間を延ばす=change の until・今月も使う=答えだけ）。
答えは帳面の review の行で、tx と同じ1回の保存に入る。続きの場所は帳面の review の行から決まる（別に持たない）。
期限が過ぎた札は、答えないままでも・片付けても・戻しても、対象の月の表に効かない（帳面の期間で決まるため）。
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass

from .ledger import Ledger, LedgerError
from .nextmonth import next_ym
from .store import Store

ANSWERS = ("archive", "extend", "keep", "change")


def _span(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def _prev(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


@dataclass
class Card:
    rule_id: str
    reason: str          # expired / added_last_month
    name: str
    until: str | None
    said: str | None


def review_cards(ledger: Ledger, year: int, month: int) -> dict:
    """対象の月（year, month）の表を作る前の見直し。答えた札も並びに残す（最後の一覧で戻せるように）。"""
    py, pm = _prev(year, month)
    ps, pe = _span(py, pm)
    start, _ = _span(year, month)
    # 並びは最初に答える前の帳面で決める（片付けた札も、最後の一覧で戻せるように並びに残す）
    ym = f"{year:04d}-{month:02d}"
    first = next((t["rev"] for t in ledger.txs if str(t.get("op_id") or "").startswith(f"review-{ym}-")), None)
    all_prev = ledger.rules_at(at_rev=(first - 1) if first else None, between=(ps, pe))
    add_tx = {t["rule"]["id"]: t for t in ledger.txs if t["op"] == "add"}
    cards: list[Card] = []
    unchanged = 0
    once = 0
    for rid, r in all_prev.items():
        until = r.meta.get("until")
        if r.scope == "once":
            once += 1
            continue
        t = add_tx.get(rid)
        if until and ps <= until < start:
            cards.append(Card(rid, "expired", r.name, until, r.meta.get("said")))
        elif t and t.get("from") == ps and r.meta.get("said"):
            cards.append(Card(rid, "added_last_month", r.name, until, r.meta.get("said")))
        else:
            unchanged += 1
    cards.sort(key=lambda c: (c.reason != "expired", c.rule_id))
    answers = answers_for(ledger, year, month)
    pos = next((i for i, c in enumerate(cards) if c.rule_id not in answers), len(cards))
    return {"cards": cards, "answers": answers, "cursor": pos, "unchanged": unchanged, "once": once}


def answers_for(ledger: Ledger, year: int, month: int) -> dict[str, dict]:
    """札ごとの今の答え（取り消された答えは除く）。"""
    ym = f"{year:04d}-{month:02d}"
    cancelled = {t["target_rev"] for t in ledger.txs if t["op"] == "undo"}
    out: dict[str, dict] = {}
    for t in ledger.txs:
        if t["op"] == "review" and t["review"].get("month") == ym and t["rev"] not in cancelled:
            out[t["rule_id"]] = {**t["review"], "rev": t["rev"]}
    return out


def answer(store: Store, year: int, month: int, rule_id: str, what: str, *, new_until: str | None = None,
           now: dt.datetime | None = None) -> dict:
    """1枚に答える。答えと tx は帳面の1回の保存に入る（途中で止まっても、前か後の完全な状態で開く）。"""
    if what not in ANSWERS:
        raise LedgerError(f"知らない答え {what}")
    led = store.ledger()
    view = review_cards(led, year, month)
    card = next((c for c in view["cards"] if c.rule_id == rule_id), None)
    if card is None:
        raise LedgerError(f"見直す札ではない {rule_id}")
    if rule_id in view["answers"]:
        raise LedgerError("もう答えた札です。戻してから答え直してください")
    at = (now or dt.datetime.now()).isoformat(timespec="seconds")
    ym = f"{year:04d}-{month:02d}"
    op_id = f"review-{ym}-{rule_id}"
    revs = []
    if what == "archive":
        revs.append(led.append("remove", rule_id=rule_id, op_id=op_id, meta={"kind": "archive", "at": at})["rev"])
    elif what == "extend":
        if not new_until or new_until < _span(year, month)[0]:
            raise LedgerError("延ばす終わりの日が要ります（対象の月より後）")
        cur = led.rules_at()[rule_id]
        rd = {"when": None} if cur.kind == "count" else {}
        revs.append(led.append("change", rule_id=rule_id, rule=rd, op_id=op_id,
                               frm=cur.meta.get("from"), until=new_until, meta={"kind": "extend", "at": at})["rev"])
    rv = led.append("review", rule_id=rule_id, rule={"month": ym, "answer": what, "at": at, "tx": revs,
                                                     "reason": card.reason}, op_id=op_id)
    store.save_ledger(led)  # 答え・tx・続きの場所（帳面から決まる）を1回で保存
    return {"answer": what, "review_rev": rv["rev"], "tx": revs}


def undo_answer(store: Store, year: int, month: int, rule_id: str) -> None:
    """最後の一覧から、その札の答えだけを戻す（ほかの札の答えは残る）。"""
    led = store.ledger()
    a = answers_for(led, year, month).get(rule_id)
    if a is None:
        raise LedgerError("戻す答えがない")
    for rev in a.get("tx", []):
        led.append("undo", target_rev=rev, op_id=f"undo-review-{rule_id}")
    led.append("undo", target_rev=a["rev"], op_id=f"undo-review-{rule_id}")
    store.save_ledger(led)


def history(ledger: Ledger) -> list[dict]:
    """片付けた札（履歴）。片付けても過去の月の表と引き受けの記録は消さない（M6a）。"""
    cancelled = {t["target_rev"] for t in ledger.txs if t["op"] == "undo"}
    add = {t["rule"]["id"]: t["rule"] for t in ledger.txs if t["op"] == "add"}
    return [{"rule_id": t["rule_id"], "name": add.get(t["rule_id"], {}).get("name"), "rev": t["rev"]}
            for t in ledger.txs if t["op"] == "remove" and t.get("meta", {}).get("kind") == "archive"
            and t["rev"] not in cancelled]
