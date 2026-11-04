"""待機当番のほかの当番も、言葉から同じように組み立てられるかを、本物の Claude Sonnet 5.5 で数える（鍵が要る）。

uv run python tools/build_duties.py [--runs 10]

一言ごとに組み立てを runs 回呼び、アプリの確かめ（validate_def）を通った回数と、通った定義のうち
「対象の日・対象の勤務・1日の人数・回数を翌月へ持ち越すか」が最多の形とそろった回数を出す。
組み立て役が定義を作らずに聞き返した回は、そのまま「聞き返し」として数える（間違った定義を黙って作らないか）。
2026-11-04 に測った結果は README の「ほかの当番も、言葉から組み立てられるか」。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice.reader import ReadError, SonnetBuilder  # noqa: E402
from shiftvoice.server import load_dotenv  # noqa: E402
from shiftvoice.standby import DefError, validate_def  # noqa: E402
from shiftvoice.store import Store  # noqa: E402

SAYS = [
    "毎月、入浴の日（月・木）は、日勤から入浴係を2人",
    "毎月、第2水曜の委員会の日は、日勤か早番から委員会の当番1人",
    "毎月、夜勤の人のうち1人を、その夜の連絡係にして",
    "毎月、土日祝は、遅番から送迎の当番1人。回数は翌月も覚えておいて",
]


def one(builder: SonnetBuilder, text: str, fac) -> dict:
    t0 = time.time()
    try:
        got = builder.build(text)
    except ReadError as e:
        return {"err": f"読み取りの失敗 {e}", "sec": time.time() - t0}
    if "question" in got:
        return {"ask": str(got["question"]), "sec": time.time() - t0}
    try:
        return {"ok": validate_def(got.get("layer"), fac), "sec": time.time() - t0}
    except DefError as e:
        return {"err": f"確かめで外れた {e}", "sec": time.time() - t0}


def shape(g: dict) -> str:
    return json.dumps({"days": g["days"], "in": sorted(g["own_shift_on_duty_day"]["in"]), "per": g["per_day"],
                       "carry": any(c.get("carry_to_next_month") for c in g["counters"])},
                      ensure_ascii=False, sort_keys=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "data" / "hidamari", tmp / "h")
    fac = Store(tmp / "h").facility()
    builder = SonnetBuilder()
    with ThreadPoolExecutor(8) as ex:
        futs = {s: [ex.submit(one, builder, s, fac) for _ in range(args.runs)] for s in SAYS}
        res = {s: [f.result() for f in fs] for s, fs in futs.items()}
    print("| 一言 | 確かめを通った | 同じ形にそろった | 聞き返し | 1回の平均秒 |")
    print("|---|---|---|---|---|")
    for s, rs in res.items():
        oks = [r["ok"] for r in rs if "ok" in r]
        top = Counter(shape(g) for g in oks).most_common(1)
        asks = sum(1 for r in rs if "ask" in r)
        print(f"| {s} | {len(oks)}/{len(rs)} | {top[0][1] if top else 0}/{len(rs)} | {asks} | "
              f"{sum(r['sec'] for r in rs) / len(rs):.1f} |")


if __name__ == "__main__":
    main()
