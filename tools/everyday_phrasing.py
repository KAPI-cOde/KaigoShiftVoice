"""現場のふだんの言い方（tools/everyday_phrases.json）を本物の Claude Haiku でアプリに通し、正しく直した・表を変えずに
聞き返した・間違えて表を変えた、を数える。鍵が要る（.env か環境変数）。

    uv run python tools/everyday_phrasing.py [--runs 3] [--set A|B] [--out 置き場.json]

組A は直す前に通して、見つかった所を直した組。組B は直した後に初めて通した組（直す時に見ていない）。

1件ごとに、見本の施設「ひだまりの郷」の11月の最初の状態から始める（前の一言の結果を持ち越さない）。今日は
2026-11-05（木）として読む（「明日」は6日）。数え方は次のとおり。
- 表を変えなかった（聞き返した・まだ扱えないと言った・読み取りに失敗した）は「聞き返した」
- 表を変えた時は、cells（その人のその日の勤務）・rule（ルールの名前）・keep_count（その人のその勤務の回数が変わらない）が
  合えば「正しく直した」、合わなければ「間違えて変えた」。直すのが正しいのに聞き返した物も「聞き返した」に数える
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice.reader import HaikuReader  # noqa: E402
from shiftvoice.server import load_dotenv  # noqa: E402
from shiftvoice.session import JST, Session  # noqa: E402
from shiftvoice.store import SEED, Store  # noqa: E402

DATA = ROOT / "tools" / "everyday_phrases.json"


def judge(p: dict, kind: str, before: dict, after: dict, message: str) -> str:
    if kind != "applied":
        return "聞き返した"
    if p.get("cells"):
        ok = all(after[n][d - 1] == s for n, d, s in p["cells"])
        return "正しく直した" if ok else "間違えて変えた"
    if p.get("keep_count"):
        n, s = p["keep_count"]
        return "正しく直した" if before[n].count(s) == after[n].count(s) else "間違えて変えた"
    if p.get("rule"):
        return "正しく直した" if p["rule"] in (message or "") else "間違えて変えた"
    return "間違えて変えた"


def run_once(phrases: list[dict], today: dt.datetime) -> list[dict]:
    out = []
    for p in phrases:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "h"
            shutil.copytree(SEED, d)
            s = Session(Store(d), 2026, 11, HaikuReader(), now=lambda: today)
            s.open()
            before = {n: list(r) for n, r in s.store.month(2026, 11).table.items()}
            t0 = time.time()
            try:
                r = s.say(p["text"])
                kind, message = r.kind, r.message
            except Exception as e:  # noqa: BLE001 数えるために止めない
                kind, message = "exception", f"{type(e).__name__}"
            after = s.store.month(2026, 11).table
            moved = sum(1 for n in before for a, b in zip(before[n], after[n]) if a != b)
            out.append({"no": p["no"], "text": p["text"], "want": p["want"], "kind": kind, "message": message,
                        "result": judge(p, kind, before, after, message), "moved": moved,
                        "seconds": round(time.time() - t0, 1)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--set", choices=["A", "B"], action="append", help="省くと両方")
    ap.add_argument("--out")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    today = dt.datetime.combine(dt.date.fromisoformat(data["today"]), dt.time(22, 0), tzinfo=JST)
    runs = []
    for i in range(args.runs):
        for name in args.set or ["A", "B"]:
            res = run_once(data["sets"][name]["phrases"], today)
            runs.append({"run": i + 1, "set": name, "results": res})
            count = {k: sum(1 for x in res if x["result"] == k) for k in ("正しく直した", "聞き返した", "間違えて変えた")}
            print(f"{i + 1}回目 組{name}（{len(res)}件） " + "・".join(f"{k} {v}件" for k, v in count.items()))
            for x in res:
                if x["result"] == "間違えて変えた":
                    print(f"  間違えて変えた {x['no']} {x['text']} → {x['message']}（{x['moved']}マス）")
    if args.out:
        Path(args.out).write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
