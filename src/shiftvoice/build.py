"""月の表を作る（帳面の決まり＋撮影用の前提）と、段の順（4章・試験 S4）。

段の順
- 段1 本人の希望と運用の都合を外せなくして解く（fixed は常に外せない）
- 作れないと分かった（infeasible）なら 段3 希望だけ守る（運用の都合を外す）→ 段2 運用だけ守る（希望を外す）→ 段4 全部外す。
  運用の都合を先に外し、本人の希望休は最後まで守る（作者の答え 2026-11-06。前は希望を先に外していた）
- 時間切れ（作れないとは分かっていない）なら 段3 だけ試す。段4 へは自動で進まない。
  見つからなければ「この時間では見つかりませんでした」と言い、もう少し考えるか、どれを譲るかを聞く
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from .model import Facility, Month, Rule, Staff
from .solver import SolveResult, solve_month
from .store import Store

TIMEOUT_MESSAGE = "この時間では見つかりませんでした。もう少し考えますか、どれを譲るかを決めますか"
# C3（簡素な対応。17章 A23）。芯は探さず、作れない・時間切れのどちらも一文で聞き返す
C3_MESSAGE = "このルールを入れると、この時間では見つかりませんでした。外しますか、ゆるめますか"


@dataclass
class StagedResult:
    result: SolveResult            # 最後に使った段の結果
    stages: list[dict] = field(default_factory=list)  # 段ごとの状態（段・状態・秒）
    message: str | None = None     # 作成者へ聞く一文（表が無い時だけ）
    note: str | None = None        # 表はできたが段を下げた時の知らせ
    choke: list[dict] = field(default_factory=list)   # 段1で組めなかった時の詰まり所（choke.chokepoints。explain の時だけ）

    @property
    def ok(self) -> bool:
        return self.result.status == "ok"

    @property
    def seconds(self) -> float:
        return sum(s["seconds"] for s in self.stages)


# 体の目安（次の勤務までの間が短い並びの罰）を外して作った時の知らせ。ルールは段1のまま全部守っている
SHORT_REST_NOTE = "時間内に、次の勤務までの間まで整えきれなかったので、間の目安を外して作りました（ルールは全部守っています）"

STAGE_NOTE = {
    2: "本人の希望を守ったままでは作れなかったので、希望を外せる形で作りました（運用の都合は守っています）",
    3: "運用の都合を外せる形で作りました（本人の希望は守っています）",
    4: "全部のルールを外せる形で作りました（動かせないルールだけ守っています）",
}


def broken_text(broken: list[dict], names: set[str], limit: int = 6) -> str:
    """段を下げて作った時に、実際に外れた本人の希望と運用の都合を一文で言う。人ごとの数のルールは、人と数まで言う。"""
    groups: dict[str, list[dict]] = {}
    for b in broken:
        if b["flex"] in ("operational", "wish"):
            groups.setdefault(b["name"], []).append(b)
    parts = []
    for name, items in groups.items():
        who = [b for b in items if b["key"] in names]
        if len(who) != len(items):
            parts.append(f"{name}が{len(items)}か所")
            continue
        ones = [f"{b['key']}さん{b['value']}" for b in who[:limit]] + ([f"ほか{len(who) - limit}人"] if len(who) > limit else [])
        same = items[0]["lo"] is not None and items[0]["lo"] == items[0]["hi"]
        parts.append(f"{name}{'（' + str(items[0]['lo']) + '）' if same else ''}が{'・'.join(ones)}")
    return "外れたのは、" + "、".join(parts) if parts else ""


CONSULT_LIMIT = 6


def day_off_consult(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
                    res: SolveResult) -> tuple[str, list[dict]]:
    """段を下げて作った表で外れた希望休を、人と日と、入れると守れなくなるルールで言い、本人と相談するよう言う
    （作者 2026-11-07「希望休は原則ハードルール。どうしても組めない時のみ相談する」）。ルールは、作った表のそのマスを
    希望休にした時に新しく破れるルール（配慮の強さは除く。多くて2件）。返すのは（一文, 外れた希望休の一覧）。"""
    from .rules import check_table, is_day_off_wish
    by_id = {r.id: r for r in rules}
    off = [b for b in res.broken if b["rule"] in by_id and is_day_off_wish(by_id[b["rule"]], facility)]
    if not off or not res.table:
        return "", off
    before = {(b["rule"], b["key"]) for b in res.broken}
    parts = []
    for b in off[:CONSULT_LIMIT]:
        r = by_id[b["rule"]]
        who, d = b["key"].rsplit("/", 1)
        d = int(d)
        if r.meta.get("derived") == "rest_not_ake":
            parts.append(f"{who}さんの{d + 1}日（前の日が夜勤で、明けになります）")
            continue
        t = {k: list(v) for k, v in res.table.items()}
        t[who][d - 1] = r.params["in"][0]
        new = [x for x in check_table(rules, facility, staff, month, t)
               if (x["rule"], x["key"]) not in before and x["flex"] != "negotiable" and x["rule"] != r.id]
        names = list(dict.fromkeys(x["name"] for x in new))[:2]
        parts.append(f"{who}さんの{d}日" + (f"（入れると{'・'.join('「' + n + '」' for n in names)}が守れません）"
                                           if names else ""))
    if len(off) > CONSULT_LIMIT:
        parts.append(f"ほか{len(off) - CONSULT_LIMIT}日")
    return "守れなかった希望休は、" + "、".join(parts) + "です。本人と相談してください", off


def impossible_wishes(facility: Facility, rules: list[Rule], month: Month) -> tuple[list[Rule], list[str]]:
    """前の月の末の夜勤の明けと重なる1日の希望休と、動かせないマスと重なる希望休は、どう組んでも叶わない。残すと表全体が
    作れないと分かって段を下げ、関係のない人の希望休まで外れた（2026-11-06 通しの試し。1人の1日で10件外れた）。その日だけ外す。"""
    out, notes = [], []
    ake = {t.symbol for t in facility.shift_types if t.kind == "ake"}
    nights = _ake_as_rest_nights(facility)
    rest = {t.symbol for t in facility.shift_types if t.kind == "rest"}
    off_syms = {t.symbol for t in facility.shift_types if t.kind in ("rest", "leave")}
    for r in rules:
        if not (r.kind == "cell" and r.flex == "wish" and r.when and r.who and len(r.who) == 1):
            out.append(r)
            continue
        who, want = r.who[0], set(r.params.get("in") or [])
        tail = month.prev_tail.get(who)
        bad: set[int] = set()
        if 1 in r.when and tail and tail[-1] in facility.symbols and facility.shift(tail[-1]).next_is_ake and not want & ake:
            bad.add(1)
            notes.append(f"{who}さんの1日は前の月の夜勤の明けなので、希望休を入れられません")
        elif 1 in r.when and tail and nights and tail[-1] in nights and want and want <= rest:
            bad.add(1)
            notes.append(f"{who}さんの1日は前の月の夜勤の明けなので、希望休を入れられません")
        night_pins = {d for o in rules if nights and o.kind == "cell" and o.flex == "fixed" and o.who == [who]
                      and set(o.params.get("in") or []) and set(o.params["in"]) <= nights for d in (o.when or [])}
        for o in rules:   # 後から動かせない形で頼んだマス（希望休の日の「10日の〇〇さん、日8に」）。頼んだ方を残す
            if o.kind == "cell" and o.flex == "fixed" and o.who == [who] and o.params.get("in") and not want & set(o.params["in"]):
                for d in set(o.when or []) & set(r.when) - bad:
                    bad.add(d)
                    if not (want <= off_syms and set(o.params["in"]) <= off_syms):
                        notes.append(f"{who}さんの{d}日は「{o.name}」があるので、希望休を守れませんでした")
                    elif d - 1 in night_pins:   # 有給でも、前の日が動かせない夜勤なら明けになる（明けを公と書く施設。Astra）
                        notes.append(f"{who}さんの{d}日は前の日が夜勤なので、希望休が明けになります")
                    # 希望休の日の有給は、その日も休みなので「守れませんでした」とは言わない
        if set(r.when) - bad:
            r = replace(r, when=[d for d in r.when if d not in bad]) if bad else r
            out.append(r)
            if nights and want and want <= rest:
                # 明けを「公」と書く施設では、希望休の日が夜勤の明けになると、休めていないのに希望どおりに見えた（2026-11-07
                # 実際の施設の表を画面の口で作った通しの試しで4日）。希望休の前の日は夜勤にしない（希望休と同じ強さ）
                # 前の日に動かせない夜勤を頼んであれば、その日は足さない（足すとぶつかって段が下がり、関係のない人の
                # 希望休まで外れた。Astra）。その希望休は明けになるので知らせる
                pinned = {d for o in rules if o.kind == "cell" and o.flex == "fixed" and o.who == [who]
                          and set(o.params.get("in") or []) and set(o.params["in"]) <= nights for d in (o.when or [])}
                before = [d - 1 for d in r.when if d >= 2 and d - 1 not in pinned]
                for d in r.when:
                    if d - 1 in pinned:
                        notes.append(f"{who}さんの{d}日は前の日が夜勤なので、希望休が明けになります")
                if before:
                    out.append(replace(r, id=f"{r.id}-ake", name=f"{r.name}（前の日は夜勤にしない）",
                                       params={"not_in": sorted(nights)}, when=before,
                                       meta={**r.meta, "derived": "rest_not_ake"}))
    return out, notes


def _ake_as_rest_nights(facility: Facility) -> set[str]:
    """明けの記号が無く、夜勤の翌日を休み（公）と書く施設の夜勤の記号（日をまたぐ勤務）。明けの記号がある施設は空。"""
    if any(t.kind == "ake" for t in facility.shift_types):
        return set()
    out = set()
    for t in facility.shift_types:
        if t.kind != "work" or t.next_is_ake:
            continue
        if t.start and t.hours:
            h, m = (int(x) for x in t.start.split(":"))
            if h + m / 60 + t.hours > 24:
                out.add(t.symbol)
        elif t.symbol == "夜" or "夜勤" in (t.name or ""):
            out.add(t.symbol)   # 時刻を空欄にした夜勤も、名前で夜勤と見る（Astra）
    return out


def solve_staged(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
                 **kw) -> StagedResult:
    """段の順で解く。叶わない希望休の知らせは、どの段で作れても添える。"""
    rules, notes = impossible_wishes(facility, rules, month)
    out = _solve_staged(facility, staff, rules, month, **kw)
    if notes:
        out.note = "。".join(notes + ([out.note] if out.note else []))
    return out


# 取り込んだ施設で一から表を作る時、段1が時間切れなら、段を下げる前にこの計算の量でもう一度段1を探す（作者 2026-11-07
# 「希望休は原則守る。外す前にほかの調整を探す」）。実際の施設の10月の写し（17人・3ユニット、名前は作り話）で、量40では
# 一度も見つからず段3へ下がって夜勤が2人でない日が8日残ったが、量300では7回とも、全部のルールと希望休51日を守る表が
# 見つかった（量150では2回に1回）。待ちはこの Mac で1〜2分ほど増える
PATIENCE_DTIME = 300.0


def patience_of(facility: Facility) -> float | None:
    """一から表を作る時に段1を長く探す計算の量。取り込んだ施設（設定に pasted_month がある）だけ。施設の設定
    solve.patience_dtime があればその量（0で止める）。見本の施設と公開版（SHIFTVOICE_PUBLIC=1。重い処理の枠を長く
    占めないように）は None。環境変数 SHIFTVOICE_PATIENCE（0で止める。試験で使う）があればそれを先に見る。"""
    if os.environ.get("SHIFTVOICE_PUBLIC") == "1" or "pasted_month" not in facility.settings:
        return None
    env = os.environ.get("SHIFTVOICE_PATIENCE")
    v = float(env) if env not in (None, "") else facility.settings.get("solve", {}).get("patience_dtime", PATIENCE_DTIME)
    return float(v) if v else None


# 運用の都合の回数のルールを外す時の、1人あたりのずれの上限。小さい方から試す。3の次がすぐ無制限だと、公休9日の施設で
# 14日の希望休が1件あるだけで2人が公休26日・28日になった（Astra 2026-11-06 の指摘2）
DEV_CAPS = (1, 2, 3, 5, 8, 13, None)


def _solve_staged(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
                  **kw) -> StagedResult:
    """段の順で解く。秒は全段の合計で数え、段ごとの状態を残す。
    patience（計算の量）を渡された時だけ、段1が時間切れなら段を下げる前に、その量で段1をもう一度探す（patience_of）。
    一言の直し・静かなルールの確かめ・作れない時の診断は渡さないので、今までどおりの速さ"""
    stages: list[dict] = []
    patience = kw.pop("patience", None)
    explain = kw.pop("explain", False)
    original = facility

    def run(stage: int) -> SolveResult:
        secs = 0.0
        for cap in DEV_CAPS if stage >= 3 else (None,):
            r = solve_month(facility, staff, rules, month, stage=stage, dev_cap=cap, **kw)
            secs += r.seconds
            if r.status == "ok":
                break
        stages.append({"stage": stage, "status": r.status, "seconds": round(secs, 3)})
        return r

    r = run(1)
    if r.status == "ok":
        return StagedResult(r, stages)
    # 体の目安（次の勤務までの間が短い並びの罰）を入れた施設で、段1が時間内に見つからない時は、ルールを緩める前に
    # 目安を外して段1で作り直す。ルールを守るのが先（2026-11-04、職員48人の試算で、目安のせいで段3へ下がり
    # 運用の都合を外していた。目安なしの表を出発点に磨く2段の作り方も試したが、48人では磨ききれず、32人では遅くなった）
    quality = facility.settings.get("quality", {})
    if r.status == "timeout" and quality.get("short_rest"):
        lite = replace(facility, settings={**facility.settings, "quality": {**quality, "short_rest": 0}})
        r2 = solve_month(lite, staff, rules, month, stage=1, **kw)
        stages.append({"stage": 1, "status": r2.status, "seconds": round(r2.seconds, 3), "without": "short_rest"})
        if r2.status == "ok":
            return StagedResult(r2, stages, note=SHORT_REST_NOTE)
        facility = lite   # 目安なしでも段1で作れない時は、目安を外したまま今までの段の順へ
        r = r2
    if r.status == "timeout" and patience and "max_dtime" not in kw:
        # 段を下げる前に、計算の量を増やして段1をもう一度探す（目安は戻す。量300なら目安ありでも見つかった）。
        # 作れないと分かった時（infeasible）は長く探しても無駄なので、ここへは来ない
        r3 = solve_month(original, staff, rules, month, stage=1, max_dtime=patience, **kw)
        stages.append({"stage": 1, "status": r3.status, "seconds": round(r3.seconds, 3), "patience": patience})
        if r3.status == "ok":
            return StagedResult(r3, stages)
        r = r3
    # 全部の希望休とルールで段1が組めなかった。段を下げる前に、いちばんの詰まり所を見つけて、作成者が手をつける前に
    # 伝える（作者 2026-11-07。日勤専属の人に早出を頼むなどの対処を先に試し、どうしてもダメな時だけ希望休を相談する）。
    # 一から表を作る時（explain）だけ。日ごとの小さなモデルなので、月全体が時間切れでも一瞬で出る
    chokes = []
    if explain:
        from .choke import chokepoints
        chokes = chokepoints(original, staff, rules, month)
    if r.status == "infeasible":
        order = [3, 2, 4]   # 運用の都合を先に外し、本人の希望は最後まで守る（作者の答え 2026-11-06）
    else:  # 時間切れは段3だけ。希望を外す段（2・4）へは進まない
        order = [3]
    timed_out = r.status == "timeout"
    for st in order:
        r = run(st)
        if r.status == "ok":
            # 外れた希望休は、ほかの外れと分けて、人と日とぶつかったルールで言い、本人との相談をすすめる
            consult, off = day_off_consult(facility, staff, rules, month, r)
            offkeys = {(b["rule"], b["key"]) for b in off}
            detail = broken_text([b for b in r.broken if (b["rule"], b["key"]) not in offkeys], {s.name for s in staff})
            return StagedResult(r, stages, note=STAGE_NOTE[st] + ("。" + detail if detail else "")
                                + ("。" + consult if consult else ""), choke=chokes)
        timed_out = timed_out or r.status == "timeout"
    return StagedResult(r, stages, message=TIMEOUT_MESSAGE if timed_out else C3_MESSAGE, choke=chokes)


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


def clip_to_month(rule: Rule, start: str, end: str) -> Rule:
    """効く期間（from・until）が月の途中で始まる・終わる時は、その日までだけに効かせる。
    適用する日は対象の表の日付で判定する（13章）。期限つきの回数は、その月の初めから終わりの日までの累計になる"""
    frm, until = rule.meta.get("from"), rule.meta.get("until")
    last = int(end[-2:])
    lo = int(frm[-2:]) if frm and start < frm <= end else 1
    hi = int(until[-2:]) if until and start <= until < end else last
    if lo == 1 and hi == last:
        return rule
    days = [d for d in (rule.when or range(1, last + 1)) if lo <= d <= hi]
    import copy
    r = copy.deepcopy(rule)
    r.when = days
    return r


def month_rules(store: Store, year: int, month: int, *, with_premise: bool = True) -> list[Rule]:
    import calendar
    last = calendar.monthrange(year, month)[1]
    ledger = store.ledger()
    start, end = f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"
    rules = [clip_to_month(r, start, end) for r in ledger.rules_at(between=(start, end)).values()]
    if with_premise:
        rules += premise_rules(store.premise(year, month))
    return rules


def build_original(store: Store, year: int, month: int, *, with_premise: bool = True) -> StagedResult:
    fac, staff = store.facility(), store.staff_on(year, month)
    m = store.month(year, month)
    rules = month_rules(store, year, month, with_premise=with_premise)
    # 一から作るので、段1を長く探してよく、組めなければ詰まり所も探す
    return solve_staged(fac, staff, rules, m, patience=patience_of(fac), explain=True)
