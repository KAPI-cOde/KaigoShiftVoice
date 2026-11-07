"""ルールの整理の見立て。その月のルールを、作成者が押すだけで決められる3つの山に分ける。

- 片付けてよい      当てはまる人や日が無いルール（名簿にいない人だけのルールを含む）
- 1つにまとめられる ほかのルールに含まれていて、無くても表を作る時に同じになるルール（後から入ったルールに置き換わった物を含む）
- ぶつかっている    今の表で守れていない運用の都合以上のルールと、詰まり所（choke.py）の芯に出てくる組

静かなルール（quiet.py）は何か月も測ってから出すので、取り込んだばかりの月には何も出ない。ここはその月のルールだけで言う。
計算で確かに言えた物は「確か」、Claude が中身を読んで見立てた物は「AI の見立て」として分けて出す。決めるのは作成者で、
片付けは見直しと同じ帳面の remove（cleanup.apply）。一覧の「片付けたルール」から戻せる。

「表を作る時に同じ」は、表を作る計算に入る場面（with_overrides の後の expand）が同じ式になり、残る方が、段のどれでも
外れる方より先には外れない、という意味。相談できるルールどうしが重なる時は、外した時の罰が二重だった分だけ変わる。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .build import impossible_wishes, month_rules
from .model import Rule, Staff
from .names import Masker
from .reader import REREAD_MODEL
from .rules import cell_value, check_table, expand, fair_on, with_overrides
from .solver import _allowed, pinned_cells, requested_cells
from .store import Store

PILES = ("remove", "merge", "conflict")
TITLES = {"remove": "片付けてよい", "merge": "1つにまとめられる", "conflict": "ぶつかっている"}
LINES = {"remove": "当てはまる人や日が無く、表を作る時に使っていないルールです",
         "merge": "同じことを決めているルールの組です。片方を片付けても、表は同じになります",
         "conflict": "同時には守れないルールの組です。どちらを緩めるかを相談してください"}
# 段ごとに外せない強さ（build.py の段の順。段1 全部守る・段2 希望を外す・段3 運用の都合を外す・段4 動かせない物だけ）
HARD_AT = {"fixed": {1, 2, 3, 4}, "operational": {1, 2}, "wish": {1, 3}, "negotiable": set()}
STRENGTH = {"fixed": "動かせない", "operational": "運用の都合", "wish": "本人の希望", "negotiable": "相談できる"}
KIND = {"coverage": "1日の人数", "count": "月の回数", "sequence": "勤務の並び", "max_run": "続けて働く日数",
        "weekday": "曜日の頼み", "cell": "日付のマス", "unit_after": "明け番とユニット", "unit_apart": "ユニットに分ける",
        "thin_even": "手薄なユニットの平均化", "layer": "表に無い当番"}
AI_LIMIT = 10      # 山ごとに出す AI の見立ての数の上限
LINE_MAX = 90


def _candidate(r: Rule) -> bool:
    """片付けの候補にできるルール。毎月のルールだけ（今月だけのルールは月が終われば効かない）。
    なるべく平等に入れる有給と手薄なユニットの平均化は場面を持たないので、当てはまる場面の有無で言えない。"""
    return (r.scope == "rule" and r.kind not in ("layer", "thin_even") and not r.meta.get("premise")
            and not r.params.get("fair"))


def _outlives(b: Rule, a: Rule) -> bool:
    """b が a の効く間ずっと効く毎月のルールか。今月だけのルールに含まれていても、来月は a だけが残る。"""
    if b.scope != "rule" or b.meta.get("premise"):
        return False
    bu, au = b.meta.get("until"), a.meta.get("until")
    return bu is None or (au is not None and bu >= au)


# ---- 計算に入る場面の比べ方

def _shape(inst, month) -> tuple:
    """場面の式の形。マスごとに、そのマスの記号が式に足す係数（1マスには記号が1つだけ入るので、これが同じなら同じ式）。
    前の月の末のマスは定数にする。"""
    cells: dict = {}
    const = 0
    for t in inst.terms:
        if t.day < 1:
            v = cell_value({}, month, t.staff, t.day)
            const += t.coef if v is not None and v in t.symbols else 0
            continue
        d = cells.setdefault((t.staff, t.day), {})
        for s in t.symbols:
            d[s] = d.get(s, 0) + t.coef
    shape = frozenset((c, frozenset((s, v) for s, v in d.items() if v)) for c, d in cells.items()
                      if any(d.values()))
    return shape, const


def _state(r: Rule) -> str:
    return json.dumps([r.kind, r.flex, r.params, r.who, r.when, r.meta], sort_keys=True, ensure_ascii=False, default=str)


class _Expander:
    """with_overrides の後のルールごとの場面。同じ中身のルールは一度だけ展開する。"""

    def __init__(self, fac, staff, month, rules: list[Rule]):
        self.fac, self.staff, self.month = fac, staff, month
        self.cache: dict = {}
        self.people = {s.name: s for s in staff}
        # マスに置ける記号。なるべく平等に入れる有給を働かせた時（一から作る時）の開け方で見る（いちばん広い）
        self.req = requested_cells(fair_on(rules), fac, staff, month)
        self.allowed: dict = {}

    def _cell(self, who: str, day: int) -> list[str] | None:
        k = (who, day)
        if k not in self.allowed:
            s = self.people.get(who)
            self.allowed[k] = _allowed(s, self.fac, self.req.get(k, set())) if s else None
        return self.allowed[k]

    def always(self, e: tuple, side: str) -> bool:
        """場面 e の上限（hi）か下限（lo）が、マスに置ける記号のどれを置いても守られるか（入れない勤務の上限など）。"""
        shape, c, lo, hi = e
        total = c
        for (who, day), coefs in shape:
            syms = self._cell(who, day)
            if syms is None:
                return False
            m = dict(coefs)
            vals = [m.get(x, 0) for x in syms] or [0]
            total += max(vals) if side == "hi" else min(vals)
        return total <= hi if side == "hi" else total >= lo

    def entries(self, r: Rule) -> tuple:
        k = (r.id, _state(r))
        if k not in self.cache:
            out = []
            for inst in expand(r, self.fac, self.staff, self.month):
                shape, const = _shape(inst, self.month)
                if shape:
                    out.append((shape, const, inst.lo, inst.hi))
            self.cache[k] = tuple(out)
        return self.cache[k]

    def universe(self, rules: list[Rule]) -> dict[str, tuple[Rule, tuple]]:
        return {r.id: (r, self.entries(r)) for r in with_overrides(rules)}


def _covers(j: Rule, i: Rule) -> bool:
    """j が、i を外せない段のどれでも外せないか（i の場面を j の場面で守れるか）。"""
    return HARD_AT[i.flex] <= HARD_AT[j.flex]


def _index(uni: dict, kinds: set[str]) -> dict:
    idx: dict = {}
    for r, ents in uni.values():
        if r.kind in kinds:
            for e in ents:
                idx.setdefault((r.kind, e[0]), []).append((r, e, len(ents)))
    return idx


def _implied(r: Rule, e: tuple, idx: dict, X: "_Expander") -> set[str] | None:
    """場面 e（ルール r）が、どう置いても守られるか、idx のどれかの場面で必ず守られるなら、守る場面のルールの id。
    守られないなら None。同じ種類・同じ式の場面だけを見る（数える勤務や範囲が違う組は、迷う組み合わせなので言わない）。
    守る場面が何件かある時は、強いルール、場面の多い（広く決めた）ルールを選ぶ（残す方として作成者に言うため）。"""
    shape, c, lo, hi = e
    out: set[str] = set()
    for side in ("hi", "lo"):
        want = hi if side == "hi" else lo
        if want is None or X.always(e, side):
            continue
        hits = [(len(HARD_AT[j.flex]), n, j.id) for j, (_s, cj, loj, hij), n in idx.get((r.kind, shape), ())
                if _covers(j, r) and (hij is not None and hij - cj <= want - c if side == "hi"
                                      else loj is not None and loj - cj >= want - c)]
        if not hits:
            return None
        out.add(max(hits, key=lambda h: (h[0], h[1]))[2])
    return out


def _redundant(a: Rule, rules: list[Rule], X: _Expander) -> set[str] | None:
    """a を外しても、表を作る計算に入る場面が同じ式で守られるか。守られるなら、a の場面を守るルールの id（a が計算に何も
    入れていなければ空）。変わるなら None。a を外すとほかのルールの名指しの譲り方が変わる時は、その変わり方も確かめる。"""
    full = X.universe(rules)
    rest = X.universe([r for r in rules if r.id != a.id])
    lost, gained = [], []
    for rid, (r, ents) in full.items():
        if rid == a.id:
            lost += [(r, e) for e in ents]
            continue
        r2, ents2 = rest[rid]
        if ents2 is ents:
            continue
        c1, c2 = Counter(ents), Counter(ents2)
        lost += [(r, e) for e in (c1 - c2).elements()]
        gained += [(r2, e) for e in (c2 - c1).elements()]
    if not lost and not gained:
        return set()
    kinds = {r.kind for r, _e in lost + gained}
    keep: set[str] = set()
    idx_rest = _index(rest, kinds)
    for r, e in lost:
        got = _implied(r, e, idx_rest, X)
        if got is None:
            return None
        if r.id == a.id:
            keep |= got
    idx_full = _index(full, kinds)
    for r, e in gained:
        if _implied(r, e, idx_full, X) is None:
            return None
    return keep


def _same_cells(a: Rule, rules: list[Rule], fac, staff, month) -> bool:
    """a を外しても、頼まれた時だけ置く記号を開けるマスと、動かせないマスが変わらないか。"""
    rest = [r for r in rules if r.id != a.id]
    if requested_cells(rules, fac, staff, month) != requested_cells(rest, fac, staff, month):
        return False
    return all(pinned_cells(rules, f) == pinned_cells(rest, f)
               for f in ({"fixed", "wish", "operational"}, {"fixed", "operational"}, {"fixed", "wish"}, {"fixed"}))


def _check(a: Rule, rules: list[Rule], X: _Expander, fac, staff, month) -> set[str] | None:
    """毎月のルールだけで見ても、その月のルール全部で見ても、a を外して同じになるか。毎月のルールだけで見るのは、今月だけの
    ルールに守られて今月だけ要らない物を、片付けの候補にしないため。守るルールは毎月のルールだけで見た時の物を返す。"""
    long = [r for r in rules if r.id == a.id or _outlives(r, a)]
    keep = _redundant(a, long, X)
    if keep is None or _redundant(a, rules, X) is None or not _same_cells(a, rules, fac, staff, month):
        return None
    return keep


def _overrider(a: Rule, rules: list[Rule], X: _Expander) -> str | None:
    """a が計算に何も入れていない時、a に代わったルール（それを外すと a が計算に戻る、後から入った毎月のルール）。"""
    long = [r for r in rules if r.id == a.id or _outlives(r, a)]
    for b in reversed(long):
        if b.id == a.id or b.kind != a.kind:
            continue
        uni = X.universe([r for r in long if r.id != b.id])
        if uni.get(a.id, (None, ()))[1]:
            return b.id
    return None


def contained(rules: list[Rule], fac, staff, month, skip: set[str] = frozenset()) -> list[dict]:
    """ほかのルールに含まれている毎月のルール。1件は {"drop": id, "keep": [id], "what": 種類}。種類は contained（含まれている）・
    replaced（後から入ったルールに置き換わっている）・always（どう置いても守られる。keep は空）。
    弱いルール・狭く決めたルールから順に見て、片付けの候補にした物を除いた残りで次を確かめる。同じことを決めた2件は
    片方だけになる（両方を片付けると決めが消える）。どれも1件だけ片付けても同じになることを確かめてある。"""
    X = _Expander(fac, staff, month, rules)
    size = {rid: len(ents) for rid, (_r, ents) in X.universe(rules).items()}
    order = sorted((a for a in rules if _candidate(a) and a.id not in skip),
                   key=lambda a: (len(HARD_AT[a.flex]), size.get(a.id, 0)))
    cur = list(rules)
    out: list[dict] = []
    for a in order:
        keep = _check(a, rules, X, fac, staff, month)
        if keep is not None and out:
            keep = _check(a, cur, X, fac, staff, month)
        if keep is None:
            continue
        what = "contained"
        if not keep and X.universe(cur)[a.id][1]:
            what = "always"
        elif not keep:
            b = _overrider(a, cur, X)
            if b is None:
                continue
            keep, what = {b}, "replaced"
        out.append({"drop": a.id, "keep": sorted(keep), "what": what})
        cur = [r for r in cur if r.id != a.id]
    # 残す方が、後で片付けの候補になった時は、それを守るルールへ付け替える（後の確かめは前に除いた物の無い所でしているので、
    # 守る関係はそのままつながる）
    dropped = {x["drop"]: x for x in out}

    def resolve(k: str) -> list[str]:
        if k not in dropped:
            return [k]
        return [z for kk in dropped[k]["keep"] for z in resolve(kk)]

    for x in out:
        x["keep"] = list(dict.fromkeys(z for k in x["keep"] for z in resolve(k)))
    return out


# ---- 名簿にいない人

def ghost_names(store: Store, roster: set[str]) -> list[str]:
    """帳面・表・引き受けの記録に出てくる、今の名簿にいない人の名前（辞めた人・外した人）。"""
    seen: dict[str, None] = {}
    for t in store.ledger().txs:
        rule = t.get("rule") if isinstance(t.get("rule"), dict) else {}
        for w in (rule.get("who") or []) + (rule.get("staff") or []):
            if isinstance(w, str):
                seen[w] = None
    for m in store.months():
        for w in list((m.table or {}).keys()) + list(m.prev_tail.keys()):
            seen[w] = None
    for a in store.acceptances():
        seen[a.staff] = None
    return [w for w in seen if w and w not in roster]


def _masker(staff: list[Staff], ghosts: list[str], fac) -> Masker:
    unit = fac.units[0] if fac.units else ""
    return Masker(list(staff) + [Staff(name=g, unit=unit, can=[]) for g in ghosts], fac)


# ---- 機械の数え

def collect(store: Store, year: int, month: int) -> dict:
    """計算で確かに言える物と、Claude へ送る伏せた材料。"""
    fac, staff, m = store.facility(), store.staff_on(year, month), store.month(year, month)
    rules = month_rules(store, year, month, with_premise=False)
    by_id = {r.id: r for r in rules}
    roster = {s.name for s in staff}
    ghosts = ghost_names(store, roster)
    masker = _masker(staff, ghosts, fac)
    ghost_codes = {masker.to_code[g] for g in ghosts}
    remove, merge, conflict, stale = [], [], [], []
    gone_ids: set[str] = set()
    for r in rules:
        if not _candidate(r):
            continue
        if not any(i.terms for i in expand(r, fac, staff, m)):
            gone = [w for w in (r.who or []) if w not in roster]
            if r.who and len(gone) == len(r.who):
                remove.append({"ids": [r.id], "what": "absent", "who": gone})
            else:
                remove.append({"ids": [r.id], "what": "no_scene"})
            gone_ids.add(r.id)
            continue
        masked, unknown = masker.mask(r.name)
        named = [masker.to_name[c] for c in sorted(set(re.findall(rf"職員\d{{{masker.width}}}", masked)) & ghost_codes)]
        named += [u for u in unknown if u not in masker.leftover and u not in named]
        named += [w for w in (r.who or []) if w not in roster and w not in named]
        if named:
            stale.append({"id": r.id, "who": named})
    same: dict[str, str] = {}
    for x in contained(rules, fac, staff, m, skip=gone_ids):
        if x["what"] == "always":
            remove.append({"ids": [x["drop"]], "what": "always"})
            continue
        merge.append({"keep": x["keep"], "drop": [x["drop"]], "what": x["what"]})
        if x["what"] == "contained" and len(x["keep"]) == 1:
            same[x["drop"]] = x["keep"][0]
    # ぶつかっている。今の表で守れていない運用の都合以上のルールと、詰まり所の芯の組
    places: Counter = Counter()
    if m.table:
        for b in check_table(rules, fac, staff, m, m.table):
            if b["flex"] in ("operational", "wish", "fixed"):
                places[same.get(b["rule"], b["rule"])] += 1
    from .choke import chokepoints
    cores: dict[frozenset, dict] = {}
    try:
        chk, _notes = impossible_wishes(fac, rules, m)
        for c in chokepoints(fac, staff, chk, m, limit=10):
            # 含まれているルールの場面は、残す方の場面と同じ式なので、残す方の名前で言う
            ids = list(dict.fromkeys(same.get(i, i) for i in c.get("rule_ids", []) if i in by_id))
            if len(ids) >= 2:
                cores.setdefault(frozenset(ids), {"ids": ids, "what": "choke", "day": c["day"],
                                                  "wishes": len(c["wishes"])})
    except Exception:   # 詰まり所を探せなくても、ほかの山は出す
        cores = {}
    for g in cores.values():
        g["places"] = sum(places.get(i, 0) for i in g["ids"])
        conflict.append(g)
    in_core = {i for g in cores.values() for i in g["ids"]}
    for rid, n in places.items():
        if rid not in in_core and rid in by_id:
            conflict.append({"ids": [rid], "what": "broken", "places": n})
    found = {"remove": remove, "merge": merge, "conflict": conflict}
    k = 0
    for p in PILES:
        for x in found[p]:
            k += 1
            x["key"] = f"m{k}"
    return {"year": year, "month": month, "rules": rules, "by_id": by_id, "found": found, "stale": stale,
            "masker": masker, "fac": fac,
            "roster": [{"id": masker.to_code[s.name], "unit": masker.unit_code.get(s.unit, s.unit), "can": s.can}
                       for s in staff],
            "total": sum(1 for r in rules if r.scope == "rule" and not r.meta.get("premise"))}


# ---- Claude へ送る材料

def _period(r: Rule) -> str:
    if r.scope != "rule":
        return "今月だけ"
    u = r.meta.get("until")
    return f"毎月（{int(u[5:7])}月{int(u[8:10])}日まで）" if u else "毎月"


def _note(x: dict, pile: str, by_id: dict, code) -> str:
    """計算で言えたことの短い文（Claude へ送る時は人を番号にする）。"""
    nm = lambda i: f"{i}「{by_id[i].name}」"
    if x["what"] == "absent":
        return f"名簿にいない{'・'.join(code(w) for w in x['who'])}さんだけのルールで、当てはまる日が無い"
    if x["what"] == "no_scene":
        return "当てはまる人や日が無い"
    if x["what"] == "always":
        return "入れる勤務などから、どの表でも必ず守られる（表を作る時に効いていない）"
    if x["what"] == "contained":
        return f"{nm(x['drop'][0])}は{'・'.join(nm(i) for i in x['keep'])}に含まれ、無くても表は同じ"
    if x["what"] == "replaced":
        return f"{nm(x['drop'][0])}は後から入った{nm(x['keep'][0])}に置き換わり、表を作る時に使っていない"
    if x["what"] == "choke":
        return (f"{x['day']}日を組む時に、{'と'.join(nm(i) for i in x['ids'][:4])}を同時には守れない"
                + (f"（希望休{x['wishes']}件も重なる）" if x.get("wishes") else ""))
    return f"{nm(x['ids'][0])}が今の表で{x['places']}か所守れていない"


def _unit_codes(params: dict, mk: Masker) -> dict:
    """中身のユニットの名前を番号にする（JSON の中は区切りが無く、文の伏せ方では当たらないため）。"""
    p = json.loads(json.dumps(params, ensure_ascii=False))
    if isinstance(p.get("units"), list):
        p["units"] = [mk.unit_code.get(u, u) for u in p["units"]]
    seen = p.get("seen")
    if isinstance(seen, dict) and isinstance(seen.get("units"), dict):
        seen["units"] = {mk.unit_code.get(u, u): v for u, v in seen["units"].items()}
    return p


def payload(base: dict) -> dict | None:
    """Claude へ送る材料。人は職員NN、ユニットはユニットN。名前を伏せきれないルールは名前と中身を送らない。
    それでも名前が残る時は None（送らない）。"""
    mk: Masker = base["masker"]
    staff_names = [n for n in list(mk.to_code) + [s.call for s in mk.staff if s.call] if n and len(n) >= 2]

    def code(w: str) -> str:
        return mk.to_code.get(w, "名簿外")

    def masked(text: str) -> str | None:
        t, unknown = mk.mask(text)
        return None if unknown or mk.leftover else t

    rules = []
    for r in base["rules"]:
        detail = masked(json.dumps(_unit_codes(r.params, mk), ensure_ascii=False))
        rules.append({"id": r.id, "name": masked(r.name), "kind": KIND.get(r.kind, r.kind),
                      "strength": STRENGTH.get(r.flex, r.flex), "period": _period(r),
                      "who": [code(w) for w in r.who] if r.who else "全員", "days": r.when,
                      "detail": json.loads(detail) if detail else None,
                      "reason": masked(r.meta["reason"]) if r.meta.get("reason") else None})
    found = {}
    for p in PILES:
        for x in base["found"][p]:
            note = masked(_note(x, p, base["by_id"], code)) or "（名前を伏せきれないので中身は送りません）"
            found[x["key"]] = {"pile": p, "note": note,
                               **({"keep": x["keep"], "drop": x["drop"]} if p == "merge" else {"ids": x["ids"]})}
    gone = sorted({code(w) for s in base["stale"] for w in s["who"]})
    out = {"month": f"{base['year']:04d}-{base['month']:02d}", "roster": base["roster"],
           "not_in_roster": gone, "units": list(mk.unit_code.values()), "rules": rules, "found": found}
    text = json.dumps(out, ensure_ascii=False)
    leak = [n for n in staff_names if n and n in text]
    if leak:
        for r in out["rules"]:
            blob = json.dumps(r, ensure_ascii=False)
            if any(n in blob for n in leak):
                r["name"], r["detail"], r["reason"] = None, None, None
        text = json.dumps(out, ensure_ascii=False)
        if any(n in text for n in leak):
            return None
    return out


SYSTEM = """あなたは介護施設の勤務表アプリの整理係です。施設でシフトを作る人（作成者）が「ルールが多くて、何が要るのか分からない」と困っています。
今月のルールの一覧（rules）と、手元の計算で確かに言えたこと（found）を渡します。人は「職員NN」、ユニットは「ユニットN」と伏せてあります。名簿にいる人は roster の番号だけで（unit はユニット、can は入れる勤務の記号）、not_in_roster は名簿にいない人です。

ルールを3つの山に分け、作成者が押すだけで決められるようにします。決めるのは作成者で、あなたは提案だけをします。
- remove 片付けてよい。それだけで要らないルール（当てはまる人や日が無い、事情が終わったと読み取れる など）。ほかのルールがあるから要らない物は remove に入れず merge に入れる
- merge 1つにまとめられる。keep の1件を守れば drop のルールも必ず守られる組。重なるだけの組や、ほかのルールと合わせて初めて守られる組は入れない
- conflict ぶつかっている。同時には守れない組。why には、どちらを緩めるかの相談を1行で書く

返事は JSON だけ。形
{"lines": {"remove": "山の説明1行", "merge": "1行", "conflict": "1行"},
 "reasons": {"m1": "その項目の理由1行", "m2": "..."},
 "ai": {"remove": [{"id": "r12", "why": "1行", "by": []}],
        "merge": [{"keep": "r3", "drop": ["r7"], "why": "1行"}],
        "conflict": [{"ids": ["r3", "r9"], "why": "1行"}]}}

守ること
- found の項目は計算で確かに言えたことです。山は変えず、reasons に、作成者に伝わる理由を1行ずつ書きます。note の事実を言い換えるだけで、事実を足しません
- ai には、ルールの中身を読んで、あなたが見立てた物だけを入れます（例 同じことを別の言い方で決めている2件、言い直しで古くなった方、ぶつかって見える2件）。found にある組と同じ物は入れません。迷う物は入れません。無ければ空の並びにします
- remove の by は、理由にほかのルールを挙げた時のそのルールの id の並び（無ければ空）
- 片付ける（remove の id、merge の drop）のは period が「毎月」で始まるルールだけです。今月だけのルールは月が終われば効かないので、片付けに入れません
- 強さ（strength）が違う2件をまとめる時は、強い方を残します。弱い方を残して強い方を片付けません
ルールの効き方（ai を見立てる時は、これで確かめる）
- 人を名指しした月の回数は、同じ勤務の全員向けの回数より優先し、その人には名指しの数が効く（名指しの数の方が広い時も）
- ユニットを名指しした人数は、全ユニット向けの人数より優先する。同じ人の同じ勤務の回数が2件あれば、後から入った方が効く
- detail の capable_only は、その勤務に入れる人（roster の can にその記号がある人）だけを数える。人は can に無い勤務には入らない
- 強さは「動かせない」がいちばん強く、「相談できる」がいちばん弱い。表が作れない時は弱い方から外す
- ai に入れるのは、上の効き方で確かめて外しても表が変わらないと言える物か、事情が終わったとルールの文から読める物だけです。why には、なぜ必ず守られるのかを書きます

- 1行は40字ほどまで。作成者はプログラムの言葉が分かりません。r12 のような番号、英語、記号の「：」は書かず、ルールは「」に入れた名前で言います。「札」「決まり」とは言わず「ルール」と言います。人は職員NNのまま書きます
"""


class TidyError(Exception):
    """Claude の見立てを受け取れなかった（通信・途中で切れた・形が違う）。計算で言える物だけを出す。"""


def message(p: dict) -> str:
    return "整理の材料 " + json.dumps(p, ensure_ascii=False)


def digest(p: dict) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


def parse_tidy(text: str) -> dict:
    """返事の形を確かめる。考えた後の Sonnet は JSON を ```json の囲みに入れることがあるので、あれば最後の囲みを読む。"""
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fences:
        text = fences[-1]
    mm = re.search(r"\{.*\}", text, re.S)
    if not mm:
        raise TidyError("返事に JSON がない")
    try:
        obj = json.loads(mm.group(0))
    except json.JSONDecodeError as e:
        raise TidyError(f"返事の JSON が読めない {e}") from e
    if not isinstance(obj, dict):
        raise TidyError("返事の形が違う")
    lines = obj.get("lines") if isinstance(obj.get("lines"), dict) else {}
    reasons = obj.get("reasons") if isinstance(obj.get("reasons"), dict) else {}
    ai = obj.get("ai") if isinstance(obj.get("ai"), dict) else {}
    return {"lines": {k: v for k, v in lines.items() if k in PILES and isinstance(v, str)},
            "reasons": {k: v for k, v in reasons.items() if isinstance(k, str) and isinstance(v, str)},
            "ai": {p: [x for x in ai.get(p) or [] if isinstance(x, dict)] if isinstance(ai.get(p), list) else []
                   for p in PILES}}


@dataclass
class Sent:
    system: str
    payload: dict


class RecordedTidier:
    """録った返事で答える。キーは送る材料の digest。無い材料は TidyError（呼べない時と同じ扱い）。"""

    def __init__(self, replies: dict[str, dict]):
        self.replies = replies
        self.sent: list[Sent] = []

    @classmethod
    def from_file(cls, path: Path) -> "RecordedTidier":
        p = Path(path)
        return cls(json.loads(p.read_text(encoding="utf-8")) if p.exists() else {})

    def ask(self, p: dict) -> dict:
        self.sent.append(Sent(SYSTEM, p))
        hit = self.replies.get(digest(p))
        if hit is None:
            raise TidyError("録った返事がない材料")
        return parse_tidy(hit["reply"])


@dataclass
class SonnetTidier:
    """本物の Sonnet（読み直しと同じモデル・考えるのは少なめ）。鍵は SDK が環境変数から読む。"""
    model: str = REREAD_MODEL
    timeout: float = 120.0
    max_tokens: int = 8000
    record_to: Path | None = None
    sent: list[Sent] = field(default_factory=list)
    last_usage: dict | None = None

    def ask(self, p: dict) -> dict:
        import anthropic
        self.sent.append(Sent(SYSTEM, p))
        try:
            client = anthropic.Anthropic(timeout=self.timeout, max_retries=1)
            resp = client.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": message(p)}],
                thinking={"type": "adaptive"}, output_config={"effort": "low"},
            )
        except anthropic.APIError as e:
            raise TidyError(f"整理の見立てを呼べませんでした（{type(e).__name__}）") from e
        self.last_usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
        if resp.stop_reason != "end_turn":
            raise TidyError(f"返事が途中で止まりました（{resp.stop_reason}）")
        text = "".join(b.text for b in resp.content if b.type == "text")
        got = parse_tidy(text)
        if self.record_to:
            path = Path(self.record_to)
            rec = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            rec[digest(p)] = {"sent": p, "reply": text}
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return got


# ---- 画面へ出す形

def _clean(s, mk: Masker, by_id: dict) -> str | None:
    """Claude の1行を、画面に出せる形にする（名前を戻す・番号をルールの名前に・コロンと「札」「決まり」を使わない）。"""
    if not isinstance(s, str):
        return None
    t = " ".join(s.split())
    t = re.sub(r"(?<![A-Za-z0-9])(r\d{2,4})(?![0-9])", lambda m: f"「{by_id[m.group(1)].name}」" if m.group(1) in by_id else m.group(1), t)
    t = mk.unmask(t)
    t = re.sub(r"\s*[:：]\s*", "、", t).replace("決まり", "ルール").replace("札", "ルール")
    t = t.strip("、 ")
    if not t:
        return None
    return t if len(t) <= LINE_MAX else t[:LINE_MAX - 1] + "…"


def _local_why(x: dict, by_id: dict) -> str:
    nm = lambda i: f"「{by_id[i].name}」"
    if x["what"] == "absent":
        return f"名簿にいない{'・'.join(x['who'])}さんだけのルールです"
    if x["what"] == "no_scene":
        return "当てはまる人や日がありません"
    if x["what"] == "always":
        return "入れる勤務などから、どの表でもいつも守られています"
    if x["what"] == "contained":
        return f"{'・'.join(nm(i) for i in x['keep'])}に含まれていて、無くても表は同じです"
    if x["what"] == "replaced":
        return f"後から入った{nm(x['keep'][0])}に置き換わっていて、表を作る時に使っていません"
    if x["what"] == "choke":
        return f"{x['day']}日に同時には守れません。どちらを緩めるかを相談してください"
    return f"今の表で{x['places']}か所守れていません。ほかのルールとの兼ね合いを相談してください"


def finish(base: dict, ai=None) -> dict:
    """計算の見立てに、Claude の見立て（あれば）を合わせて、画面へ出す3つの山にする。"""
    by_id, mk = base["by_id"], base["masker"]
    found = base["found"]
    got, ai_state = None, "off"
    if ai is not None and os.environ.get("SHIFTVOICE_PUBLIC") != "1":
        p = payload(base)
        if p is None:
            ai_state = "skipped"
        else:
            try:
                got = ai.ask(p)
                ai_state = "on"
            except TidyError:
                ai_state = "error"
            except Exception:   # 想定外でも、計算で言える物は出す
                ai_state = "error"
    reasons = (got or {}).get("reasons", {})
    lines = (got or {}).get("lines", {})
    monthly = {i for i, r in by_id.items() if _candidate(r)}
    taken = {i for x in found["remove"] for i in x["ids"]} | {i for x in found["merge"] for i in x["drop"]}
    keeps = {i for x in found["merge"] for i in x["keep"]}
    names = lambda ids: [by_id[i].name for i in ids]
    piles = {p: [] for p in PILES}
    for p in PILES:
        for x in found[p]:
            why = _clean(reasons.get(x["key"]), mk, by_id) or _local_why(x, by_id)
            item = {"basis": "sure", "why": why}
            if p == "merge":
                item.update(keep=x["keep"], keep_names=names(x["keep"]), drop=x["drop"], drop_names=names(x["drop"]))
            else:
                item.update(ids=x["ids"], names=names(x["ids"]))
            piles[p].append(item)
    if got:
        seen_conf = {frozenset(x["ids"]) for x in found["conflict"]}
        merges = list(got["ai"]["merge"][:AI_LIMIT])
        for x in got["ai"]["remove"][:AI_LIMIT]:
            i, why = x.get("id"), _clean(x.get("why"), mk, by_id)
            by = [b for b in x.get("by") or [] if isinstance(b, str) and b in by_id and b != i] \
                if isinstance(x.get("by"), list) else []
            if by:   # ほかのルールがあるから要らない、という見立ては、まとめる山で残す方と強さを確かめる
                merges.append({"keep": by[0], "drop": [i], "why": x.get("why")})
            elif i in monthly and i not in taken and i not in keeps and why:
                taken.add(i)
                piles["remove"].append({"basis": "ai", "why": why, "ids": [i], "names": names([i])})
        for x in merges:
            k, drop, why = x.get("keep"), x.get("drop"), _clean(x.get("why"), mk, by_id)
            drop = [drop] if isinstance(drop, str) else drop
            if not (isinstance(k, str) and k in by_id and k not in taken and isinstance(drop, list) and drop and why):
                continue
            drop = list(dict.fromkeys(d for d in drop if isinstance(d, str)))
            if any(d not in monthly or d in taken or d in keeps or d == k for d in drop):
                continue
            if not all(_covers(by_id[k], by_id[d]) for d in drop):
                continue   # 残す方が、片付ける方を外せない段のどれかで外れる組（強い方を片付ける組など）は出さない
            taken |= set(drop)
            keeps.add(k)
            piles["merge"].append({"basis": "ai", "why": why, "keep": [k], "keep_names": names([k]),
                                   "drop": drop, "drop_names": names(drop)})
        for x in got["ai"]["conflict"][:AI_LIMIT]:
            ids, why = x.get("ids"), _clean(x.get("why"), mk, by_id)
            if not (isinstance(ids, list) and why):
                continue
            ids = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i in by_id))
            if len(ids) < 2 or frozenset(ids) in seen_conf:
                continue
            seen_conf.add(frozenset(ids))
            piles["conflict"].append({"basis": "ai", "why": why, "ids": ids, "names": names(ids)})
    count = lambda p: sum(len(x.get("drop") or x.get("ids")) for x in piles[p]) if p != "conflict" else len(piles[p])
    out_piles = [{"pile": p, "title": TITLES[p], "count": count(p),
                  "line": _clean(lines.get(p), mk, by_id) or LINES[p], "items": piles[p]} for p in PILES]
    total = base["total"]
    n = sum(x["count"] for x in out_piles)
    how = "、AI と計算で" if ai_state == "on" else ""
    head = (f"ルールは{total}件。見直すとよい物を{how}分けました。押した物だけを片付けます。"
            "片付けた物は、一覧の「片付けたルール」から戻せます") if n else \
        f"ルールは{total}件。いま片付けやまとめの候補は見つかりませんでした"
    note = {"off": "AI を使わずに、計算で確かに言える物だけを出しています",
            "skipped": "名前を伏せきれないルールがあったので、AI には送らず、計算で確かに言える物だけを出しています",
            "error": "AI の見立てを受け取れなかったので、計算で確かに言える物だけを出しています"}.get(ai_state)
    stale = [{"id": s["id"], "name": by_id[s["id"]].name, "who": s["who"]} for s in base["stale"]
             if s["id"] not in taken]
    return {"total": total, "head": head, "ai": ai_state, "ai_note": note, "piles": out_piles, "stale": stale}


def view(store: Store, year: int, month: int, ai=None) -> dict:
    return finish(collect(store, year, month), ai)
