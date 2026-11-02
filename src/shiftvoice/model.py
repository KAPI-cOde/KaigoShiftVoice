"""データの形。施設・職員・決まり（札）・変更の帳面（tx）・月の表・待機などの層・引き受けの記録。

施設ごとに変わる判断は facility.json の settings に置き、コードで決めない。
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FLEXES = ("fixed", "wish", "operational", "negotiable")


@dataclass(frozen=True)
class ShiftType:
    symbol: str          # 記号1文字（早・日・遅・夜・明・公・有・研）
    name: str
    kind: str            # work / ake / rest / leave
    start: str | None = None
    hours: float | None = None
    next_is_ake: bool = False
    on_request_only: bool = False  # 頼まれた時だけ置く（有給・研修）

    @property
    def counts_as_workday(self) -> bool:
        """連勤の数え方。明けと研修は勤務の日、公休と有給は休み（連勤を切る）。"""
        return self.kind in ("work", "ake")


@dataclass
class Facility:
    id: str
    name: str
    units: list[str]
    shift_types: list[ShiftType]
    settings: dict[str, Any]

    def shift(self, symbol: str) -> ShiftType:
        for s in self.shift_types:
            if s.symbol == symbol:
                return s
        raise KeyError(symbol)

    @property
    def symbols(self) -> list[str]:
        return [s.symbol for s in self.shift_types]

    def workday_symbols(self) -> list[str]:
        return [s.symbol for s in self.shift_types if s.counts_as_workday]

    @classmethod
    def from_dict(cls, d: dict) -> "Facility":
        return cls(
            id=d["id"], name=d["name"], units=list(d["units"]),
            shift_types=[ShiftType(**s) for s in d["shift_types"]],
            settings=d.get("settings", {}),
        )


@dataclass
class Staff:
    name: str            # 名前（施設の中で重ならない）
    unit: str
    can: list[str]       # 入れる勤務の記号
    call: str | None = None  # 呼び方
    changes: list[dict] = field(default_factory=list)  # 月から効く職員情報の変更（例 12月から夜勤可）

    def to_dict(self) -> dict:
        d = {"name": self.name, "unit": self.unit, "can": self.can}
        if self.call:
            d["call"] = self.call
        if self.changes:
            d["changes"] = self.changes
        return d

    def can_on(self, ym: str) -> list[str]:
        """その月（YYYY-MM）に入れる勤務。月から効く職員情報の変更を当てる。"""
        can = list(self.can)
        for c in sorted(self.changes, key=lambda c: c["from"]):
            if c["from"] <= ym:
                can += [x for x in c.get("can_add", []) if x not in can]
                can = [x for x in can if x not in c.get("can_remove", [])]
        return can

    @classmethod
    def from_dict(cls, d: dict) -> "Staff":
        return cls(name=d["name"], unit=d["unit"], can=list(d["can"]),
                   call=d.get("call"), changes=list(d.get("changes", [])))


@dataclass
class Rule:
    """決まりの札。id と版（ver）は作った時から変わらない。"""
    id: str
    ver: int
    name: str
    kind: str            # coverage / count / sequence / max_run / weekday / cell
    flex: str            # fixed / wish / operational / negotiable
    params: dict
    who: list[str] | None = None
    when: list[int] | None = None   # 日の並び（月全体なら None）
    scope: str = "rule"  # rule 毎月 / once 今月だけ
    weight: int | None = None       # 外す時の重み（None なら施設の既定）
    light: bool = False
    meta: dict = field(default_factory=dict)  # 言った言葉・理由・言った人・日時など

    def __post_init__(self):
        if self.flex not in FLEXES:
            raise ValueError(f"flex が不正 {self.flex}")

    def to_dict(self) -> dict:
        return {"id": self.id, "ver": self.ver, "name": self.name, "kind": self.kind,
                "flex": self.flex, "params": self.params, "who": self.who, "when": self.when,
                "scope": self.scope, "weight": self.weight, "light": self.light, "meta": self.meta}

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(id=d["id"], ver=d.get("ver", 1), name=d["name"], kind=d["kind"],
                   flex=d["flex"], params=d.get("params", {}), who=d.get("who"),
                   when=d.get("when"), scope=d.get("scope", "rule"), weight=d.get("weight"),
                   light=d.get("light", False), meta=d.get("meta", {}))


@dataclass
class Month:
    """月の表。前の月の末5日は、夜勤の明けと連勤を月またぎで数えるために持つ。"""
    year: int
    month: int
    holidays: list[int]
    prev_tail: dict[str, list[str]]   # 名前 → 前の月の末5日の記号（古い順）
    table: dict[str, list[str]] | None = None  # 名前 → 1日からの記号
    layers: dict[str, Any] = field(default_factory=dict)  # 勤務とは別の層（待機など）。名前 → {定義, 割り当て}
    rev: int | None = None            # 確定した時の帳面の rev
    built_rev: int | None = None      # 表を作った・直した時の帳面の rev（保存の途中で止まった時の立て直しに使う）
    history: list[dict] = field(default_factory=list)  # 戻す履歴 [{op_id, table, built_rev}]（直す前の表）

    @property
    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    def weekday(self, day: int) -> str:
        return WEEKDAYS[dt.date(self.year, self.month, day).weekday()]

    def is_weekend_or_holiday(self, day: int) -> bool:
        return self.weekday(day) in ("sat", "sun") or day in self.holidays

    def to_dict(self) -> dict:
        return {"year": self.year, "month": self.month, "holidays": self.holidays,
                "prev_tail": self.prev_tail, "table": self.table, "layers": self.layers,
                "rev": self.rev, "built_rev": self.built_rev, "history": self.history}

    @classmethod
    def from_dict(cls, d: dict) -> "Month":
        return cls(year=d["year"], month=d["month"], holidays=list(d.get("holidays", [])),
                   prev_tail={k: list(v) for k, v in d.get("prev_tail", {}).items()},
                   table=d.get("table"), layers=d.get("layers", {}), rev=d.get("rev"),
                   built_rev=d.get("built_rev"), history=list(d.get("history", [])))


@dataclass
class Acceptance:
    """引き受けの記録。変更の帳面とは別の記録で、元の操作の ID（op_id）で tx とつなぐ。

    数えるのは「了承をもらって表を直した回数」だけ。発生日を持つ。
    """
    id: str
    staff: str
    date: str            # 発生日 YYYY-MM-DD
    layer: str           # どの当番で呼ばれたか（例 standby）
    op_id: str           # 表を直した操作の ID
    tx_rev: int | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Acceptance":
        return cls(**d)
