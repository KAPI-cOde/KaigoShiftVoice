"""画面5の「架空の表の累計の試算」。置いてある結果が、いまのコードで測り直した値と同じか（台本 v3 の1分10秒）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from absence_sim import OUT, simulate  # noqa: E402
from shiftvoice.store import Store  # noqa: E402


def test_absence_sim_reproduces(tmp_path):
    res = simulate(Store.from_seed(ROOT / "data" / "hidamari", tmp_path / "run"))
    saved = json.loads(OUT.read_text(encoding="utf-8"))
    for k in ("cases", "filled_by_off_staff", "filled_inside", "failed", "calls", "top2"):
        assert res[k] == saved[k], k
    assert res["failed"] == 0
    assert sum(res["calls"].values()) == res["filled_by_off_staff"]   # 1件に1人
    avg = res["filled_by_off_staff"] / len(res["calls"])
    assert res["top2"][1]["count"] > avg                               # 多い順の2人は平均より多い（集まっている）


def test_absence_sim_in_state(tmp_path):
    from shiftvoice.session import Session
    st = Store.from_seed(ROOT / "data" / "hidamari", tmp_path / "run")
    assert Session(st, 2026, 11, reader=None).state()["absence_sim"]["top2"][0]["name"]
