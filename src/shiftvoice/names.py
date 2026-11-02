"""名前の置き換え（8章。試験 R9）。

職員は「職員NN」、ユニットは「ユニットN」にして送り、返事を名前に戻す。
- 呼び方は 名前・呼び名（call）・頭2文字以上（他の人と重ならないもの）
- 頭だけの呼び方は、後ろが区切りの時だけ置き換える（日付や行事名の文字のつながりに当たらないように）
- 「ゼロ」は数字の0にする。名前の置き換えの後に行う（先にすると名前の伏せが漏れる）
- 名簿にない「〇〇さん」が残ったら送らない（unknown に入れて、作成者に名簿から選んでもらう）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import Facility, Staff

# 呼び方の後ろに来てよい区切り
BOUNDARY = r"(?=さん|くん|ちゃん|[、。,.\s　のはがにもとをへやで]|$)"
HONORIFIC = r"(?:さん|くん|ちゃん|様|氏|君|先生)"
# 「〇〇さん」でも人の名前ではない言葉
NOT_NAMES = {"皆", "母", "父", "お母", "お父", "家族", "利用者", "看護師", "相談員", "新人", "息子", "娘", "奥"}


@dataclass
class Masker:
    staff: list[Staff]
    facility: Facility
    to_code: dict[str, str] = field(default_factory=dict)    # 名前 → 職員NN
    to_name: dict[str, str] = field(default_factory=dict)    # 職員NN → 名前
    unit_code: dict[str, str] = field(default_factory=dict)  # 東 → ユニット1

    def __post_init__(self):
        for i, s in enumerate(self.staff, 1):
            code = f"職員{i:02d}"
            self.to_code[s.name] = code
            self.to_name[code] = s.name
        for i, u in enumerate(self.facility.units, 1):
            self.unit_code[u] = f"ユニット{i}"

    def _aliases(self) -> list[tuple[str, str, bool]]:
        """（呼び方, 名前, 区切りが要るか）。長い呼び方を先に当てる。"""
        out: list[tuple[str, str, bool]] = []
        for s in self.staff:
            out.append((s.name, s.name, False))
            if s.call:
                out.append((s.call, s.name, True))
            for k in range(2, len(s.name)):
                pre = s.name[:k]
                if sum(o.name.startswith(pre) for o in self.staff) == 1:
                    out.append((pre, s.name, True))
        out.sort(key=lambda a: -len(a[0]))
        return out

    def mask(self, text: str) -> tuple[str, list[str]]:
        """送る文と、名簿にない名前の一覧を返す。"""
        t = text
        for alias, name, need_boundary in self._aliases():
            pat = re.escape(alias) + (BOUNDARY if need_boundary else "")
            t = re.sub(pat, self.to_code[name], t)
        for u, code in self.unit_code.items():
            t = re.sub(re.escape(u) + r"(?=ユニット|フロア|[、。,.\s　のはがにでもとへやを]|$)", code, t)
            t = t.replace(code + "ユニット", code)
        t = t.replace("ゼロ", "0")
        unknown = [m for m in re.findall(r"([一-龥々ァ-ヶA-Za-z]{1,6})" + HONORIFIC, t)
                   if not re.fullmatch(r"職員\d{0,2}", m) and m not in NOT_NAMES]
        return t, unknown

    def unmask(self, obj):
        """返事（JSON の値）の 職員NN・ユニットN を名前に戻す。知らない番号は残す（手元で聞き返す）。"""
        if isinstance(obj, str):
            s = re.sub(r"職員\d{2}", lambda m: self.to_name.get(m.group(0), m.group(0)), obj)
            for u, code in self.unit_code.items():
                s = s.replace(code, u)
            return s
        if isinstance(obj, list):
            return [self.unmask(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        return obj

    def roster_codes(self) -> list[dict]:
        """AI へ渡す名簿（名前も呼び方も入れない）。"""
        return [{"id": self.to_code[s.name], "unit": self.unit_code[s.unit], "can": s.can}
                for s in self.staff]
