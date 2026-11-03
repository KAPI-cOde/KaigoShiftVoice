"""ルールが200件ほどに増えた架空の施設で、静かなルールの見つけ方を確かめる（開発だけ）。

uv run python tools/many_rules.py <置き場のフォルダ> [--months 3] [--workers 8]

ひだまりの郷（架空）の16人を土台に、施設全体のルールと職員ごとのルールを、くじで決めた中身で足す。
実在の施設のルールは使わない（件数の目安だけを、16人の施設で200件前後とした）。
11月から月ごとに表を作り、そのたびに静かなルールを数え、最後に翌月の見直しに出る候補を表示する。

中に入れる「静かになりやすいルール」の例（入れた理由）
- 名簿にいない人のルール（辞めた人の事情が残る）
- 誰も届かない上限（夜勤は月8回まで など。ほかのルールで先に止まる）
- 同じ人・同じ勤務の上限が2件（月5回までと月6回まで。ゆるい方は効かない）
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice.build import build_original  # noqa: E402
from shiftvoice.nextmonth import copy_next_month, next_ym  # noqa: E402
from shiftvoice.quiet import check_month, no_scene_ids, overlap_streaks, quiet_months_setting, streaks  # noqa: E402
from shiftvoice.review import review_cards  # noqa: E402
from shiftvoice.store import Store  # noqa: E402

WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WDJ = dict(zip(WD, "月火水木金土日"))


def gen_rules(staff: list[dict], rnd: random.Random) -> list[dict]:
    out: list[dict] = []

    def add(name, kind, flex, params, who=None):
        out.append({"name": name, "kind": kind, "flex": flex, "params": params, "who": who, "when": None,
                    "scope": "rule", "meta": {"source": "架空の生成", "said": name}})

    for s in staff:
        n = s["name"]
        can = s["can"]
        if "夜" in can:
            k = rnd.choice([4, 5, 6, 8])
            add(f"{n}さん 夜勤は月{k}回まで", "count", "operational", {"shifts": ["夜"], "max": k}, [n])
        for sym in ("早", "遅"):
            if sym in can and rnd.random() < 0.8:
                k = rnd.choice([5, 6, 7, 9, 12])
                add(f"{n}さん {sym}番は月{k}回まで", "count", "negotiable", {"shifts": [sym], "max": k}, [n])
                if rnd.random() < 0.3:   # 同じ人・同じ勤務の上限がもう1件（言い直しが残った形）
                    add(f"{n}さん {sym}番は月{k + 1}回まで", "count", "negotiable", {"shifts": [sym], "max": k + 1}, [n])
        for _ in range(rnd.choice([2, 3, 3, 4])):
            wd = rnd.choice(WD)
            sym = rnd.choice([x for x in can if x != "日"] or can)
            add(f"{n}さん {WDJ[wd]}曜は{ {'夜': '夜勤', '日': '日勤'}.get(sym, sym + '番') }に入れない", "weekday", rnd.choice(["wish", "negotiable"]),
                {"weekdays": [wd], "not_in": [sym]}, [n])
        if rnd.random() < 0.3:
            wd = rnd.choice(WD)
            add(f"{n}さん {WDJ[wd]}曜は休みにしたい", "weekday", "negotiable", {"weekdays": [wd], "in": ["公"]}, [n])
        if rnd.random() < 0.5:
            k = rnd.choice([3, 4])
            add(f"{n}さん 連勤は{k}日まで", "max_run", "negotiable", {"shifts": "work", "max": k}, [n])
        if "夜" in can and rnd.random() < 0.4:
            add(f"{n}さん 夜勤の前の日は早番にしない", "sequence", "negotiable",
                {"if": ["早"], "next_not_in": ["夜"]}, [n])
        if rnd.random() < 0.3:
            add(f"{n}さん 遅番の2連続を避ける", "max_run", "negotiable", {"shifts": ["遅"], "max": 1}, [n])
    for gone in ("森", "松井", "長谷", "宮本", "野口"):   # 辞めた人のルールが残った形
        add(f"{gone}さん 日曜は早番に入れない", "weekday", "wish", {"weekdays": ["sun"], "not_in": ["早"]}, [gone])
        add(f"{gone}さん 夜勤は月3回まで", "count", "operational", {"shifts": ["夜"], "max": 3}, [gone])
    # 施設全体のルール
    add("夜勤は月8回まで", "count", "fixed", {"shifts": ["夜"], "max": 8})
    add("早番は月15回まで", "count", "negotiable", {"shifts": ["早"], "max": 15})
    add("遅番は月15回まで", "count", "negotiable", {"shifts": ["遅"], "max": 15})
    add("連勤は7日まで", "max_run", "fixed", {"shifts": "work", "max": 7})
    add("夜勤の2連続を避ける", "max_run", "negotiable", {"shifts": ["夜"], "max": 1})
    add("早番の3連続を避ける", "max_run", "negotiable", {"shifts": ["早"], "max": 2})
    add("遅番の3連続を避ける", "max_run", "negotiable", {"shifts": ["遅"], "max": 2})
    add("夜勤明けの翌日に夜勤を入れない", "sequence", "fixed", {"if": ["明"], "next_not_in": ["夜"]})
    add("遅番の次の日に早番を置かない", "sequence", "negotiable", {"if": ["遅"], "next_not_in": ["早"]})
    add("明けの翌日に早番を置かない", "sequence", "fixed", {"if": ["明"], "next_not_in": ["早"]})
    for wd in WD:
        add(f"{WDJ[wd]}曜の早番は1人", "coverage", "fixed",
            {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 1, "weekdays": [wd]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    d = Path(a.dir)
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(ROOT / "data" / "hidamari", d, ignore=shutil.ignore_patterns("premise", "absence-sim-*"))
    store = Store(d)
    led = store.ledger()
    staff = [s.to_dict() for s in store.staff()]
    rules = gen_rules(staff, random.Random(a.seed))
    for i, r in enumerate(rules, 1):
        r["id"] = f"g{i:03d}"
        led.append("add", rule=r, op_id=f"gen-{i}", frm="2026-06-01", meta={"kind": "gen"})   # 前からあるルール
    store.save_ledger(led)
    print(f"ルール {len(led.rules_at())}件（足したのは{len(rules)}件）")
    y, m = 2026, 11
    for i in range(a.months):
        if i:
            copy_next_month(store, *prev)
        t = time.time()
        r = build_original(store, y, m, with_premise=False)
        mon = store.month(y, m)
        mon.table = r.result.table
        store.save_month(mon)
        print(f"{y}-{m:02d} 表 {r.result.status} 段{[s['stage'] for s in r.stages]} {time.time() - t:.1f}秒", flush=True)
        q = check_month(store, y, m, workers=a.workers)
        st = [v["state"] for v in q["rules"].values()]
        print(f"  静か {st.count('quiet')}件（場面なし {sum(1 for v in q['rules'].values() if v.get('no_scene'))}件）"
              f"・重なり {st.count('overlap')}件・効いた {st.count('needed')}件・不明 {st.count('unknown')}件  {q['seconds']}秒",
              flush=True)
        prev = (y, m)
        y, m = next_ym(y, m)
    copy_next_month(store, *prev)
    s = streaks(store, y, m)
    v = review_cards(store.ledger(), y, m, s, quiet_months_setting(store), overlap=overlap_streaks(store, y, m),
                     no_scene=no_scene_ids(store, y, m))
    print(f"{y}-{m:02d} の見直しに出る {len(v['cards'])}件 / ほかに静か・重なり {v['quiet_more']}件 / 変更なし {v['unchanged']}件")
    for c in v["cards"]:
        print("  ", c.reason, c.months, "か月", c.name, ("と " + c.partner_name) if c.partner_name else "")


if __name__ == "__main__":
    main()
