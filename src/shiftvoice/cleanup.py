"""ルールの整理（docs/rule-cleanup.md の6）。片付けの候補を、専用の画面で種類ごとにまとめて、その時に全部決める。

前は毎月の見直しに月5件ずつ混ぜていたが、作者が素材を見て「その時直すべきものは、その時に全部やるべき」
「なんで整理の検討に入っているかが分からない」と書いた（2026-11-04）。そこで、
- 毎月の見直しは「期限が来た・先月増えた」だけに戻し
- 整理は一覧の［整理する］から開く専用の画面にし、冒頭で理由を1文で言い
- 種類ごと（今の名簿にいない人・同じことを決めている2件・無くても表が同じだった・一時的な事情）に、まとめて片付ける
候補の選び方（3か月続いたか・答えた後は聞かない・守りが消える月は出さない）は見直しの札と同じ（review.review_cards）。
"""
from __future__ import annotations

import datetime as dt

from .aging import aged, temporary_months_setting
from .ledger import LedgerError
from .nextmonth import next_ym
from .quiet import no_scene_ids, overlap_streaks, quiet_months_setting, streaks
from .review import review_cards
from .store import Store

KINDS = ("absent", "overlap", "quiet", "aged")


def view(store: Store, year: int, month: int) -> dict:
    """今の月（year, month）で開いた時の整理の画面の中身。測った最後の月（今の月）までの記録で選ぶ。"""
    ny, nm = next_ym(year, month)
    led = store.ledger()
    qm = quiet_months_setting(store)
    v = review_cards(led, ny, nm, streaks(store, ny, nm), qm, 10 ** 6, overlap_streaks(store, ny, nm),
                     no_scene_ids(store, ny, nm), aged=aged(store, ny, nm), temp_months=temporary_months_setting(store))
    rules = led.rules_at()
    groups: dict[str, list[dict]] = {k: [] for k in KINDS}
    for c in v["cards"]:
        # 見直しを先に始めていると、札はその時点の帳面で選ばれる。整理で片付けた後の物は出さない
        if c.rule_id not in rules or (c.partner and c.partner not in rules):
            continue
        c.group = [i for i in c.group if i in rules]
        if c.reason == "quiet" and c.no_scene:
            ids = [c.rule_id] + c.group
            groups["absent"].append({"who": "・".join(rules[c.rule_id].who or []), "ids": ids,
                                     "names": [rules[i].name for i in ids], "months": c.months})
        elif c.reason == "overlap":
            groups["overlap"].append({"loose": c.rule_id, "strict": c.partner, "known": c.loose,
                                      "loose_name": c.name, "strict_name": c.partner_name, "months": c.months})
        elif c.reason == "quiet":
            groups["quiet"].append({"id": c.rule_id, "name": c.name, "months": c.months})
        elif c.reason == "aged":
            groups["aged"].append({"id": c.rule_id, "name": c.name, "months": c.months, "reason": c.reason_text})
    n = (sum(len(g["ids"]) for g in groups["absent"]) + len(groups["overlap"]) + len(groups["quiet"])
         + len(groups["aged"]))
    total = sum(1 for r in led.rules_at().values() if r.scope == "rule")
    return {"total": total, "count": n, "months": qm, "groups": groups,
            "head": (f"ルールは{total}件。このうち{n}件は、{qm}か月のあいだ表を作るのに効いていなかったか、"
                     "一時的な事情で足したルールです。片付けると一覧が読みやすくなります（あとから一覧の"
                     "「片付けたルール」で戻せます）") if n else f"ルールは{total}件。いま片付けの候補はありません"}


def apply(store: Store, year: int, month: int, archive: list[str], keep: list[str], now: dt.datetime) -> dict:
    """選んだルールを片付け、残すと決めたルールは次に同じ月数だけ測れるまで候補に出さない（見直しの［今月も使う］と同じ）。
    全部が1回の保存に入る。片付けは履歴に残り、一覧から戻せる（review.restore）。"""
    led = store.ledger()
    cur = led.rules_at()
    bad = [r for r in archive + keep if r not in cur]
    if bad:
        raise LedgerError(f"もう無いルールです（{'・'.join(bad)}）。画面を開き直してください")
    # 残すの印は、見直しの答えと同じく翌月に付ける。次に候補になるのは、その後また同じ月数だけ測れた時（review._quiet_run）
    ny, nm = next_ym(year, month)
    ym = f"{ny:04d}-{nm:02d}"
    at = now.isoformat(timespec="seconds")
    op = f"cleanup-{at}"
    reasons = _reasons(store, year, month)
    for rid in archive:
        led.append("remove", rule_id=rid, op_id=op, meta={"kind": "archive", "at": at, "from": "cleanup"})
    for rid in keep:
        led.append("review", rule_id=rid, rule={"month": ym, "answer": "keep", "at": at, "tx": [],
                                                "reason": reasons.get(rid, "quiet"), "from": "cleanup"}, op_id=op)
    store.save_ledger(led)
    return view(store, year, month)


def _reasons(store: Store, year: int, month: int) -> dict[str, str]:
    g = view(store, year, month)["groups"]
    out = {}
    for x in g["overlap"]:
        out[x["loose"]] = out[x["strict"]] = "overlap"
    for x in g["aged"]:
        out[x["id"]] = "aged"
    return out
