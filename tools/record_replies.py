"""本物の Claude で返事を録り、用意した返事（tests/fixtures）と比べる。鍵が要る（.env か環境変数）。

uv run python tools/record_replies.py [--runs N] [--model claude-haiku-5-5] [--out 置き場.json] [--no-build]

- 送るのは名前を伏せた一言だけ（用意した返事のキーそのもの）
- 「合う」は、手元の確かめ（intake）に通した後の中身が同じこと（理由の言い方や、人が文字列か並びかの違いは数えない）
- 違う時は、手元に通した後の違いと、返事の項目の違いを出す。用意した返事を直す前にメインが読んで決める
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice.intake import Clarify, to_proposal  # noqa: E402
from shiftvoice.names import Masker  # noqa: E402
from shiftvoice.reader import (HaikuReader, ReadError, RecordedReader, SonnetBuilder,  # noqa: E402
                               parse_build, parse_reply)
from shiftvoice.server import load_dotenv  # noqa: E402
from shiftvoice.session import Session  # noqa: E402
from shiftvoice.store import Store  # noqa: E402

KEYS = ("type", "staff", "days", "shift", "first", "next", "min", "max", "until_day", "from_day", "to_day",
        "weekdays", "avoid", "from_month", "can_add", "strength")


def _built_store(tmp: Path) -> Store:
    """11月の元の表を作った置き場（move の元の日を表から補うため）。"""
    d = tmp / "hidamari"
    shutil.copytree(ROOT / "data" / "hidamari", d)
    st = Store(d)
    replies = json.loads((ROOT / "tests" / "fixtures" / "replies.json").read_text(encoding="utf-8"))
    Session(st, 2026, 11, RecordedReader(replies)).open()
    return st


def _settle(items, masked, st: Store):
    """手元の確かめに通した後の中身。custom は組み立て役へ回る印だけ。"""
    if any(it.get("type") == "custom" for it in items):
        return ("custom",)
    if any(it.get("type") == "unclear" for it in items):
        return ("unclear",)   # 聞き返しの文の言い方は数えない
    fac, staff = st.facility(), st.staff()
    m = st.month(2026, 11)
    real = Masker(staff, fac).unmask(items)
    try:
        p = to_proposal(real, masked, fac, staff, m, leave_caps={"斉藤": 2})
    except Clarify as e:
        return ("clarify", str(e))
    rules = [(r["name"], r["kind"], r["flex"], json.dumps(r["params"], sort_keys=True, ensure_ascii=False),
              tuple(r["who"] or ()), tuple(r["when"] or ())) for r in p.rules]
    sc = [(c["staff"], c["from"], tuple(c["can_add"]), tuple(c["can_remove"])) for c in p.staff_changes]
    rm = [(tuple(x["who"]), tuple(x["days"]), x["shift"]) for x in p.removes]
    return (p.kind, tuple(rules), tuple(sc), tuple(rm), p.scope, p.until)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=str(ROOT / "data" / "runtime" / "recorded_replies.json"))
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--only", default=None, help="この文字を含む一言だけ")
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads((ROOT / "tests" / "fixtures" / "replies.json").read_text(encoding="utf-8"))
    if a.only:
        fixtures = {k: v for k, v in fixtures.items() if a.only in k}
    tmp = Path(tempfile.mkdtemp())
    st = _built_store(tmp)
    reader = HaikuReader(record_to=out) if a.model is None else HaikuReader(model=a.model, record_to=out)
    tally = {k: 0 for k in fixtures}
    secs = []
    for run in range(a.runs):
        bad = 0
        for masked, want_text in fixtures.items():
            want = parse_reply(want_text)
            t0 = time.time()
            try:
                got = reader.read(masked)
            except ReadError as e:
                print(f"失敗 {masked} {e}")
                bad += 1
                continue
            secs.append(time.time() - t0)
            sw, sg = _settle(want, masked, st), _settle(got, masked, st)
            ok = sw == sg
            tally[masked] += ok
            if not ok:
                diffs = [f"{i}.{k} 用意={w.get(k)} 本物={g.get(k)}" for i, (w, g) in enumerate(zip(want, got))
                         for k in KEYS if w.get(k) != g.get(k)]
                if len(want) != len(got):
                    diffs.append(f"件数 用意={len(want)} 本物={len(got)}")
                diffs.append(f"手元 用意={sw}")
                diffs.append(f"手元 本物={sg}")
                print("違う " + masked + "\n    " + "\n    ".join(diffs))
            bad += not ok
        print(f"[{run + 1}周目] 合う {len(fixtures) - bad} / {len(fixtures)} 件")
    if a.runs > 1:
        print("一言ごとの合った回数")
        for k, n in tally.items():
            if n < a.runs:
                print(f"  {n}/{a.runs} {k}")
    if secs:
        secs.sort()
        print(f"所要 中央 {secs[len(secs) // 2]:.2f}秒 最長 {secs[-1]:.2f}秒（{len(secs)}回）")
    if not a.no_build:
        builds = json.loads((ROOT / "tests" / "fixtures" / "builds.json").read_text(encoding="utf-8"))
        b = SonnetBuilder()
        for masked, want_text in builds.items():
            got = b.build(masked)
            print("組み立て", masked, "\n    本物=", json.dumps(got, ensure_ascii=False),
                  "\n    用意=", json.dumps(parse_build(want_text), ensure_ascii=False))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"録った返事 {out}")


if __name__ == "__main__":
    main()
