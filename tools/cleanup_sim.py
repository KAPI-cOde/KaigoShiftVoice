"""片付けの候補を毎月受け入れ続けた時に、守りが消えないかを確かめる（開発だけ）。

uv run python tools/cleanup_sim.py <置き場のフォルダ> [--months 6] [--accept first]

tools/many_rules.py と同じ架空の施設（ひだまりの16人＋生成したルール）を作り、11月から月ごとに
表を作る → 静かなルールを数える → 翌月の見直しで、片付けの候補を全部「先に出したボタン」で片付ける、を繰り返す。
見るのは次の3つ。
- ルールの件数の動き
- 片付けたルールの中身が、その後の表で破れた場面の数（0に近いほど、片付けても守りが消えていない）
- 残したルールの破れ（片付けで表の質が落ちていないか。動かせない決まりは0のはず）
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from many_rules import gen_rules  # noqa: E402
from shiftvoice.build import build_original, month_rules  # noqa: E402
from shiftvoice.model import Rule  # noqa: E402
from shiftvoice.nextmonth import copy_next_month, next_ym  # noqa: E402
from shiftvoice.quiet import (check_month, no_scene_ids, overlap_streaks, quiet_months_setting,  # noqa: E402
                              quiet_per_month_setting, streaks)
from shiftvoice.review import answer, review_cards  # noqa: E402
from shiftvoice.rules import check_table  # noqa: E402
from shiftvoice.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quiet-months", type=int, default=None, help="見直しに出すまでの月数（既定は施設の設定の3）")
    a = ap.parse_args()
    d = Path(a.dir)
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(ROOT / "data" / "hidamari", d, ignore=shutil.ignore_patterns("premise", "absence-sim-*"))
    store = Store(d)
    if a.quiet_months:
        fac = json.loads((d / "facility.json").read_text())
        fac["settings"].setdefault("review", {})["quiet_months"] = a.quiet_months
        (d / "facility.json").write_text(json.dumps(fac, ensure_ascii=False, indent=1))
    led = store.ledger()
    for i, r in enumerate(gen_rules([s.to_dict() for s in store.staff()], random.Random(a.seed)), 1):
        r["id"] = f"g{i:03d}"
        led.append("add", rule=r, op_id=f"gen-{i}", frm="2026-06-01", meta={"kind": "gen"})
    store.save_ledger(led)
    archived: dict[str, Rule] = {}
    y, m = 2026, 11
    rows = []
    for i in range(a.months):
        t = time.time()
        r = build_original(store, y, m, with_premise=False)
        mon = store.month(y, m)
        mon.table = r.result.table
        store.save_month(mon)
        fac, staff = store.facility(), store.staff_on(y, m)
        rules = month_rules(store, y, m, with_premise=False)
        kept_broken = check_table(rules, fac, staff, mon, mon.table)
        hard = [b for b in kept_broken if b["flex"] == "fixed"]
        gone_broken = check_table(list(archived.values()), fac, staff, mon, mon.table) if archived else []
        q = check_month(store, y, m)
        ny, nm = next_ym(y, m)
        copy_next_month(store, y, m)
        s, qm, lap, ns = streaks(store, ny, nm), quiet_months_setting(store), overlap_streaks(store, ny, nm), \
            no_scene_ids(store, ny, nm)
        v = review_cards(store.ledger(), ny, nm, s, qm, quiet_per_month_setting(store), lap, ns)
        cur = store.ledger().rules_at()
        n_arch = 0
        for c in v["cards"]:
            if c.reason not in ("quiet", "overlap"):
                answer(store, ny, nm, c.rule_id, "keep", quiet=s, quiet_months=qm, overlap=lap, no_scene=ns)
                continue
            for rid in [c.rule_id] + c.group:
                archived[rid] = cur[rid]
                n_arch += 1
            answer(store, ny, nm, c.rule_id, "archive", quiet=s, quiet_months=qm, overlap=lap, no_scene=ns)
        row = {"month": f"{y}-{m:02d}", "rules": len(rules), "table": r.result.status,
               "kept_broken": len(kept_broken), "hard_broken": len(hard),
               "archived_so_far": len(archived) - n_arch, "archived_broken": len(gone_broken),
               "archived_broken_rules": sorted({b["name"] for b in gone_broken}),
               "quiet": sum(1 for v2 in q["rules"].values() if v2["state"] == "quiet"),
               "overlap": sum(1 for v2 in q["rules"].values() if v2["state"] == "overlap"),
               "archive_next": n_arch, "seconds": round(time.time() - t, 1)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        y, m = ny, nm
    (d / "cleanup_sim.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
