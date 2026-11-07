"""名前の置き換え（8章。試験 R9）。

職員は「職員NN」、ユニットは「ユニットN」にして送り、返事を名前に戻す。
- 呼び方は 名前・呼び名（call）・頭2文字以上・下の名前（4字以上の名前の、姓が2字以上残る漢字・かなの終わり）・
  読み（yomi。ひらがな。姓と名の間を空白で区切れば、それぞれの読みでも伏せる）。どれも他の人と重ならないものだけ
- 字体の違い（齋藤・斎藤・斉藤）と、ひらがな・カタカナの違いは同じ名前として伏せる。声の読み取りは名前をかなや別の字体で返すため
- 頭だけ・下の名前・読みの呼び方は、後ろが区切りの時だけ置き換える（日付や行事名の文字のつながりに当たらないように）
- 「ゼロ」は数字の0にする。名前の置き換えの後に行う（先にすると名前の伏せが漏れる）
- 名簿にない「〇〇さん」が残ったら送らない（unknown に入れて、作成者に名簿から選んでもらう）。かなの「〇〇さん」も、
  名前でない言葉（たくさん・みなさん）でなければ送らない。読みの無い名簿では、かなの名前は伏せられないため
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
# ひらがなに「さん」「さま」が続いても名前でない言葉（助詞の後ろで当てる）。それ以外のひらがな＋さんは、名簿の読みで
# 伏せきれなければ名前として聞き返す（声の読み取りは名前をひらがなで返すことがある。読みの無い名前を送らないため）
KANA_SAN_NOT_NAMES = {"たく", "みな", "おく", "おかあ", "おとう", "おばあ", "おじい", "おじ", "おば", "おにい", "おねえ",
                      "かあ", "とう", "ばあ", "じい", "にい", "ねえ", "おつかれ", "ごくろう", "おきゃく", "きゃく",
                      "おいしゃ", "いしゃ", "かんごし", "ぎょう", "よう", "えらい", "おまわり", "ありがと", "おはよう",
                      "おかげ", "ごちそう", "おたがい"}
# 「く」で終わる動詞の漢字。「続くんは」（関西の「続くのは」）の「くん」を呼び方と取らない。名前にもなる字（空・歩・輝・開）は入れない
KU_VERBS = set("続行働動聞書置引付着抜効届欠退除省")
# ひらがなの「く」で終わる動詞の頭（「つづくんは」）。名前の終わりにならない物だけ
KANA_KU_VERBS = ("つづ", "はたら")
# 声の読み取りが取り違えやすい字体。同じ組の字は同じ名前として伏せる（同じ組の名前が名簿に2人いる時は伏せずに聞き返す）
VARIANTS = ["斉斎齋齊", "沢澤", "辺邊邉", "浜濱", "高髙", "崎﨑嵜", "国國", "広廣", "島嶋嶌", "富冨", "徳德", "蔵藏",
            "寿壽", "桜櫻", "竜龍", "恵惠", "真眞", "実實", "曽曾", "槙槇", "条條", "瀬瀨", "柳栁", "園薗", "峰峯", "籠篭"]
_VAR = {c: g for g in VARIANTS for c in g}
# かなの読みは、前が区切りか助詞の時だけ名前とみる（「いちばん」の「ちば」に当てない）
_KANA_PRE = r"(?:(?<![ぁ-ゖァ-ヶー])|(?<=[はがのにをともへでや]))"
# 漢字の下の名前は、前が漢字・カタカナでない時だけ伏せる（「山田花子」の「花子」を名簿の田村花子にしない。残れば聞き返す）
_KANJI_PRE = r"(?<![一-龥々ァ-ヶ])"


def _hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _norm(s: str) -> str:
    """字体とかなの違いをそろえた形（比べるためだけに使う）。"""
    return "".join(_VAR.get(c, c)[0] for c in _hira(s))


def _loose(w: str) -> str:
    """字体の違い・ひらがなとカタカナの違いを許す正規表現。"""
    out = []
    for c in w:
        if c in _VAR:
            out.append(f"[{_VAR[c]}]")
        elif "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヶ":
            h = _hira(c)
            out.append(f"[{h}{chr(ord(h) + 0x60)}]")
        else:
            out.append(re.escape(c))
    return "".join(out)


def _is_kana(w: str) -> bool:
    return bool(w) and all("ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヶ" or c == "ー" for c in w)


def _yomi_words(s: Staff) -> list[str]:
    """読みの形。全体（空白を除く）と、空白で区切った姓と名の読み（2字以上）。"""
    parts = (s.yomi or "").replace("　", " ").split()
    if not parts:
        return []
    return ["".join(parts)] + ([p for p in parts if len(p) >= 2] if len(parts) == 2 else [])


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
        self.width = max(2, len(str(len(self.staff))))   # 番号の桁。100人を超えたら全員3桁にそろえる（職員10と職員100を見分ける）
        for i, s in enumerate(self.staff, 1):
            code = f"職員{i:0{self.width}d}"
            self.to_code[s.name] = code
            self.to_name[code] = s.name
        for i, u in enumerate(self.facility.units, 1):
            self.unit_code[u] = f"ユニット{i}"

    def _aliases(self) -> list[tuple[str, str, bool, bool, str]]:
        """（呼び方, 名前, 区切りが要るか, 字体とかなの違いを許すか, 前の区切りの種類）。長い呼び方を先に当てる。
        前の区切りは ""（見ない）・"yomi"（読み。前がかなでない時だけ）・"given"（漢字の下の名前。前が漢字でない時だけ）。
        読みや字体をそろえると2人に当たる物は、そろえずに当てる（読みは足さない）。"""
        out: list[tuple[str, str, bool, bool, str]] = []
        norms = [_norm(o.name) for o in self.staff]
        # 読みの形ごとの人（全体の読み・姓と名の読み）。2人に当たる読みは使わない
        owners: dict[str, set[str]] = {}
        for o in self.staff:
            for w in _yomi_words(o):
                owners.setdefault(_norm(w), set()).add(o.name)
        fulls = [_norm(_yomi_words(o)[0]) for o in self.staff if o.yomi and _yomi_words(o)]
        for s in self.staff:
            out.append((s.name, s.name, False, norms.count(_norm(s.name)) == 1, ""))
            if s.call:
                out.append((s.call, s.name, True, True, ""))
            for k in range(2, len(s.name)):
                pre = s.name[:k]
                if sum(n.startswith(_norm(pre)) for n in norms) == 1:
                    out.append((pre, s.name, True, True, ""))
                elif sum(o.name.startswith(pre) for o in self.staff) == 1:
                    out.append((pre, s.name, True, False, ""))
            # 下の名前（「田村花子」を「花子は12日休み」と呼ぶ。Astra の指摘）。姓が2字以上残る終わりだけ（「長谷川」の「谷川」は取らない）
            for k in range(2, len(s.name) - 1):
                suf = s.name[-k:]
                if not re.fullmatch(r"[一-龥々ぁ-ゖァ-ヶー]+", suf):
                    continue   # 番号の名前（担当100）の終わり（00）は名前でない
                if sum(n.endswith(_norm(suf)) for n in norms) == 1:
                    out.append((suf, s.name, True, True, "yomi" if _is_kana(suf) else "given"))
            words = _yomi_words(s)
            for w in words:
                if owners.get(_norm(w)) == {s.name}:
                    out.append((w, s.name, True, True, "yomi"))
            if words and owners.get(_norm(words[0])) == {s.name}:
                for k in range(3, len(words[0])):   # 読みの頭（えんどう）。2字の頭は普通の言葉に当たりやすいので3字から
                    if sum(y.startswith(_norm(words[0][:k])) for y in fulls) == 1:
                        out.append((words[0][:k], s.name, True, True, "yomi"))
        out.sort(key=lambda a: -len(a[0]))
        return out

    def _left_pat(self, alias: str, loose: bool, kind: str, boundary: str) -> str:
        """伏せた後に残っていたら送らない形。読みは、前が区切りか後ろが区切りの時だけ（「いちばん」の「ちば」は通す）。"""
        pat = _loose(alias) if loose else re.escape(alias)
        return f"(?:{_KANA_PRE}{pat}|{pat}{boundary})" if kind == "yomi" else pat

    def leaks(self, text: str) -> list[str]:
        """名簿の名前・呼び方・読み（字体やかなの違いも）で、text に残っている物の名前。"""
        out = []
        for alias, name, _, loose, kind in self._aliases():
            if name not in out and re.search(self._left_pat(alias, loose, kind, BOUNDARY), text):
                out.append(name)
        return out

    def mask(self, text: str) -> tuple[str, list[str]]:
        """送る文と、名簿にない名前の一覧を返す。"""
        t = text
        # 区切りには、数字と勤務の記号（夜勤・早番などの頭の字）も入れる。読点の落ちた「はな15日休み」
        # 「岡田夜勤なし」の呼び方・頭を伏せ漏らさないため（レビュー c の指摘）
        syms = "".join(re.escape(x) for x in self.facility.symbols if len(x) == 1)
        boundary = BOUNDARY[:-1] + f"|[0-9０-９一二三四五六七八九十{syms}])"
        aliases = self._aliases()
        for alias, name, need_boundary, loose, kind in aliases:
            # 読みと下の名前は前も区切りの時だけ伏せる（名前でない言葉や別の人の名前の中を番号にしない）
            pat = {"yomi": _KANA_PRE, "given": _KANJI_PRE}.get(kind, "") + \
                (_loose(alias) if loose else re.escape(alias)) + (boundary if need_boundary else "")
            t = re.sub(pat, self.to_code[name], t)
        for u, code in self.unit_code.items():
            t = re.sub(re.escape(u) + r"(?=ユニット|フロア|[、。,.\s　のはがにでもとへやを]|$)", code, t)
            t = t.replace(code + "ユニット", code)
        t = re.sub(rf"(職員\d{{{self.width}}})(?=[0-9０-９])", r"\1 ", t)   # 番号と日付がつながらないように（職員17 15日）
        # 区切りが無くて伏せきれなかった呼び方・名前の頭が残っていたら、送らずに聞き返す（レビュー c の指摘。
        # 「ゆうちゃ来週休み」「遠藤午後休み」のように、後ろに続く言葉の幅は字を足しても塞ぎきれない）
        self.leftover = {}
        for alias, name, need_boundary, loose, kind in aliases:
            # 鍵は一言に書かれたままの形（「チバ」）。名前を選んだ時に、一言のその所を名前に置き換えるため
            if need_boundary and (hit := re.search(self._left_pat(alias, loose, kind, boundary), t)):
                self.leftover.setdefault(hit.group(0), name)
        t = t.replace("ゼロ", "0")
        # 名前でない語の前にひらがななどが続く所は、その字ごと置き換えてから送る（名前の一部でも外へ出さない）。
        # 一覧の全部の語を tests/test_safety.py::test_every_not_name_word_after_hiragana_still_stops が確かめる
        t = _redact_not_names(t)
        unknown = []
        for mm in re.finditer(r"([一-龥々ァ-ヶA-Za-z]{1,6})" + HONORIFIC, t):
            m = mm.group(1)
            if re.fullmatch(r"職員\d{0,3}", m):
                continue
            if mm.group(0)[len(m):] == "くん" and m[-1] in KU_VERBS:
                continue   # 「続くんは」「行くんやったら」（「続くのは」の言い方）
            if m in NOT_NAMES or m in FAMILY_WORDS:
                # 置き換えの後に残った名前でない語は、直前が区切り・数字・呼び方の終わり・文の頭の時だけ通す（閉じる側）
                i = mm.start(1)
                if i == 0 or t[i - 1] in SAFE_STOP or t[i - 1].isdigit() or re.search(HONORIFIC + "$", t[:i]):
                    continue
            unknown.append(m)
        # ひらがなに「ちゃん」「くん」が続く所は呼び名（「あかさかちゃん」「ゆうちゃん」）。名簿の呼び方で伏せきれなかった
        # 物は、Claude へ送らずに聞き返す（2026-11-03 作った施設で本物の一言を測ると、そのまま送っていた）。
        # 「さん」「さま」は下で、名前でない言葉（たくさん・みなさん）の一覧に無い物だけ聞き返す
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
            if word == "くん" and run.endswith(KANA_KU_VERBS):
                continue
            if run not in KANA_NOT_NAMES and run not in unknown:
                unknown.append(run)
        for mm in re.finditer(r"([ぁ-ゖー]{1,8})(?:さん|さま|様|君|氏|先生)", t):
            run = mm.group(1).lstrip("ー")
            if not run or (len(run) == 1 and run in PARTICLES_HEAD):
                continue   # カタカナの言葉の終わり（リーダーさん・ヘルパーさん）と、助詞の後ろの数（夜勤はさんにん）
            # 助詞の後ろ（文の頭も）から先が名前でない言葉なら通す（「夜勤がたくさん」「山田のおくさん」）
            if any((k == 0 or run[k - 1] in PARTICLES_HEAD) and run[k:] in KANA_SAN_NOT_NAMES for k in range(len(run))):
                continue
            if re.search(r"(?:じゅう|ひゃく)$", run):
                continue   # 「じゅうさんにち」の数
            if run not in unknown:
                unknown.append(run)
        return t, list(self.leftover) + unknown

    def suggest(self, unknown: str) -> list[str]:
        """名簿にない名前に近い名簿の名前（手元だけで出す。AI には送らない。13章）。
        字体・かなの違いをそろえて名前・呼び方・読みと同じなら、その人たち（齋藤→斉藤、キクチ→菊池）。
        かなの名前の終わりが読みと同じなら、その人たち（らいげつちば→千葉）。
        どれも無ければ、同じ字数で、同じ位置の字が（2字なら1字、3字以上なら字数−1）合う名前と呼び方。"""
        nu = _norm(unknown)
        same = [s.name for s in self.staff if any(_norm(a) == nu for a in [s.name, s.call, *_yomi_words(s)] if a)]
        if same:
            return same
        if _is_kana(unknown):
            tail = [s.name for s in self.staff if any(nu.endswith(_norm(w)) for w in _yomi_words(s))]
            if tail:
                return tail
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
            s = re.sub(rf"職員\d{{{self.width}}}", lambda m: self.to_name.get(m.group(0), m.group(0)), obj)
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
