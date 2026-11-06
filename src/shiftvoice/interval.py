"""次の勤務までの間が短い並びを数える。

医学的な判断ではなく、表の並びの数を数えるだけ。勤務の種類の「始まりの時刻」と「長さ」から、
前の日の勤務の終わりから次の日の勤務の始まりまでの時間を出し、線（時間）より短い並びを数える。
夜勤（翌日が明けの勤務）と明け・休みは数えない。
"""
from __future__ import annotations

DEFAULT_HOURS = 14.0


def _get(t, key):
    return t.get(key) if isinstance(t, dict) else getattr(t, key, None)


def _clock(s: str) -> float:
    h, m = s.split(":")
    return int(h) + int(m) / 60


def crosses_midnight(t) -> bool:
    """始まりの時刻＋長さが24時間を超える（日をまたぐ）勤務か。"""
    st, hrs = _get(t, "start"), _get(t, "hours")
    return bool(st and hrs and _clock(st) + hrs > 24)


def short_pairs(shift_types, line_hours: float = DEFAULT_HOURS) -> dict[tuple[str, str], float]:
    """線より短い（前の日の記号, 次の日の記号）→ 線との差（時間）。時刻と長さのある、夜勤でない勤務だけ。"""
    timed = [t for t in shift_types
             if _get(t, "start") and _get(t, "hours") and not _get(t, "next_is_ake")
             and _get(t, "kind") in (None, "work")]
    out: dict[tuple[str, str], float] = {}
    for a in timed:
        for b in timed:
            if crosses_midnight(a) and crosses_midnight(b):   # 日をまたぐ勤務どうし（夜→夜）は数えない（2026-11-07）
                continue
            rest = 24 - (_clock(_get(a, "start")) + _get(a, "hours")) + _clock(_get(b, "start"))
            if rest < line_hours:
                out[(_get(a, "symbol"), _get(b, "symbol"))] = line_hours - rest
    return out


def count_short(table: dict[str, list[str]], shift_types, line_hours: float = DEFAULT_HOURS,
                prev_tail: dict[str, list[str]] | None = None) -> tuple[int, dict[str, int]]:
    """線より短い並びの総数と、組ごとの数（「遅→早」の形）。前の月の末日と1日目の組も数える。"""
    pairs = short_pairs(shift_types, line_hours)
    names = {_get(t, "symbol"): _get(t, "name") for t in shift_types}
    by: dict[str, int] = {}
    total = 0
    for nm, row in table.items():
        seq = list((prev_tail or {}).get(nm, [])[-1:]) + list(row)
        for a, b in zip(seq, seq[1:]):
            if (a, b) in pairs:
                total += 1
                k = f"{names.get(a, a)}→{names.get(b, b)}"
                by[k] = by.get(k, 0) + 1
    return total, by
