"""ルールの見直し（画面8）のデータの土台と操作の裏側。毎月、表を作る前に開く。

見直す札（今週作る理由は2つだけ）
- 期限が来た  until が前の月の中にある毎月の札（「〈日〉で終わりました。〈翌月〉には効きません」）
- 先月増えた  前の月の表を作る間に話して増えた毎月の札（期限が来た札と両方に当たる時は「期限が来た」で1回だけ）
- 静か        何か月も続けて、外して作り直しても表がその札どおりだった毎月の札（quiet.py。片付けの候補）。
              ［今月も使う］と答えたら、その後また同じ月数続くまで出さない
ほか
- 変更なし    たたんで数だけ
- 今月だけの頼み  前の月の今月だけの札。翌月へ引き継がない（帳面の期間で自然に効かない）。数だけ知らせる

操作はすべて変更の帳面の tx（片付ける=remove・戻す=undo・期間を延ばす=change の until・変える=change の中身・今月も使う=答えだけ）。
答えは帳面の review の行で、tx と同じ1回の保存に入る。続きの場所は帳面の review の行から決まる（別に持たない）。
期限が過ぎた札は、答えないままでも・片付けても・戻しても、対象の月の表に効かない（帳面の期間で決まるため）。
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

from .ledger import Ledger, LedgerError
from .rules import may_cover
from .nextmonth import next_ym
from .store import Store

ANSWERS = ("archive", "extend", "keep", "change", "archive_partner")


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
    kind: str = ""
    months: int = 0      # 静かな札の、続けて静かだった月の数
    partner: str | None = None        # 重なりの札の相手（同じことを決めているもう1件）
    partner_name: str | None = None
    no_scene: bool = False            # 当てはまる場面が無かった（作り直さずに静かと数えた）
    loose: bool = False               # 重なりの札で、この札（rule_id）の方がゆるいと分かる時
    group: list[str] = field(default_factory=list)   # 一緒に答える札（同じ人の当てはまる場面が無い札・先月増えた札）
    group_names: list[str] = field(default_factory=list)
    pairs: list[tuple[str, str]] = field(default_factory=list)   # まとめた先月増えた札の中の、同じ所を決める組
    reason_text: str | None = None    # 事情の札の、足した時の理由


def _months_between(a: str, b: str) -> int:
    """a・b は YYYY-MM。b − a の月数。"""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


ADDED_GROUP_AT = 4    # 先月増えた札がこの件数以上なら1枚にまとめる（［全部 今月も使う］か［1件ずつ見る］）
QUIET_PER_MONTH = 5   # 1か月に見直しに出す静かな札の上限（多いと見直しにならない。施設の設定 review.quiet_per_month）


def review_cards(ledger: Ledger, year: int, month: int, quiet: dict[str, int] | None = None,
                 quiet_months: int = 3, quiet_per_month: int = QUIET_PER_MONTH,
                 overlap: dict[str, int] | None = None, no_scene: set[str] | None = None,
                 split: bool = False, added_group_at: int = ADDED_GROUP_AT,
                 aged: dict[str, int] | None = None, temp_months: int = 3) -> dict:
    """対象の月（year, month）の表を作る前の見直し。答えた札も並びに残す（最後の一覧で戻せるように）。
    quiet は札ごとの続けて静かだった月の数（quiet.streaks）。quiet_months 以上なら片付けの候補に出す。"""
    py, pm = _prev(year, month)
    ps, pe = _span(py, pm)
    start, _ = _span(year, month)
    # 並びは最初に答える前の帳面で決める（片付けた札も、最後の一覧で戻せるように並びに残す）
    ym = f"{year:04d}-{month:02d}"
    first = next((t["rev"] for t in ledger.txs if str(t.get("op_id") or "").startswith(f"review-{ym}-")), None)
    all_prev = ledger.rules_at(at_rev=(first - 1) if first else None, between=(ps, pe))
    # 前の月に効いていて対象の月には効かないルール（期限が来た・外した）。静かなルールがそれに守られていた
    # だけかもしれないので、重なる物は静かの候補にしない（Astra 2026-11-03 の指摘1。測った月にはあった守りが消える）
    te = _span(year, month)[1]
    target = ledger.rules_at(at_rev=(first - 1) if first else None, between=(start, te))
    gone = [r for rid, r in all_prev.items() if rid not in target]
    add_tx = {t["rule"]["id"]: t for t in ledger.txs if t["op"] == "add"}
    cards: list[Card] = []
    quiet_cards: list[Card] = []
    once_names: list[str] = []
    unchanged = 0
    once = 0
    for rid, r in all_prev.items():
        until = r.meta.get("until")
        if r.scope == "once":
            once += 1
            once_names.append(r.name)
            continue
        t = add_tx.get(rid)
        if until and ps <= until < start:
            cards.append(Card(rid, "expired", r.name, until, r.meta.get("said"), r.kind))
        elif t and t.get("from") == ps and r.meta.get("said"):
            cards.append(Card(rid, "added_last_month", r.name, until, r.meta.get("said"), r.kind))
        elif quiet and r.kind != "layer" and _quiet_run(ledger, rid, ym, quiet.get(rid, 0)) >= quiet_months \
                and not any(may_cover(r, g) for g in gone):
            quiet_cards.append(Card(rid, "quiet", r.name, until, r.meta.get("said"), r.kind,
                                    _quiet_run(ledger, rid, ym, quiet.get(rid, 0)), no_scene=rid in (no_scene or ())))
        elif aged and rid in aged and _quiet_run(ledger, rid, ym, aged[rid]) >= temp_months:
            # 一時的な事情の理由で足したルール（aging.py）。事情が終わっていないかを聞く
            quiet_cards.append(Card(rid, "aged", r.name, until, r.meta.get("said"), r.kind, aged[rid],
                                    reason_text=r.meta.get("reason")))
        else:
            unchanged += 1
    # 重なり（同じことを決めている2件）。片方ずつなら外せる組を1枚の札にして、どちらを片付けるか選んでもらう
    if overlap:
        used: set[str] = set()
        carded = {c.rule_id for c in cards}
        lap = sorted((rid for rid in overlap if rid in all_prev and all_prev[rid].scope == "rule" and rid not in carded
                      and _quiet_run(ledger, rid, ym, overlap[rid]) >= quiet_months
                      and not any(may_cover(all_prev[rid], g) for g in gone)))
        for a in lap:
            if a in used:
                continue
            b = next((x for x in lap if x != a and x not in used
                      and (may_cover(all_prev[a], all_prev[x]) or may_cover(all_prev[x], all_prev[a]))), None)
            if b is None:
                continue
            used |= {a, b}
            unchanged -= 2          # 2件とも変更なしに数えていた
            st = _stricter(all_prev[b], all_prev[a])
            if st is False:
                a, b = b, a         # 先に出す（おすすめに見える）片付けは、ゆるい方にする
            ra = all_prev[a]
            quiet_cards.append(Card(a, "overlap", ra.name, ra.meta.get("until"), ra.meta.get("said"), ra.kind,
                                    min(_quiet_run(ledger, a, ym, overlap[a]), _quiet_run(ledger, b, ym, overlap[b])),
                                    b, all_prev[b].name, loose=st is not None))
    # 同じ人の、当てはまる場面が無い札（表にいない人のルール）は1枚にまとめる。1回で決められるように
    merged: list[Card] = []
    by_who: dict[tuple, Card] = {}
    for c in quiet_cards:
        who = tuple(sorted(all_prev[c.rule_id].who or [])) if c.reason == "quiet" and c.no_scene else ()
        if who and who in by_who:
            head = by_who[who]
            head.group.append(c.rule_id)
            head.months = min(head.months, c.months)
            continue
        if who:
            by_who[who] = c
        merged.append(c)
    for c in merged:
        if c.group:
            n = 1 + len(c.group)
            names = "、".join(all_prev[x].name for x in [c.rule_id] + c.group)
            c.name = f"{'・'.join(all_prev[c.rule_id].who)}さんのルール {n}件（{names}）"
    quiet_cards = merged
    # 静かな札は長く続いた順に、月に quiet_per_month 件まで。残りは変更なしに数え、件数だけ知らせる
    quiet_cards.sort(key=lambda c: (not c.no_scene, -c.months, c.rule_id))   # 当てはまる場面が無い札を先に
    cards += quiet_cards[:quiet_per_month]
    quiet_more = sum(2 if c.reason == "overlap" else 1 + len(c.group) for c in quiet_cards[quiet_per_month:])
    unchanged += quiet_more
    # 先月増えた札が多い月は1枚にまとめる。毎月何件も［今月も使う］を押し続けるだけになる、と読み手が書いた（2026-11-04）。
    # ［1件ずつ見る］（split）か、まとめた札の中のどれかに1件ずつ答えた後は、まとめない
    answered = answers_for(ledger, year, month)
    added = [c for c in cards if c.reason == "added_last_month"]
    head_grouped = any(answered.get(c.rule_id, {}).get("group") for c in added)
    one_by_one = any(c.rule_id in answered and not answered[c.rule_id].get("group") for c in added)
    if len(added) >= added_group_at and (head_grouped or (not split and not one_by_one)):
        head = added[0]
        head.group = [c.rule_id for c in added[1:]]
        head.group_names = [c.name for c in added]
        # まとめた中に同じ所を決める2件があると、［全部 今月も使う］で気づけない（読み手 2026-11-04）。組を添える
        for i, x in enumerate(added):
            for y in added[i + 1:]:
                rx, ry = all_prev[x.rule_id], all_prev[y.rule_id]
                if (rx.who or None) == (ry.who or None) and (may_cover(rx, ry) or may_cover(ry, rx)):
                    head.pairs.append((x.name, y.name))
        head.name = f"先月増えたルール {len(added)}件"
        cards = [c for c in cards if c.reason != "added_last_month" or c is head]
    order = {"expired": 0, "added_last_month": 1, "quiet": 2, "overlap": 2, "aged": 2}
    # 片付けの候補は上で決めた順（場面の無い札が先・長く続いた順）のまま。番号順に並べ直すと、それが崩れた
    cards.sort(key=lambda c: (order[c.reason], c.rule_id if order[c.reason] < 2 else ""))
    answers = answers_for(ledger, year, month)
    pos = next((i for i, c in enumerate(cards) if c.rule_id not in answers), len(cards))
    return {"cards": cards, "answers": answers, "cursor": pos, "unchanged": unchanged, "once": once,
            "once_names": once_names,
            "quiet_more": quiet_more}


def _stricter(x, y) -> bool | None:
    """x が y より厳しいか（回数の上限が小さい・下限が大きい）。分からない時は None。"""
    if x.kind != "count" or y.kind != "count":
        return None
    px, py = x.params, y.params
    if px.get("max") is not None and py.get("max") is not None and px.get("min") == py.get("min") and px["max"] != py["max"]:
        return px["max"] < py["max"]
    if px.get("min") is not None and py.get("min") is not None and px.get("max") == py.get("max") and px["min"] != py["min"]:
        return px["min"] > py["min"]
    return None


def _quiet_run(ledger: Ledger, rid: str, ym: str, streak: int) -> int:
    """静かな月の数を、前に［今月も使う］と答えた月より後だけに切る（同じ札を毎月聞かない）。"""
    cancelled = {t["target_rev"] for t in ledger.txs if t["op"] == "undo"}
    kept = [t["review"]["month"] for t in ledger.txs
            if t["op"] == "review" and t["rev"] not in cancelled
            and (rid in (t["rule_id"], t["review"].get("partner")) or rid in t["review"].get("group", []))
            and t["review"].get("reason") in ("quiet", "overlap", "aged") and t["review"].get("answer") == "keep"
            and (t["review"]["month"] < ym or (t["review"].get("from") == "cleanup" and t["review"]["month"] <= ym))]
    return min(streak, _months_between(max(kept), ym)) if kept else streak


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
           new_rule: dict | None = None, said: str | None = None, now: dt.datetime | None = None,
           quiet: dict[str, int] | None = None, quiet_months: int = 3,
           quiet_per_month: int = QUIET_PER_MONTH, overlap: dict[str, int] | None = None,
           no_scene: set[str] | None = None, split: bool = False, aged: dict[str, int] | None = None,
           temp_months: int = 3) -> dict:
    """1枚に答える。答えと tx は帳面の1回の保存に入る（途中で止まっても、前か後の完全な状態で開く）。"""
    if what not in ANSWERS:
        raise LedgerError(f"知らない答え {what}")
    led = store.ledger()
    view = review_cards(led, year, month, quiet, quiet_months, quiet_per_month, overlap, no_scene, split,
                        aged=aged, temp_months=temp_months)
    card = next((c for c in view["cards"] if c.rule_id == rule_id), None)
    if card is None:
        raise LedgerError(f"見直す札ではない {rule_id}")
    if rule_id in view["answers"]:
        raise LedgerError("もう答えた札です。戻してから答え直してください")
    at = (now or dt.datetime.now()).isoformat(timespec="seconds")
    ym = f"{year:04d}-{month:02d}"
    op_id = f"review-{ym}-{rule_id}"
    revs = []
    extra = {}
    if what == "change":
        # 話して直した中身は、対象の月から効く新しい札にする。元の札は前の月の末で閉じる
        # （元の札を書き換えると、確定済みの前の月の札まで変わる。2026-11-02 のレビューの指摘A）
        cur = led.rules_at()[rule_id]
        if not new_rule or new_rule.get("kind") != cur.kind or cur.kind == "layer":
            raise LedgerError("このルールの変え方として読めませんでした。同じルールの直し方を1つ言ってください")
        start = _span(year, month)[0]
        py, pm = _prev(year, month)
        prev_end = _span(py, pm)[1]
        revs.append(led.append("change", rule_id=rule_id, rule={"name": cur.name}, op_id=op_id,
                               frm=cur.meta.get("from"), until=prev_end,
                               meta={"kind": "change_close", "at": at})["rev"])
        # 外した番号も使い回さない（session._next_id と同じ数え方。ひとつ戻すと別の頼みを消していた。敵対レビュー 2026-11-03）
        ids = {t["rule"]["id"] for t in led.txs if t["op"] == "add" and isinstance(t.get("rule"), dict)} | set(led.rules_at())
        n = max([int(r[1:]) for r in ids if r[1:].isdigit()] + [100]) + 1
        rd = {k: new_rule[k] for k in ("name", "params", "who", "when", "flex", "kind") if k in new_rule}
        rd.update({"id": f"r{n}", "scope": cur.scope,
                   "meta": {**new_rule.get("meta", {}), "said": said, "at": at, "source": "話した",
                            "changed_from": rule_id, "reason": cur.meta.get("reason")}})
        revs.append(led.append("add", rule=rd, op_id=op_id, frm=start,
                               until=new_until or cur.meta.get("until"),
                               meta={"kind": "change", "at": at, "said": said})["rev"])
        extra = {"new_name": rd.get("name"), "said": said, "new_rule_id": rd["id"]}
    elif what == "archive_partner":
        if card.reason != "overlap" or not card.partner:
            raise LedgerError("この札にはもう片方のルールがありません")
        revs.append(led.append("remove", rule_id=card.partner, op_id=op_id, meta={"kind": "archive", "at": at})["rev"])
        extra = {"partner": card.partner}
    elif what == "archive":
        for rid in [rule_id] + card.group:
            revs.append(led.append("remove", rule_id=rid, op_id=op_id, meta={"kind": "archive", "at": at})["rev"])
    elif what == "extend":
        if not new_until or new_until < _span(year, month)[0]:
            raise LedgerError("延ばす終わりの日が要ります（対象の月より後）")
        cur = led.rules_at()[rule_id]
        rd = {"when": None} if cur.kind == "count" else {}
        revs.append(led.append("change", rule_id=rule_id, rule=rd, op_id=op_id,
                               frm=cur.meta.get("from"), until=new_until, meta={"kind": "extend", "at": at})["rev"])
    if card.partner:
        extra = {"partner": card.partner, **extra}
    if card.group:
        extra = {"group": card.group, **extra}
    rv = led.append("review", rule_id=rule_id, rule={"month": ym, "answer": what, "at": at, "tx": revs,
                                                     "reason": card.reason, **extra}, op_id=op_id)
    store.save_ledger(led)  # 答え・tx・続きの場所（帳面から決まる）を1回で保存
    return {"answer": what, "review_rev": rv["rev"], "tx": revs}


def undo_answer(store: Store, year: int, month: int, rule_id: str) -> None:
    """最後の一覧から、その札の答えだけを戻す（ほかの札の答えは残る）。"""
    led = store.ledger()
    a = answers_for(led, year, month).get(rule_id)
    if a is None:
        raise LedgerError("戻す答えがない")
    cancelled = {t["target_rev"] for t in led.txs if t["op"] == "undo"}
    for rev in a.get("tx", []):
        if rev in cancelled:
            continue   # 片付けた後に一覧から戻した札（restore）。二重に取り消すと、また外れてしまう
        led.append("undo", target_rev=rev, op_id=f"undo-review-{rule_id}")
    led.append("undo", target_rev=a["rev"], op_id=f"undo-review-{rule_id}")
    store.save_ledger(led)


def restore(store: Store, rule_id: str, at: str) -> str:
    """片付けた札を戻す（spec 画面8・計画 M6b）。片付けた remove を取り消すので、言った言葉・理由・期間も元のまま。
    今の月の表は作り直さず、次に表を作る時から効く。戻した札の名前を返す。"""
    led = store.ledger()
    hit = next((h for h in reversed(history(led)) if h["rule_id"] == rule_id), None)
    if hit is None:
        raise LedgerError("片付けたルールの中に見つかりません")
    led.append("undo", target_rev=hit["rev"], op_id=f"restore-{rule_id}", meta={"kind": "restore", "at": at})
    store.save_ledger(led)
    return hit["name"]


def history(ledger: Ledger) -> list[dict]:
    """片付けた札（履歴）。片付けても過去の月の表と引き受けの記録は消さない（M6a）。"""
    cancelled = {t["target_rev"] for t in ledger.txs if t["op"] == "undo"}
    add = {t["rule"]["id"]: t["rule"] for t in ledger.txs if t["op"] == "add"}
    return [{"rule_id": t["rule_id"], "name": add.get(t["rule_id"], {}).get("name"), "rev": t["rev"]}
            for t in ledger.txs if t["op"] == "remove" and t.get("meta", {}).get("kind") == "archive"
            and t["rev"] not in cancelled]
