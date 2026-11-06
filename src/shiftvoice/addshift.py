"""話して足す勤務の種類（「8時から17時の日8を足して」）。

足す一言は Claude へ送らず、手元で読む。本物の Haiku は「8時から17時の日8を足して」を5回に1回「日勤を8人」の人数の
ルールと読み、残りも型に無い頼み（custom）だった（2026-11-06）。読み違いが施設の形に入るので、言い方を狭く決めて手元で読む。
- 見分けは一言の全文で見る。「早番を足して」「10日に夜勤を足して」「日勤を1人足して」のように、今ある勤務を人や日に
  足す一言は、ここで読まずに今までどおり読み取りへ送る
- 足した勤務は「頼まれた時だけ置く」。全員が入れて、今ある勤務の人数には数えない
- 日をまたぐ勤務は足さない。夜勤の翌日が明けという並びは帳面のルールが「夜」の記号で決めていて、新しい記号には
  効かない。足すと翌朝の早番と重なる表が作れてしまう

続きの一言（「10日の小島さん、日8に」）は、足した勤務の名前を「勤務A」に伏せ、1往復の例を添えて送る。読み取りの指示文は
変えない（1行足しただけで別の一言の読みが5回中5回から2回に落ちたことがある）。足した勤務を言っていない一言の送る中身は
前と同じになる。そのまま送ると「日8」を5回とも日勤と読み、「平日は日8を1人」は日勤8人と読んだ。名前のまま例を添えても
4回に1回は日勤に落ち、「勤務A」＋例は11の言い方の44回すべてで読めた（2026-11-06）。
"""
from __future__ import annotations

import json
import re
import unicodedata

_MARKS = re.compile(r"[\s、。，．,!！?？…・「」『』]")
_AT = r"(午前|午後|朝|昼|夕方|夕|夜|晩)?(\d{1,2})(?:時(半|\d{1,2}分)?|:(\d{2}))"
_LENGTH = re.compile(rf"(?<![0-9]){_AT}から(\d{{1,2}})時間(半)?")
_RANGE = re.compile(rf"(?<![0-9]){_AT}(?:から|より|[〜~ー-])(翌日?の?|次の日の?)?{_AT}(?:まで)?")
_HALF_SAID = re.compile(r"(?<![0-9])\d{1,2}時半?(?:から|まで)?の?")   # 片方の時刻だけ。足さずに聞き返す
_ANSWER_TAIL = re.compile(r"(?:まで|です|で|ね|よ|でお願いします|お願いします)*")

# 人・日・曜日・期間の話が入る一言は、新しい勤務の話ではない
_NOT_ADD = re.compile(r"職員\d|ユニット\d|(?<![0-9])\d{1,2}日|\d+[人回]|\d+名(?!前)|曜|平日|土日|週末|祝|毎日|毎週|毎月|今月|来月|"
                      r"取り消|やめ|いらな|外し|減ら")
_KIND = r"(?:勤務|シフト)"
_VERB = (r"(?:新しく|新規で?|新規に)?(?:足(?:す|して|したい|します|しといて|しておいて)|追加(?:する|して|したい|します|で)?|"
         r"作(?:る|って|りたい|ります|っておいて)|作成(?:する|して|したい|します)?)")
_POLITE = r"(?:ください|下さい|ほしい|欲しい|お願いします|おねがいします|もらえる|もらえますか|くれる|くれますか|ね|よ)*"
_SAID = re.compile(rf"(?:新しい)?(?P<h>{_KIND}(?:の|に|で|として))?(?:新しい)?(?P<n>[一-龥々ァ-ヺーA-Za-z0-9]{{1,6}}?)"
                   rf"(?P<l>っていう|て言う|という|って)?の?(?P<k>(?:新しい)?{_KIND})?(?:は|を|も|が)?(?:まで)?(?:で|の)?"
                   rf"(?P<k2>{_KIND}を?)?{_VERB}{_POLITE}(?:時間は|時間が|時刻は)?(?:まで|です|で)*")
# 名前を別の節で言う形（「8時から17時の勤務を新しく作って。名前は日8」「日8という名前で作って」）。前は読めずに組み立て役へ行き、
# 無い画面を案内する断りが返った（2026-11-06 AI のテストプレイで4回中4回）。「勤務、日8を作って」は時刻がある時だけ
_NAME_CH = r"[一-龥々ァ-ヺーA-Za-z0-9]"
_CALLED = re.compile(rf"(?:名前|名称|呼び名|勤務名|シフト名)(?:は|が|を)(?P<a>{_NAME_CH}{{1,6}})|"
                     rf"(?P<b>{_NAME_CH}{{1,6}})(?:っていう|て言う|という|って|の)?(?:名前|名称)(?:で|に)")
_AFTER_KIND = re.compile(rf"(?:新しい)?{_KIND}を?(?P<a>{_NAME_CH}{{1,6}}?)(?:を|で)?{_VERB}")
_NEW_KIND = re.compile(rf"新しい{_KIND}|{_KIND}を?新しく|{_KIND}を?新規")
_ONCALL = re.compile(r"当番|待機|呼び出し|オンコール")
# 勤務の名前にならない言葉（人・当番・休みの話は、今までどおり読み取りへ）
_NOT_NAME = re.compile(r"勤務|シフト|職員|人|当番|待機|呼び出し|オンコール|ルール|休|有給|希望|残業|看護|介護|パート|応援|ヘルプ|"
                       r"スタッフ")
# 「勤務」の言葉が無くても新しい勤務の名前と読む形（日8・A勤・中番）。時刻も無い時はこの形だけ聞き返す
_NAME_SHAPE = re.compile(r"[一-龥ァ-ヺA-Za-z]{1,3}\d{1,2}|[一-龥ァ-ヺA-Za-z]{1,3}(?:番|勤)")
MAX_NAME = 4   # 表のマス・印刷の幅に入る字数

UNCHANGED = "足していません"


def tight(text: str) -> str:
    return _MARKS.sub("", unicodedata.normalize("NFKC", text))


def _minutes(pre: str | None, hour: str, tail: str | None, colon: str | None) -> int | None:
    h, m = int(hour), int(colon or 0)
    if tail == "半":
        m = 30
    elif tail:
        m = int(tail[:-1])
    if pre in ("午後", "夕方", "夕", "夜", "晩") and h < 12 or pre == "昼" and h <= 5:
        h += 12
    return h * 60 + m if h <= 24 and m < 60 else None


def read_times(t: str) -> dict | None:
    """一言（tight の後）の中の時刻の範囲。{"span": 文の中の位置, "start": 分, "end": 分, "next_day": 翌と言ったか, "am_pm":
    終わりの時刻に午前・午後の言葉があるか}。無ければ None。読めない時刻は start が None。"""
    m = _LENGTH.search(t)
    if m:
        a = _minutes(*m.groups()[:4])
        n = int(m.group(5)) * 60 + (30 if m.group(6) else 0)
        return {"span": m.span(), "start": a, "end": None if a is None else a + n, "next_day": False, "am_pm": True}
    m = _RANGE.search(t)
    if not m:
        return None
    g = m.groups()
    return {"span": m.span(), "start": _minutes(*g[0:4]), "end": _minutes(*g[5:9]), "next_day": bool(g[4]),
            "am_pm": bool(g[5])}


def clock(minutes: int) -> str:
    """分を「8時」「8時半」「8時15分」に。"""
    h, m = divmod(minutes, 60)
    return f"{h}時" + ("" if not m else "半" if m == 30 else f"{m}分")


def said_times(st) -> str:
    """勤務の種類（dict でも ShiftType でも）の時刻を「8時〜17時」に。"""
    start = st["start"] if isinstance(st, dict) else st.start
    hours = st["hours"] if isinstance(st, dict) else st.hours
    a = int(start[:2]) * 60 + int(start[3:])
    return f"{clock(a)}〜{clock(a + int(hours * 60))}"


def _item(name: str, times: dict) -> dict:
    a, b = times["start"], times["end"]
    if a is None or b is None:
        return {"type": "unclear", "question": f"時刻が読めませんでした。{name}は何時から何時までですか。{UNCHANGED}", "ask_times": name}
    if b > 24 * 60 or b <= a or times["next_day"]:
        if b <= 12 * 60 and a < 12 * 60 and a < b + 12 * 60 and not times["am_pm"] and not times["next_day"]:
            return {"type": "unclear", "ask_times": name,
                    "question": f"{clock(a)}から{clock(b + 12 * 60)}までですか。「{clock(a)}から{clock(b + 12 * 60)}」のように言ってください。{UNCHANGED}"}
        return {"type": "unclear", "ask_times": name,
                "question": f"日をまたぐ勤務は、まだ一言で足せません（次の日を明けにするルールが新しい勤務に効かないため）。"
                            f"{name}は日をまたがない時刻で何時から何時までですか。{UNCHANGED}"}
    hours = (b - a) / 60
    return {"type": "add_shift", "name": name, "start": f"{a // 60:02d}:{a % 60:02d}",
            "hours": int(hours) if hours == int(hours) else hours}


def add_shift_items(masked: str, facility, waiting: str | None = None) -> list[dict] | None:
    """伏せた後の一言が新しい勤務を足す頼みなら、読み取り結果の形（add_shift か、聞き返しの unclear）を手元で作る。違えば None。
    waiting は、前の一言で「何時から何時までですか」と聞いた勤務の名前（答えは時刻だけの一言になる）。"""
    t = tight(masked)
    times = read_times(t)
    if waiting and times:
        a, b = times["span"]
        if not t[:a] and _ANSWER_TAIL.fullmatch(t[b:]):
            return [_item(waiting, times)]
    if _NOT_ADD.search(t) or _ONCALL.search(t):
        return None
    rest = t[:times["span"][0]] + t[times["span"][1]:] if times else _HALF_SAID.sub("", t)
    m = _SAID.fullmatch(re.sub(r"^(?:の|で|は|に|が)+", "", rest))
    got = _named(m["n"], times, bool(m["h"] or m["l"] or m["k"] or m["k2"]), facility) if m else None
    if got is not None:
        return got
    said = _said_apart(re.sub(r"時間(?:は|が)|時刻は", "", rest), times)
    if said is None:
        return None
    return _named(said, times, True, facility) if said else _no_name(times)


def _said_apart(rest: str, times: dict | None) -> str | None:
    """全文の形（_SAID）に入らない、新しい勤務の頼み。名前（無ければ空）を返す。新しい勤務の頼みでなければ None。"""
    want = re.search(_VERB, rest) or _NEW_KIND.search(rest)
    m = _CALLED.search(rest)
    if m and want:
        return m["a"] or m["b"]
    if not times or not re.search(_KIND, rest) or not want:
        return "" if want and _NEW_KIND.search(rest) else None
    m = _AFTER_KIND.search(rest)
    return m["a"] if m and _NAME_SHAPE.fullmatch(_kanji_tail(m["a"])) else ""


def _no_name(times: dict | None) -> list[dict]:
    """名前を言っていない時は聞く。時刻を言っていれば、答えの名前で足す（session が次の一言を答えと読む）。"""
    if not times:
        return [{"type": "unclear", "question": f"新しい勤務の名前と時刻を、「8時から17時の日8を足して」のように言ってください。{UNCHANGED}"}]
    return [{"type": "unclear", "question": f"新しい勤務の名前は何にしますか。「日8」のように言ってください。{UNCHANGED}",
             "ask_more": {"need": "shift_name", "times": times}}]


def _kanji_tail(name: str) -> str:
    """名前の終わりの漢数字を数字にする（「日八」→「日8」。表のマスでそろえ、声の直しとも合わせる）。"""
    return re.sub(r"(?<=.)[〇一二三四五六七八九]$", lambda m: str("〇一二三四五六七八九".index(m[0])), name)


def _named(name: str, times: dict | None, marked: bool, facility) -> list[dict] | None:
    """言われた名前を確かめて、足す形か聞き返しにする。勤務の名前にならなければ None。"""
    name = _kanji_tail(name)
    if name[0].isdigit() or _NOT_NAME.search(name):
        return None
    if any(name in (st.symbol, st.name) for st in facility.shift_types):
        # 「早番を足して」は今ある勤務を足す頼み（読み取りへ）。時刻や「勤務」の言葉がある時だけ、もうあると言う
        return [{"type": "unclear", "question": f"{name}はもうある勤務です。{UNCHANGED}"}] if times or marked else None
    if not times and not marked and not _NAME_SHAPE.fullmatch(name):
        return None
    if len(name) < 2 or len(name) > MAX_NAME:
        return [{"type": "unclear", "question": f"表のマスに入るよう、勤務の名前は2字から{MAX_NAME}字で言ってください（日8、中番など）。{UNCHANGED}"}]
    if not times:
        return [{"type": "unclear", "question": f"{name}は何時から何時までですか。「8時から17時」のように言ってください。{UNCHANGED}",
                 "ask_times": name}]
    return [_item(name, times)]


# 続きの一言。足した勤務の名前を「勤務A」に伏せて送り、返事を戻す
_ZEN = str.maketrans("０１２３４５６７８９", "0123456789")


PLAIN_WORK = {"早", "日", "遅", "夜", "研"}   # Claude が知っている勤務。ほかの勤務の名前は読み手に伝わらない


def spoken_shifts(facility) -> list:
    """話して足した勤務と、取り込みで答えた施設の勤務（日8 など）。取り込んだ勤務もそのまま送ると「日8」を日勤と読み、
    入れない勤務の頼みとして断っていた（2026-11-06 通しの試し）。"""
    return [st for st in facility.shift_types if st.spoken or (st.kind == "work" and st.symbol not in PLAIN_WORK)]


def _words(st) -> set[str]:
    """一言の中で当てる言い方。1字の漢字の記号は、ふつうの言葉の中の字と見分けられないので当てない。"""
    return {st.name} | ({st.symbol} if len(st.symbol) >= 2 or st.symbol.isascii() or "\u2460" <= st.symbol <= "\u2473" else set())


def hide_spoken(masked: str, facility) -> tuple[str, dict[str, str]]:
    """一言の中の足した勤務の名前を「勤務A」に置き換える。返すのは送る文と、{勤務A: 記号}（言っていなければ文はそのまま）。"""
    t = masked.translate(_ZEN)
    tokens: dict[str, str] = {}
    by_word = {w: (f"勤務{chr(ord('A') + i % 26)}", st.symbol)
               for i, st in enumerate(spoken_shifts(facility)) for w in _words(st)}
    if not by_word:
        return masked, {}
    words = "|".join(re.escape(w) for w in sorted(by_word, key=len, reverse=True))   # 長い名前を先に当てる

    def put(m: re.Match) -> str:
        token, sym = by_word[m.group(0)]
        tokens[token] = sym
        return token

    t = re.sub(rf"(?<![0-9A-Za-z])(?:{words})(?![0-9])", put, t)   # 1回で置き換える（「勤務A」の A を当て直さない）
    return (t, tokens) if tokens else (masked, {})


def spoken_example(tokens: dict[str, str]) -> list[dict] | None:
    """足した勤務が勤務の記号として読めるよう、読み取りへ添える1往復の例（指示文は変えない）。"""
    if not tokens:
        return None
    token = next(iter(tokens))
    reply = {"items": [{"type": "set_cell", "staff": "職員01", "days": [3], "shift": token, "strength": "wish", "reason": None}]}
    return [{"role": "user", "content": f"一言「職員01さん、3日は{token}で」"},
            {"role": "assistant", "content": json.dumps(reply, ensure_ascii=False)}]


def show_spoken(obj, tokens: dict[str, str]):
    """返事の「勤務A」を足した勤務の記号に戻す。"""
    if not tokens:
        return obj
    if isinstance(obj, str):
        for token, sym in tokens.items():
            obj = obj.replace(token, sym)
        return obj
    if isinstance(obj, list):
        return [show_spoken(x, tokens) for x in obj]
    if isinstance(obj, dict):
        return {k: show_spoken(v, tokens) for k, v in obj.items()}
    return obj


def read_spoken(reader, sent: str, tokens: dict[str, str]) -> list[dict]:
    """伏せた文を読み手に渡し、返事を戻す。足した勤務を言っていない一言の呼び方は前と同じ。"""
    return show_spoken(reader.read(sent, spoken_example(tokens)), tokens) if tokens else reader.read(sent)
