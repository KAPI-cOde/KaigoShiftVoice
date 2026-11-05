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
BOUNDARY = r"(?=さん|さま|くん|ちゃん|[、。,.\s　のはがにもとをへやで]|$)"
HONORIFIC = r"(?:さん|さま|くん|ちゃん|様|氏|君|先生)"
# 「〇〇さん」でも人の名前ではない言葉
NOT_NAMES = {"皆", "利用者", "看護師", "相談員", "新人"}
# 家族の言葉（「お子さんの熱で休み」が名簿にない名前「子」で止まった。2026-11-03 作った施設で本物の一言を測った時）
FAMILY_WORDS = {"子", "孫", "旦那", "主人", "妻", "夫", "嫁", "兄", "姉", "弟", "妹", "祖母", "祖父", "親",
                "母", "父", "お母", "お父", "家族", "息子", "娘", "奥"}
# ひらがなに「ちゃん」「くん」が続いても呼び名でない言葉
KANA_NOT_NAMES = {"あか", "おにい", "おねえ", "にい", "ねえ", "ぼっ", "おじょう", "じょう", "ぼう",
                  "おばあ", "おじい", "ばあ", "じい", "おかあ", "おとう", "かあ", "とう"}
PARTICLES_HEAD = set("はがのにをともへでや")
# 漢字の送り仮名1字＋助詞で、呼び名になりそうにない並び（休み＋を、明け＋は）。ここに無い2字は名前かもしれないので聞き返す
OKURI_PARTICLE = {"みを", "みは", "みも", "みが", "みに", "けは", "けも", "けを", "けが", "けに"}
SAFE_STOP = set("、。,.・ 　（(「」)）!！?？\n")
_NAMEISH = re.compile(r"[一-龥々ァ-ヶA-Za-z]")


def _redact_not_names(t: str) -> str:
    """名前でない語（皆・利用者・家族の言葉など）に呼び方が付いた所で、その前に区切り以外の字（ひらがな・長音など）が
    続く時は、その字ごと「（家族）」「（皆）」などに置き換えてから送る。
    前の字が名前の一部か（「よし子さん」「とお子さん」）、助詞か（「12日はお子さん」）は、字からは見分けられない。
    見分けを当てにいくと、レビューのたびに抜け道が見つかった（失敗の一覧14・18）。置き換えれば、名前の一部だったとしても
    外へは出ず、人が読み取れなければ今どおり聞き返す。前が区切り・数字・呼び方の終わりなら、そのまま送る。"""
    words = sorted(NOT_NAMES | FAMILY_WORDS, key=len, reverse=True)
    pat = re.compile("(" + "|".join(map(re.escape, words)) + ")" + HONORIFIC)
    out, pos = [], 0
    for mm in pat.finditer(t):
        i, w = mm.start(1), mm.group(1)
        if i < pos or (i > 0 and _NAMEISH.fullmatch(t[i - 1])):
            continue   # 前が漢字・カタカナなら、もっと長い名前の一部（漢字＋子の名前）。名簿にない名前として下で止める
        j = i
        while (j > 0 and t[j - 1] not in SAFE_STOP and not t[j - 1].isdigit()
               and not (t[j - 1] == "日" and j > 1 and t[j - 2].isdigit())   # 「12日」の日で止める（日付を食わない）
               and not re.search(HONORIFIC + "$", t[:j])):
            j -= 1
        if j == i:
            continue
        out.append(t[pos:j])
        out.append("（家族）" if w in FAMILY_WORDS else f"（{w}）")
        pos = mm.end()
    out.append(t[pos:])
    return "".join(out)


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
        # 名前でない語の前にひらがななどが続く所は、その字ごと置き換えてから送る（名前の一部でも外へ出さない）。
        # 一覧の全部の語を tests/test_safety.py::test_every_not_name_word_after_hiragana_still_stops が確かめる
        t = _redact_not_names(t)
        unknown = []
        for mm in re.finditer(r"([一-龥々ァ-ヶA-Za-z]{1,6})" + HONORIFIC, t):
            m = mm.group(1)
            if re.fullmatch(r"職員\d{0,2}", m):
                continue
            if m in NOT_NAMES or m in FAMILY_WORDS:
                # 置き換えの後に残った名前でない語は、直前が区切り・数字・呼び方の終わり・文の頭の時だけ通す（閉じる側）
                i = mm.start(1)
                if i == 0 or t[i - 1] in SAFE_STOP or t[i - 1].isdigit() or re.search(HONORIFIC + "$", t[:i]):
                    continue
            unknown.append(m)
        # ひらがなに「ちゃん」「くん」が続く所は呼び名（「あかさかちゃん」「ゆうちゃん」）。名簿の呼び方で伏せきれなかった
        # 物は、Claude へ送らずに聞き返す（2026-11-03 作った施設で本物の一言を測ると、そのまま送っていた）。
        # 「さん」は「たくさん」「みなさん」と見分けられないので当てない（README の既知の限界）
        for mm in re.finditer(r"([ぁ-ゖー]{1,8})(?:ちゃん|くん)", t):
            run = mm.group(1)
            j = mm.start(1)
            after = t[mm.end():mm.end() + 1]
            word = t[mm.end() - 3:mm.end()] if t[mm.end() - 3:mm.end()] == "ちゃん" else t[mm.end() - 2:mm.end()]
            before = t[mm.start(1) - 1] if mm.start(1) > 0 else ""
            # 通すのは決まった言い方だけ（迷ったら聞き返す。名前を送らないため）。「ちゃんと」「くんで」「くんだ」の
            # 前が助詞1字で、その前が文の頭・句読点・漢字・カタカナの時だけ（「シフトをくんで」「、もちゃんと」）
            fixed = (word == "ちゃん" and after == "と") or (word == "くん" and after in ("で", "だ"))
            # 前が助詞1字（その前は文の頭・句読点・漢字・カタカナ）か、漢字の送り仮名1字＋助詞（「休みをちゃんと」）の時だけ
            if fixed and run[-1] in PARTICLES_HEAD and (
                    (len(run) == 1 and (before == "" or before in SAFE_STOP or _NAMEISH.fullmatch(before)))
                    or (run in OKURI_PARTICLE and before and _NAMEISH.fullmatch(before))):
                continue
            while j > 0 and run and run[0] in PARTICLES_HEAD and len(run) > 2:
                run, j = run[1:], j + 1   # 前の助詞（「はゆうちゃん」の「は」）は名前に入れない
            if run not in KANA_NOT_NAMES and run not in unknown:
                unknown.append(run)
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
        """返事（JSON の値）の 職員NN・ユニットN を名前に戻す。知らない番号は残す（手元で聞き返す）。
        名前の無い一言に Claude が聞き返す時、伏せ方の言い方（「職員NN」「職員番号」）を書くことがあるので、
        使う人の言葉（「〇〇さん」「名前」）に直す（2026-11-05、本物の Haiku の返事に出た）。"""
        if isinstance(obj, str):
            s = re.sub(r"職員\d{2}", lambda m: self.to_name.get(m.group(0), m.group(0)), obj)
            for u, code in self.unit_code.items():
                s = s.replace(code, u)
            s = re.sub(r"職員(?:NN|ＮＮ|nn|XX|ＸＸ|xx|○○|〇〇)(?:さん)?", "〇〇さん", s)
            s = re.sub(r"職員(?:番号|ID|ＩＤ)", "名前", s)
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
