"""保存の骨組み。1か月1ファイルの JSON。書く時は一時ファイルに書いてから置き換える。

- 月の表           months/YYYY-MM.json
- 未反映の発言     months/YYYY-MM.pending.json（発言 ID つき。送り直しの置き場）
- 変更の帳面       ledger.json（施設に1本）
- 引き受けの記録   acceptances.json（tx とは別。op_id でつなぐ）
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .ledger import Ledger
from .model import Acceptance, Facility, Month, Staff


def atomic_write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Store:
    """施設1つ分の置き場。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    @classmethod
    def from_seed(cls, seed_dir: Path, runtime_dir: Path, *, reset: bool = False) -> "Store":
        """リポジトリの初めのデータを、動かす用の置き場へ写す（初めの1回か reset の時）。"""
        runtime_dir = Path(runtime_dir)
        if reset and runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        if not runtime_dir.exists():
            shutil.copytree(seed_dir, runtime_dir)
        return cls(runtime_dir)

    # 施設・職員
    def facility(self) -> Facility:
        return Facility.from_dict(read_json(self.root / "facility.json"))

    def staff(self) -> list[Staff]:
        return [Staff.from_dict(s) for s in read_json(self.root / "staff.json")]

    # 帳面
    def ledger(self) -> Ledger:
        return Ledger.from_list(read_json(self.root / "ledger.json", []))

    def save_ledger(self, ledger: Ledger) -> None:
        atomic_write_json(self.root / "ledger.json", ledger.to_list())

    # 月
    def _month_path(self, year: int, month: int) -> Path:
        return self.root / "months" / f"{year:04d}-{month:02d}.json"

    def month(self, year: int, month: int) -> Month:
        d = read_json(self._month_path(year, month))
        if d is None:
            raise FileNotFoundError(f"{year}-{month:02d} の月の表がない")
        return Month.from_dict(d)

    def save_month(self, m: Month) -> None:
        atomic_write_json(self._month_path(m.year, m.month), m.to_dict())

    # 未反映の発言
    def _pending_path(self, year: int, month: int) -> Path:
        return self.root / "months" / f"{year:04d}-{month:02d}.pending.json"

    def pending(self, year: int, month: int) -> list[dict]:
        return read_json(self._pending_path(year, month), [])

    def add_pending(self, year: int, month: int, text: str, **extra) -> dict:
        items = self.pending(year, month)
        u = {"id": "u-" + uuid.uuid4().hex[:12], "text": text, **extra}
        items.append(u)
        atomic_write_json(self._pending_path(year, month), items)
        return u

    def resolve_pending(self, year: int, month: int, uid: str) -> None:
        items = [u for u in self.pending(year, month) if u["id"] != uid]
        atomic_write_json(self._pending_path(year, month), items)

    # 引き受けの記録
    def acceptances(self) -> list[Acceptance]:
        return [Acceptance.from_dict(a) for a in read_json(self.root / "acceptances.json", [])]

    def save_acceptances(self, items: list[Acceptance]) -> None:
        atomic_write_json(self.root / "acceptances.json", [a.to_dict() for a in items])

    # 撮影・試験用の前提
    def premise(self, year: int, month: int) -> dict | None:
        return read_json(self.root / "premise" / f"{year:04d}-{month:02d}.json")
