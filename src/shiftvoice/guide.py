"""見本の施設（ひだまりの郷）の11月の表の上に出す、表の問題点の案内（2026-11-06 作者）。

介護員でない審査員には、表のどこが問題か分からない。そこで、見本の表にある本物の問題を1つ、数と理由と
直し方の一言の例で先に伝え、押すと表のその並びのマスに赤い枠を付ける。数は表から数える。

問題は「遅番の次の日に早番」。遅番と早番の記号の並びそのものを数え、その並びが、勤務の時刻から数える
短い並び（interval.short_pairs、線は standby.SHORT_GAP_HOURS か施設の設定）に入っている時だけ出す。
前の月の末日が遅番で1日目が早番の並びも数える（interval.count_short と同じ）。その時の枠は1日目のマスだけ。
自分の施設（取り込んだ施設）には出さない。
"""
from __future__ import annotations

import json
from pathlib import Path

from .interval import short_pairs
from .standby import SHORT_GAP_HOURS

SEED = Path(__file__).resolve().parents[2] / "data" / "hidamari"
GUIDE_MONTH = (2026, 11)


def _sym(facility, name: str) -> str | None:
    return next((st.symbol for st in facility.shift_types if st.name == name), None)


def late_early(facility, table: dict[str, list[str]], prev_tail: dict[str, list[str]] | None = None,
               late: str = "遅", early: str = "早") -> list[list]:
    """遅番の次の日に早番の並びごとに、枠を付けるマス [[名前, 日], ...] の組。並びの数は len で数える。"""
    out = []
    for name, row in table.items():
        tail = list((prev_tail or {}).get(name, [])[-1:])
        seq = tail + list(row)
        off = len(tail)
        for i, (a, b) in enumerate(zip(seq, seq[1:])):
            if a == late and b == early:
                d1, d2 = i + 1 - off, i + 2 - off   # 月の中の日（前の月の末日は 0）
                out.append([[name, d] for d in (d1, d2) if d >= 1])
    return out


def _seed_month() -> dict:
    p = SEED / "months" / f"{GUIDE_MONTH[0]:04d}-{GUIDE_MONTH[1]:02d}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def guide(facility, month) -> dict | None:
    """見本の施設の11月の時だけ、案内に出す問題の一覧。ほかの施設・月は None。"""
    seed_fac = json.loads((SEED / "facility.json").read_text(encoding="utf-8"))
    if facility.id != seed_fac.get("id") or (month.year, month.month) != GUIDE_MONTH or not month.table:
        return None
    late, early = _sym(facility, "遅番"), _sym(facility, "早番")
    if late is None or early is None:
        return None
    line = float((facility.settings.get("move_rest") or {}).get("short_gap_hours", SHORT_GAP_HOURS))
    if (late, early) not in short_pairs(facility.shift_types, line):
        return None
    seed = _seed_month()
    before = len(late_early(facility, seed.get("table") or {}, seed.get("prev_tail"), late, early))
    now = late_early(facility, month.table, month.prev_tail, late, early)
    if before == 0:
        return None
    return {"problems": [{
        "id": "late_early",
        "what": "遅番の次の日に早番",
        "before": before,
        "now": len(now),
        "cells": [c for pair in now for c in pair],
        "say": "遅番の次の日に早番はきついから、やめてあげて",
    }]}
