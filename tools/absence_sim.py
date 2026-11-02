"""待機が無い時、土日の急な休みの穴埋めが誰に集まるかの試算（画面5・台本 v3 の1分10秒）。

架空の施設ひだまりの郷の11月の元の表で、土日に早番・日勤・遅番・夜勤に入っている人が1人ずつ急に休んだ
全部の場合を、1件ずつ別々に試す。待機の層が無いので、アプリの狭い範囲の直し（resolve）がそのまま
直し方を選ぶ。その日に休みだった人が勤務に入ったら、その人が穴埋めの電話を受けたと数える。
Claude は呼ばない。

    uv run python tools/absence_sim.py            # 結果を data/hidamari/absence-sim-2026-11.json に書く
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice.model import Rule  # noqa: E402
from shiftvoice.resolve import resolve  # noqa: E402
from shiftvoice.session import Session  # noqa: E402
from shiftvoice.build import month_rules  # noqa: E402
from shiftvoice.store import Store, atomic_write_json  # noqa: E402

YEAR, MONTH = 2026, 11
OUT = ROOT / "data" / "hidamari" / f"absence-sim-{YEAR}-{MONTH:02d}.json"


def simulate(store: Store) -> dict:
    sess = Session(store, YEAR, MONTH, reader=None)
    sess.open()                                   # 元の表が無ければ作る（デモの起動と同じ）
    m = store.month(YEAR, MONTH)
    fac, staff = store.facility(), store.staff_on(YEAR, MONTH)
    rules = month_rules(store, YEAR, MONTH, with_premise=False)
    kinds = {st.symbol: st.kind for st in fac.shift_types}
    rest = {s for s, k in kinds.items() if k == "rest"}
    days = [d for d in range(1, m.days + 1) if m.weekday(d) in ("sat", "sun")]
    calls: Counter[str] = Counter()
    cases, failed, inside = 0, 0, 0
    t0 = time.perf_counter()
    for d in days:
        for s in staff:
            cell = m.table[s.name][d - 1]
            if cell not in ("早", "日", "遅", "夜"):
                continue
            cases += 1
            off = Rule(id="t-sim", ver=1, name=f"{s.name}さん {d}日 休み", kind="cell", flex="fixed",
                       params={"in": ["公"]}, who=[s.name], when=[d], scope="once")
            rr = resolve(fac, staff, rules + [off], m, m.table, [off])
            if not rr.ok:
                failed += 1
                continue
            helpers = [o.name for o in staff if o.name != s.name
                       and m.table[o.name][d - 1] in rest and rr.table[o.name][d - 1] not in rest]
            if not helpers:
                inside += 1                     # その日に勤務の人どうしの入れ替えで埋まった（休みの人に電話が要らない）
            calls.update(helpers)
    top = calls.most_common()
    return {
        "period": f"{YEAR}年{MONTH}月の土日（{len(days)}日）",
        "how": "11月の元の表で、土日に勤務の人が1人ずつ急に休んだ全部の場合を1件ずつ試し、"
               "待機が無い時のアプリの直しで、その日の休みから勤務に入った人を数えた",
        "cases": cases,
        "filled_by_off_staff": cases - failed - inside,
        "filled_inside": inside,
        "failed": failed,
        "calls": dict(top),
        "top2": [{"name": n, "count": c} for n, c in top[:2]],
        "seconds": round(time.perf_counter() - t0, 1),
    }


def main() -> None:
    seed = ROOT / "data" / "hidamari"
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.from_seed(seed, Path(tmp) / "run")
        res = simulate(store)
    atomic_write_json(OUT, res)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
