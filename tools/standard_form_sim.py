"""標準様式（従業者の勤務の体制及び勤務形態一覧表）の貼り付けを、シフト記号表と一緒に取り込み、
貼った月と作った翌月の「次の勤務までの間が短い並び」の数、計算の秒、ルールの数を出す。
Claude は呼ばない。貼り付けの文字はリポジトリに置かず、引数で受ける。

    uv run python tools/standard_form_sim.py <貼り付けのファイル> [<シフト記号表のファイル>] [--month 2027-02]

貼る月は、4週（28日）の表なら 2027-02 を既定にする（2月は28日）。暦月の表は --month で月を指す。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice import onboard  # noqa: E402
from shiftvoice.nextmonth import next_ym  # noqa: E402


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    month = "2027-02"
    if "--month" in argv:
        month = argv[argv.index("--month") + 1]
        args = [a for a in args if a != month]
    if not args:
        print(__doc__)
        return 2
    year, mon = int(month[:4]), int(month[5:7])
    pasted = onboard.parse_paste(Path(args[0]).read_text(encoding="utf-8"))
    symtext = Path(args[1]).read_text(encoding="utf-8") if len(args) > 1 else ""
    plan = onboard.plan_symbols(pasted, symtext) if symtext else None
    answers = dict(plan.answers) if plan else {}
    times = dict(plan.times) if plan else {}
    new_shifts = []
    if plan:
        # 記号表から決めなかった記号（7時間より短い勤務など）は、画面で［新しい勤務］を選んだ時と同じにし、
        # 記号表の時刻を入れる（作成者がパートの勤務を遅番と同じにすることは、まず無いため）
        legend = onboard.parse_symbol_table(symtext)
        for sym in plan.undecided:
            info = legend.get(sym)
            if info and "start" in info:
                answers[sym] = {"name": f"{sym}の勤務", "kind": "work"}
                length = (info["end"] - info["start"]) % 1440 or 1440
                times[sym] = {"start": f"{info['start'] // 60:02d}:{info['start'] % 60:02d}",
                              "hours": int(length / 60) if length % 60 == 0 else round(length / 60, 2)}
                new_shifts.append(sym)
    prop = onboard.infer(pasted, year, mon, answers)
    ny, nm = next_ym(year, mon)
    print(f"読んだ人数 {len(prop.staff)}人（兼務で1人にまとめた人 {len(pasted.merged)}人）、日数 {pasted.days}、"
          f"知らない記号 {len(pasted.unknown_symbols)}つ、記号表で決めた {len(plan.answers) if plan else 0}つ、"
          f"新しい勤務にした {new_shifts}、決められなかった "
          f"{[x for x in plan.undecided if x not in new_shifts] if plan else pasted.unknown_symbols}")
    if prop.problems:
        print("このままでは作れない理由", prop.problems)
    with tempfile.TemporaryDirectory() as tmp:
        t0 = time.perf_counter()
        c = onboard.create_facility(prop, Path(tmp) / "f", name="試験", next_year=ny, next_month=nm,
                                    answers=answers, times=times or None)
        sec = time.perf_counter() - t0
    sr = c.short_rest or {}
    print(f"ルール {c.rule_count}件、翌月の表 {'できた' if c.next_table_made else 'できなかった'}、計算 {sec:.1f}秒")
    if sr:
        print(f"短い並び（{sr['line']}時間未満） {year}年{mon}月 {sr['before']}件 → {ny}年{nm}月 {sr['after']}件"
              + (f"（時刻の無い勤務 {sr['untimed']}）" if sr["untimed"] else ""))
    # 貼った月と違う所（画面に赤で出る物）も出す。短い並びが減っても、夜勤が0人の夜などができていないかを見るため
    for d in c.diffs:
        print("違う所", d["text"])
    return 0 if c.next_table_made else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
