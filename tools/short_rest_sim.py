"""次の勤務までの間が短い並び（遅番の翌日の早番など）が、人が組んだ表と作った表でどう違うか、
職員が増えると作った表がどう変わるかを測る（作者の考え「人が組む表は、職員が多いほど徹底しきれない」の裏づけ）。
Claude は呼ばない。貼り付けは試験用の作り話の表と見本の表だけ。

    uv run python tools/short_rest_sim.py

1. tests/fixtures/pastes/ と data/samples/ の表を取り込み、貼った月の数と、重み0・重み100で作った翌月の数を出す。
2. 見本の表（8人）をユニットごとに写して 16・32・48 人に広げ、同じく重み0・重み100で作る。
   夜勤の回数と土日祝の勤務の回数の、いちばん多い人と少ない人の差も出す。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice import onboard  # noqa: E402
from shiftvoice.store import Store  # noqa: E402

YEAR, MONTH = 2026, 11
NEXT = (2026, 12)
SAMPLE = ROOT / "data" / "samples" / "komorebi-2026-11.tsv"
PASTES = sorted((ROOT / "tests" / "fixtures" / "pastes").glob("*.tsv"))
WEIGHTS = (0, 100)


def run(text: str, weight: int, *, spread: bool = False) -> dict:
    """1つの貼り付けを取り込み、重み weight で翌月を作る。読めない・作れない時は error に一文。"""
    saved = onboard.SHORT_REST_WEIGHT
    onboard.SHORT_REST_WEIGHT = weight
    try:
        try:
            pasted = onboard.parse_paste(text)
            prop = onboard.infer(pasted, YEAR, MONTH)
            with tempfile.TemporaryDirectory() as tmp:
                c = onboard.create_facility(prop, Path(tmp) / "f", name="試算", next_year=NEXT[0], next_month=NEXT[1])
                out = {"staff": c.staff_count, "made": c.next_table_made, "sec": c.seconds,
                       "note": c.stage_note or "", "before": None, "after": None}
                if c.short_rest:
                    out["before"], out["after"] = c.short_rest["before"], c.short_rest["after"]
                if spread and c.next_table_made:
                    out.update(spread_of(Store(Path(tmp) / "f"), *NEXT))
                return out
        except (onboard.ParseError, onboard.OnboardError) as e:
            return {"error": str(e)}
        except Exception as e:  # 想定外もそのまま記録する
            return {"error": f"{type(e).__name__}: {e}"}
    finally:
        onboard.SHORT_REST_WEIGHT = saved


def spread_of(store: Store, y: int, m: int) -> dict:
    """夜勤の回数と土日祝の勤務の回数の、いちばん多い人と少ない人の差。"""
    fac, mon = store.facility(), store.month(y, m)
    night = {s.symbol for s in fac.shift_types if getattr(s, "next_is_ake", False)}
    work = {s.symbol for s in fac.shift_types if s.kind == "work"}
    nights, offs = [], []
    for row in mon.table.values():
        nights.append(sum(1 for c in row if c in night))
        offs.append(sum(1 for d, c in enumerate(row, 1) if c in work and mon.is_weekend_or_holiday(d)))
    return {"night_gap": max(nights) - min(nights), "night_range": f"{min(nights)}〜{max(nights)}",
            "off_gap": max(offs) - min(offs), "off_range": f"{min(offs)}〜{max(offs)}"}


def scaled_paste(n_units: int) -> str:
    """見本の表（8人）をユニットごとに写して広げた作り話の表。名前は「職員A01」の形でプログラムが作る。"""
    lines = SAMPLE.read_text(encoding="utf-8").splitlines()
    head, rows = lines[:2], [l.split("\t") for l in lines[2:10]]
    ncol = len(head[0].split("\t"))
    out = list(head)
    k = 0
    for u in range(n_units):
        out.append(f"第{u + 1}ユニット" + "\t" * (ncol - 1))
        for r in rows:
            k += 1
            cells = r[1:] + [""] * (ncol - len(r))
            out.append("\t".join([f"職員{chr(65 + (k - 1) // 99)}{(k - 1) % 99 + 1:02d}"] + cells[:ncol - 1]))
    return "\n".join(out) + "\n"


def fmt(r: dict) -> str:
    if "error" in r:
        return f"作れない（{r['error']}）"
    return f"{r['after']}件" if r["made"] else f"作れず（{r['note']}）"


def main() -> None:
    from datetime import datetime
    print(f"実行 {datetime.now():%Y-%m-%d %H:%M}  uv run python tools/short_rest_sim.py")
    print("\n## 1. 貼った表と作った表（間が14時間未満の並び）\n")
    print("| 表 | 人数 | 貼った月 | 翌月 重み0 | 翌月 重み100 | 秒(0/100) | 段の知らせ(重み100) |")
    print("|---|---|---|---|---|---|---|")
    tot = {"b": 0, "a0": 0, "a100": 0, "n": 0}
    for p in [SAMPLE] + PASTES:
        text = p.read_text(encoding="utf-8")
        r0, r1 = run(text, 0), run(text, 100)
        if "error" in r0 and "error" in r1:
            print(f"| {p.name} | - | 読めない・作れない | - | - | - | {r0['error']} |")
            continue
        ok = lambda r: r.get("made")
        before = r1["before"] if ok(r1) else r0.get("before")
        if ok(r0) and ok(r1):
            tot["n"] += 1; tot["b"] += before; tot["a0"] += r0["after"]; tot["a100"] += r1["after"]
        print(f"| {p.name} | {r1.get('staff', r0.get('staff', '-'))} | {before if before is not None else '-'}件 | "
              f"{fmt(r0)} | {fmt(r1)} | {r0.get('sec', '-')}/{r1.get('sec', '-')} | {(r1.get('note') or '')[:60]} |")
    print(f"\n両方作れた{tot['n']}表の合計 貼った月 {tot['b']}件 → 翌月 重み0 {tot['a0']}件 / 重み100 {tot['a100']}件")
    print("\n## 2. 職員を増やした時（見本を第Nユニットとして写した架空の表）\n")
    print("| 人数 | 重み | 短い並び(貼った月→翌月) | 秒 | 段の知らせ | 夜勤の差(範囲) | 土日祝の差(範囲) |")
    print("|---|---|---|---|---|---|---|")
    for n in (1, 2, 4, 6):
        text = scaled_paste(n)
        for w in WEIGHTS:
            r = run(text, w, spread=True)
            if "error" in r:
                print(f"| {8 * n} | {w} | {fmt(r)} | - | - | - | - |")
                continue
            sp = f"{r['night_gap']}（{r['night_range']}）|{r['off_gap']}（{r['off_range']}）" if "night_gap" in r else "-|-"
            sp = sp.replace("|", " | ")
            print(f"| {r['staff']} | {w} | {r['before']}→{fmt(r)} | {r['sec']} | {r['note'][:60]} | {sp} |")


if __name__ == "__main__":
    main()
