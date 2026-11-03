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
# 家族の言葉（「お子さんの熱で休み」が名簿にない名前「子」で止まった。2026-11-03 作った施設で本物の一言を測った時）。
# 前が「お」「ご」か、文の頭・区切りの時だけ名前でないとみなす。「よし子さん」「はな子さん」のように前にひらがなが
# 続く時は名前の一部なので、今どおり名簿にない名前として止める（伏せずに送らないため。自動のセキュリティレビューの指摘）
FAMILY_WORDS = {"子", "孫", "旦那", "主人", "妻", "夫", "嫁", "兄", "姉", "弟", "妹", "祖母", "祖父", "親"}
SEPARATORS = set("、。,.・ 　（(「」)）!！?？")
PARTICLES = set("はがのにをともへでや")


def _hard_boundary(t: str, j: int) -> bool:
    """t[j] の直後で、ひらがなの名前が続いていないと言える所か。文の頭（j < 0）は含めない。
    漢字・カタカナ・数字・句読点の後、または「〇〇さん」などの呼び方の終わり。迷ったら False（止める側）。"""
    if j < 0:
        return False
    c = t[j]
    if c in SEPARATORS or not re.fullmatch(r"[ぁ-ゖ]", c):
        return True
    return bool(re.search(HONORIFIC + "$", t[:j + 1]))


def _not_name_here(t: str, i: int) -> bool:
    """t[i:] から始まる名前でない語（子・母・皆など）が、ここでは名前の一部でないと言えるか（fail closed）。
    （例）12日の後の「皆」、「岡田さんの」の後の「息子」、「12日は」の後の「お子」は名前でない。「よし子さん」「とも子さん」「かの子さん」
    「なお子さん」「とお子さん」は、名前の一部かもしれないので名前でないとは言わない（伏せずに送らない）。"""
    if i == 0 or (t[i - 1] in SEPARATORS) or _hard_boundary(t, i - 1):
        return True                                   # 文の頭・区切り・漢字などの直後
    prev = t[i - 1]
    if prev in "おご":
        if i - 2 < 0 or t[i - 2] in SEPARATORS or _hard_boundary(t, i - 2):
            return True                               # 「お子さん」で始まる・「12日お子さん」
        if t[i - 2] in PARTICLES and _hard_boundary(t, i - 3):
            return True                               # 「12日はお子さん」（助詞の前が区切り）
        return False
    if prev in PARTICLES and _hard_boundary(t, i - 2):
        return True                                   # 呼び方の後の助詞に続く語（岡田さんの＋息子）
    return False


@dataclass
class Masker:
    staff: list[Staff]
    facility: Facility
    to_code: dict[str, str] = field(default_factory=dict)    # 名前 → 職員NN
    to_name: dict[str, str] = field(default_factory=dict)    # 職員NN → 名前
    unit_code: dict[str, str] = field(default_factory=dict)  # 東 → ユニット1
    leftover: dict[str, str] = field(default_factory=dict)   # 伏せきれなかった呼び方・頭 → 名前（直前の mask の結果）

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
        # 区切りには、数字と勤務の記号（夜勤・早番などの頭の字）も入れる。読点の落ちた「はな15日休み」
        # 「岡田夜勤なし」の呼び方・頭を伏せ漏らさないため（レビュー c の指摘）
        syms = "".join(re.escape(x) for x in self.facility.symbols if len(x) == 1)
        boundary = BOUNDARY[:-1] + f"|[0-9０-９一二三四五六七八九十{syms}])"
        for alias, name, need_boundary in self._aliases():
            pat = re.escape(alias) + (boundary if need_boundary else "")
            t = re.sub(pat, self.to_code[name], t)
        for u, code in self.unit_code.items():
            t = re.sub(re.escape(u) + r"(?=ユニット|フロア|[、。,.\s　のはがにでもとへやを]|$)", code, t)
            t = t.replace(code + "ユニット", code)
        t = re.sub(r"(職員\d{2})(?=[0-9０-９])", r"\1 ", t)   # 番号と日付がつながらないように（職員17 15日）
        # 区切りが無くて伏せきれなかった呼び方・名前の頭が残っていたら、送らずに聞き返す（レビュー c の指摘。
        # 「ゆうちゃ来週休み」「遠藤午後休み」のように、後ろに続く言葉の幅は字を足しても塞ぎきれない）
        self.leftover = {}
        for alias, name, need_boundary in self._aliases():
            if need_boundary and alias in t and alias not in self.leftover:
                self.leftover[alias] = name
        t = t.replace("ゼロ", "0")
        unknown = []
        for mm in re.finditer(r"([一-龥々ァ-ヶA-Za-z]{1,6})" + HONORIFIC, t):
            m = mm.group(1)
            if re.fullmatch(r"職員\d{0,2}", m):
                continue
            # 名前でない語も家族の言葉も、前が区切りの時だけ名前でないとみなす（前がひらがなの名前なら名前の一部。
            # 一覧の全部の語を tests/test_safety.py::test_every_not_name_word_after_hiragana_still_stops が確かめる）
            if (m in FAMILY_WORDS or m in NOT_NAMES) and _not_name_here(t, mm.start(1)):
                continue
            unknown.append(m)
        return t, list(self.leftover) + unknown

    def suggest(self, unknown: str) -> list[str]:
        """名簿にない名前に近い名簿の名前（手元だけで出す。AI には送らない。13章）。
        同じ字数で、同じ位置の字が（2字なら1字、3字以上なら字数−1）合う名前と呼び方。"""
        out: list[str] = []
        for s in self.staff:
            for a in [s.name] + ([s.call] if s.call else []):
                need = 1 if len(a) <= 2 else len(a) - 1
                if len(a) == len(unknown) and sum(x == y for x, y in zip(a, unknown)) >= need:
                    out.append(s.name)
                    break
        return out

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
