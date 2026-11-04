"""見本の施設（ひだまりの郷）の11月の表を作り直して、data/hidamari/months/2026-11.json に入れる。

見本の表は、どの OS でも同じ表から始まるように、作った表をデータに入れて配っている。
計算の部品（OR-Tools）は OS ごとに探し方の道筋が違うので、作り直すのは作者の Mac で行う
（動画・写真・README の数は、この表から測っている）。施設・職員・ルールの見本を変えた時に使う。

使い方: uv run python tools/build_seed.py
"""
import platform
import shutil
import sys
import tempfile
from pathlib import Path

from shiftvoice.build import build_original
from shiftvoice.store import Store

SEED = Path(__file__).resolve().parents[1] / "data" / "hidamari"
FILE = SEED / "months" / "2026-11.json"


def main() -> int:
    if platform.system() != "Darwin":
        print("注意。Mac 以外で作ると、動画や写真と違う表になります", file=sys.stderr)
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "hidamari"
        shutil.copytree(SEED, d)
        st = Store(d)
        m = st.month(2026, 11)
        m.table = None
        st.save_month(m)
        r = build_original(st, 2026, 11)
        if not r.ok:
            print(r.message or "11月の表が作れませんでした", file=sys.stderr)
            return 1
        m = st.month(2026, 11)
        m.table, m.built_rev = r.result.table, st.ledger().rev
        st.save_month(m)
        new = (d / "months" / "2026-11.json").read_text(encoding="utf-8")
    if new == FILE.read_text(encoding="utf-8"):
        print("今の見本の表と同じでした（変えていません）")
        return 0
    FILE.write_text(new, encoding="utf-8")
    print("見本の11月の表を作り直しました。README の数と写真を測り直してください")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
