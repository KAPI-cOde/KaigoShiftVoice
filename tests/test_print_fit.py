"""印刷が A4 横の1枚に収まるよう、行の高さを縮める（2026-11-06 通しの試し）。話して足した勤務で人数の行が増えると、
3ユニット16人の表の凡例が2枚目へはみ出した。本物の紙は見えないブラウザの PDF で確かめ、ここでは高さの計算を確かめる。"""
import json
import pathlib
import shutil
import subprocess

import pytest

import shiftvoice

HTML = pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html"


def heights(people, units, foot):
    if not shutil.which("node"):
        pytest.skip("node が無い")
    js = HTML.read_text(encoding="utf-8").split("// PRINT-BEGIN")[1].split("// PRINT-END")[0]
    prog = js + f"console.log(JSON.stringify(printHeights({people}, {units}, {foot})));"
    return json.loads(subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout)


def test_small_table_keeps_the_old_heights():
    assert heights(16, 2, 8) == {"row": 20, "foot": 13}


def test_three_units_with_a_spoken_shift_shrinks():
    h = heights(16, 3, 18)
    assert h["row"] < 20 and h["foot"] < 13
    assert 16 * h["row"] + 18 * h["foot"] <= 670 - (32 + 3 * 16 + 62)


def test_heights_stay_readable():
    h = heights(100, 3, 30)
    assert h == {"row": 12, "foot": 9}
