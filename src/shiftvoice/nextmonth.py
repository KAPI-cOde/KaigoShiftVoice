"""翌月の写し（帳面を読んで次の月の表の置き場を作る）。

- 前の月の末5日は、前の月の表の最後の5日（夜勤の明けと連勤を月またぎで数えるため）
- 祝日は施設の設定（settings.holidays の "YYYY-MM"）
- 決まりは作らない。表を作る時に帳面を対象の月の期間で読むので、毎月の札は残り、
  今月だけの札（until が前の月の末日）と期限が過ぎた札は効かない（13章。適用は対象の表の日付で判定）
- 職員情報の変更（12月から夜勤もできる 等）は、表を作る時に月ごとに当てる（Store.staff_on）
"""
from __future__ import annotations

from .model import Month
from .store import Store

TAIL = 5


def next_ym(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def copy_next_month(store: Store, year: int, month: int, *, overwrite: bool = False) -> Month:
    cur = store.month(year, month)
    if cur.table is None:
        raise ValueError(f"{year}-{month:02d} の表がまだありません")
    ny, nm = next_ym(year, month)
    try:
        existing = store.month(ny, nm)
        if not overwrite:
            if existing.table is None:  # まだ作っていない翌月は、前の月の末5日をいまの表から取り直す（Astra の指摘8）
                existing.prev_tail = {name: list(row[-TAIL:]) for name, row in cur.table.items()}
                store.save_month(existing)
            return existing
    except FileNotFoundError:
        pass
    hol = store.facility().settings.get("holidays", {}).get(f"{ny:04d}-{nm:02d}", [])
    tail = {name: list(row[-TAIL:]) for name, row in cur.table.items()}
    m = Month(year=ny, month=nm, holidays=list(hol), prev_tail=tail)
    store.save_month(m)
    return m
