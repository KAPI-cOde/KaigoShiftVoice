"""静かなルール（ルールが増えて把握できなくなるのを防ぐ。docs/rule-cleanup.md）。

月の表を作った後に、毎月のルールを1件ずつ外して、白紙から作り直す。外しても作り直した表がそのルールどおり
だったら、その月は「静か」と数える。何か月も続けて静かなルールは、毎月の見直しで片付けの候補として1件ずつ出す。
自動では消さない（決めるのは作成者）。

測っているのは「このルールが無くても、表はこのルールどおりだった」ことだけで、「要らない」とは言わない。
来月は効くかもしれないので、候補に出す時は何か月続いたかと、片付けても履歴から戻せることを添える。

- 当てはまる場面が1つも無いルール（辞めた人・その月に無い曜日など）は、作り直さずに静かと数える
- 今月だけの頼み・当番（待機など）・撮影用の前提は数えない
- 作り直しは1件ずつ別のプロセスで並べて解く（ソルバーは1件あたり1〜17秒。ひだまりの12件で測った）
"""
from __future__ import annotations

import calendar
import datetime as dt
import os
import time
from concurrent.futures import ProcessPoolExecutor

from .build import month_rules
from .rules import check_table, expand
from .build import solve_staged
from .solver import large_workers
from .store import Store, atomic_write_json, read_json

QUIET_MONTHS = 3   # 何か月続けて静かなら見直しに出すか（施設の設定 review.quiet_months で変えられる）


def _path(store: Store, year: int, month: int):
    return store.root / "quiet" / f"{year:04d}-{month:02d}.json"


def candidates(store: Store, year: int, month: int) -> list[str]:
    """静かかどうかを数える札の id。毎月のルールで、当番と今月だけの頼みを除く。なるべく平等に入れる有給と手薄なユニットの平均化は場面を持たないので
    数えない（数えると、3か月後に「今の名簿にいない人のルール」と言われ、片付けをすすめられた）。"""
    last = calendar.monthrange(year, month)[1]
    span = (f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}")
    return [rid for rid, r in store.ledger().rules_at(between=span).items()
            if r.scope == "rule" and r.kind != "layer" and not r.meta.get("premise") and not r.params.get("fair")
            and r.kind != "thin_even"]


QUIET_DTIME = 5.0   # 作り直し1件の計算の量（表を作る時の20より小さく。静かかどうかを見るだけなので）


def _one(args):
    fac, staff, rules, month, rid, dtime = args
    target = [r for r in rules if r.id == rid]
    rest = [r for r in rules if r.id != rid]
    t = time.time()
    res = solve_staged(fac, staff, rest, month, max_dtime=dtime).result   # 表を作る時と同じ段の順で解く
    if res.status != "ok":
        return rid, {"state": "unknown", "seconds": round(time.time() - t, 2)}
    broken = check_table(target, fac, staff, month, res.table)
    return rid, {"state": "needed" if broken else "quiet", "broken": len(broken),
                 "seconds": round(time.time() - t, 2)}


def _joint(out: dict, fac, staff, rules, m, dtime: float) -> None:
    """1件ずつでは静かでも、同じことを決めたルールが2件あると、互いに守り合って両方とも静かに見える
    （「早番の人数」と「月曜の早番は1人」など。両方片付けると決まりが消える）。
    静かな札をまとめて外して作り直し、破れた札は「重なり」（ほかのルールと同じことを決めている）に移す。
    残った札は一緒に片付けても表がそれどおりになる組。破れる札が無くなるまで繰り返す。"""
    s = {rid for rid, v in out.items() if v["state"] == "quiet" and not v.get("no_scene")}
    for _ in range(6):
        if not s:
            return
        res = solve_staged(fac, staff, [r for r in rules if r.id not in s], m, max_dtime=dtime).result
        if res.status != "ok":
            for rid in s:
                out[rid] = {**out[rid], "state": "unknown"}
            return
        hit = {b["rule"] for b in check_table([r for r in rules if r.id in s], fac, staff, m, res.table)}
        if not hit:
            return
        for rid in hit:
            out[rid] = {**out[rid], "state": "overlap"}
        s -= hit


def _procs(fac, staff, workers: int | None) -> int:
    """裏で並べるプロセスの数。指定（引数・施設の設定 quiet.workers）が無ければ CPU の数から決める。表の計算で
    作業者を複数回す月（職員が30人を超える月。solver.large_workers）は、その数で割り、プロセス×作業者が
    CPU の数を大きく超えないようにする（8プロセス×8作業者で、事務用の PC が重くならないように。2026-11-05）。"""
    if workers:
        return workers
    if fac.settings.get("quiet", {}).get("workers"):
        return fac.settings["quiet"]["workers"]
    n = max(1, min(8, (os.cpu_count() or 2) - 2))
    return max(1, n // max(1, large_workers(fac, staff)))


def check_month(store: Store, year: int, month: int, *, workers: int | None = None,
                only: list[str] | None = None) -> dict:
    """その月の毎月のルールを1件ずつ外して作り直し、結果を quiet/YYYY-MM.json に残す。"""
    fac = store.facility()
    staff = store.staff_on(year, month)
    m = store.month(year, month)
    rules = month_rules(store, year, month)
    ids = only or candidates(store, year, month)
    out: dict[str, dict] = {}
    todo = []
    dtime = float(fac.settings.get("quiet", {}).get("max_deterministic_time", QUIET_DTIME))
    for rid in ids:
        r = next((x for x in rules if x.id == rid), None)
        if r is None:
            continue
        if not any(i.terms for i in expand(r, fac, staff, m)):
            out[rid] = {"state": "quiet", "no_scene": True, "seconds": 0.0}   # 当てはまる場面が無い
        else:
            todo.append((fac, staff, rules, m, rid, dtime))
    t = time.time()
    n = _procs(fac, staff, workers)
    if todo:
        # 1つずつでよい時はこのプロセスで回す。作業者を複数回す月は、1つでも別のプロセスにして優先度を下げる
        # （サーバーの中で回すと、画面の一言の直しと同じ優先度で CPU を取り合うため）
        if n == 1 and not large_workers(fac, staff):
            results = map(_one, todo)
        else:
            nice = getattr(os, "nice", None)   # Windows には無い（無ければ優先度を下げずに回す）
            ex = ProcessPoolExecutor(max_workers=n, **({"initializer": nice, "initargs": (10,)} if nice else {}))
            results = ex.map(_one, todo)   # 画面の操作を優先（nice 10）
        for rid, v in results:
            out[rid] = v
        if n != 1:
            ex.shutdown()
    _joint(out, fac, staff, rules, m, dtime)
    data = {"month": f"{year:04d}-{month:02d}", "at": dt.datetime.now().isoformat(timespec="seconds"),
            "seconds": round(time.time() - t, 1), "rules": out}
    atomic_write_json(_path(store, year, month), data)
    return data


def _prev(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def no_scene_ids(store: Store, year: int, month: int) -> set[str]:
    """前の月に、当てはまる場面が1つも無かった札（表にいない人のルールなど）。見直しで先に出す（迷わず決められる）。"""
    y, m = _prev(year, month)
    last = read_json(_path(store, y, m)) or {"rules": {}}
    return {rid for rid, v in last["rules"].items() if v.get("no_scene")}


def overlap_streaks(store: Store, year: int, month: int) -> dict[str, int]:
    """前の月に「重なり」だった札の、続けて静か・重なりだった月の数。重なりの札は1件ずつ外せば表はそれどおり
    （片方は外せる）が、同じことを決める相手と一緒には外せない。"""
    y, m = _prev(year, month)
    last = read_json(_path(store, y, m))
    if not last:
        return {}
    lap = {rid for rid, v in last["rules"].items() if v.get("state") == "overlap"}
    return {rid: n for rid, n in streaks(store, year, month, ("quiet", "overlap")).items() if rid in lap}


def streaks(store: Store, year: int, month: int, states: tuple[str, ...] = ("quiet",)) -> dict[str, int]:
    """対象の月（year, month）の前の月までさかのぼって、続けて静かだった月の数（札ごと）。
    記録の無い月で数えるのを止める（測っていない月を静かとは数えない）。"""
    out: dict[str, int] = {}
    alive: set[str] | None = None
    y, m = _prev(year, month)
    for _ in range(24):
        data = read_json(_path(store, y, m))
        if not data:
            break
        quiet = {rid for rid, v in data["rules"].items() if v.get("state") in states}
        alive = quiet if alive is None else alive & quiet
        if not alive:
            break
        for rid in alive:
            out[rid] = out.get(rid, 0) + 1
        y, m = _prev(y, m)
    return out


def quiet_months_setting(store: Store) -> int:
    return int(store.facility().settings.get("review", {}).get("quiet_months", QUIET_MONTHS))


def quiet_per_month_setting(store: Store) -> int:
    from .review import QUIET_PER_MONTH
    return int(store.facility().settings.get("review", {}).get("quiet_per_month", QUIET_PER_MONTH))
