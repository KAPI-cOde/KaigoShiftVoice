"""読み取りの型を、帳面に足す決まり・職員情報の変更にする。ここで返事を確かめる（AI は候補まで）。

一言は3種類に分かれる（spec.md 画面1）
- cells 今月だけの頼み（日付つき）。マスだけが変わり、札にしない。帳面には今月だけ（once）で入れる
- rule  決まり。札が1枚増える。毎月か今月だけかを言っていなければ聞く（期限つきは聞かない）
- staff 職員の情報。名簿の行に印。言われた月から効く
ほかに、すでに起きた穴埋め（残業・早出など）の報告 cover がある。表も帳面も変えず、引き受けの記録に足す候補にする
"""
from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass, field

from .model import WEEKDAYS, Facility, Month, Staff
from .period import read_period

DATE_TYPES = ("day_off", "set_cell", "avoid_cell", "move", "swap")
NOT_YET = ("pair_apart", "rest_after_night", "around_off", "weigh",
           "explain", "custom")
FLEX_OK = ("fixed", "operational", "wish", "negotiable")


@dataclass
class Proposal:
    kind: str                          # cells / rule / staff / cancel
    rules: list[dict] = field(default_factory=list)   # id なしの決まり（帳面に足す時に id を振る）
    staff_changes: list[dict] = field(default_factory=list)
    removes: list[dict] = field(default_factory=list)  # 取り消す頼み（人・日・勤務）。当てる先は帳面で探す
    scope: str | None = None           # rule / once。None は未決（聞く）
    until: str | None = None           # YYYY-MM-DD
    reason: str | None = None
    summary: str = ""
    replace_from: list[str] | None = None   # 言い直し（「AさんじゃなくてBさん」）で、前の頼みを外すかもしれない人
    covers: list[dict] = field(default_factory=list)   # 穴埋めの記録の候補 {staff, day（言っていなければ None）, how}


class Clarify(Exception):
    """聞き返す（表も帳面も変えない）。"""


# 名前の無い一言（「私、13日の日勤、有給に」）に、Claude が人の欄へ入れることがある埋め草（2026-11-05、本物の Haiku で
# 「職員不明」）。名簿にない人ではなく、誰の頼みか分からないと聞く
# 回数の上限の頼みで「今の分は残して、これ以上増やさない」と言っている言葉。外す・0回と言っている時は使わない
# （「今入っている夜勤も全部外して」「夜勤は0回、日勤はそのまま」。Codex Astra のレビュー 2026-11-05）
KEEP_NOW = r"追加しない|追加せず|増やさない|増やさず|そのまま|今入って|今の分"
REMOVE_WORDS = r"外し|外す|はずし|はずす|0回|０回|ゼロ|なし|無し|抜い|抜く|消し|消す|全部"
NOBODY = re.compile(r"^(職員)?(不明|未定|未指定|NN|ＮＮ|XX|ＸＸ|〇〇|○○)(さん)?$|^(本人|自分|私|わたし|僕|俺|話者|依頼者)$")
WHO_UNKNOWN = "誰の頼みか読み取れませんでした。表もルールも変えていません。名前を入れて、もう一度言ってください"


def _names(v, staff_names: set[str]) -> list[str] | None:
    if v is None:
        return None
    vs = [v] if isinstance(v, str) else list(v)
    bad = [x for x in vs if x not in staff_names]
    if bad and all(NOBODY.match(str(x).strip()) for x in bad):
        raise Clarify(WHO_UNKNOWN)
    if bad:
        raise Clarify(f"名簿にない人がいます（{'・'.join(bad)}）。名簿から選んでください")
    return vs


def _days(v, month: Month) -> list[int]:
    try:
        ds = [int(d) for d in (v if isinstance(v, list) else [v])]
    except (TypeError, ValueError):
        raise Clarify("日付が読めませんでした。何日か教えてください")
    if not ds or any(d < 1 or d > month.days for d in ds):
        raise Clarify(f"{month.month}月にない日付です。何日か教えてください")
    return ds


NO_LEAVE = "この施設には有給の記号がありません。表もルールも変えていません"


def _sym(v, facility: Facility) -> str:
    if v not in facility.symbols:
        if v in ("有", "有給") and _leave_sym(facility) is None:
            raise Clarify(NO_LEAVE)   # 取り込んで作った施設には、有給の記号が無いことがある
        raise Clarify(f"勤務が読めませんでした（{v}）")
    return v


def _num(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or int(v) != v:
        raise Clarify("回数が読めませんでした。何回か教えてください")
    return int(v)


def _flex(v, default: str) -> str:
    return v if v in FLEX_OK else default


def _who(names: list[str] | None) -> str:
    return "・".join(f"{n}さん" for n in names) if names else "全員"


NOBODY_COVER = "誰が埋めてくれたか読み取れませんでした。表も記録も変えていません。名前を入れて、もう一度言ってください"


def _how(v) -> str:
    """埋め方を短い名前にそろえる（引き受けの記録の layer に入れ、画面に「残業で穴埋め」と出す）。"""
    s = str(v or "").strip()
    if "早" in s:
        return "早出"
    if not s or "残" in s or "延長" in s:
        return "残業"
    return s[:8]


def _leave_sym(facility: Facility) -> str | None:
    """有給の記号。施設の勤務の種類に無ければ None。"""
    return next((st.symbol for st in facility.shift_types if st.kind == "leave"), None)


FIXED_WORDS = ("必ず", "かならず", "絶対", "ぜったい", "どうしても", "外せない", "動かせない")
MAYBE_WORDS = ("できれば", "出来れば", "できるだけ", "出来るだけ", "なるべく", "可能なら", "できたら", "出来たら")
CARE_WORDS = ("きつい", "あげて")      # 配慮の言い方（指示文の negotiable の決め）
OWN_WORDS = ("希望", "本人")


def _cell_strength(v, text: str, t: str) -> str:
    """休み・勤務の指定の強さ。読み取りの値を基本にし、ぶれやすい向きだけ一言の言葉で直す。
    - 休みの fixed は、一言に必ず系の言葉がある時だけ（「法事で休み」を動かせない扱いにしない）
    - negotiable は、できれば系か配慮の言葉がある時だけ（「日勤だと助かる」を T2-08 の流れに入れない）"""
    v = _flex(v, "wish")
    if v == "fixed" and t == "day_off" and not any(w in text for w in FIXED_WORDS):
        return "wish"
    if v == "negotiable" and not any(w in text for w in MAYBE_WORDS + CARE_WORDS):
        return "wish"
    return v


def _count_strength(v, text: str, single: bool) -> str:
    """回数の強さ。必ず系の言葉があれば fixed（件が1つの時だけ）。本人の希望・できればと言った時は読み取りの値を残す。
    ほかは施設の運用（operational）にそろえる（「今月は夜勤0で」が wish だと全員向けの下限に負ける）。"""
    if single and any(w in text for w in FIXED_WORDS):
        return "fixed"
    if v in ("wish", "negotiable") and any(w in text for w in OWN_WORDS + MAYBE_WORDS):
        return v
    return "operational"


LEAVE_WORDS = ("有給", "有休", "年休")
_LEAVE_DAY = re.compile(r"(?<![0-9月])([0-9]{1,2})日(?:は|に|、|,|で|の)")


def _tidy_leave(items: list[dict], text: str) -> list[dict]:
    """日付を言った有給の一言を、その日の有給のマスにそろえる（本物の Haiku は「職員02さん、有給で20日休み」を
    休み（day_off）・有給の日数（leave_count）・その両方のどれにも読み、両方だと「頼みが混ざっています」で止まった。
    2026-11-03、同じ一言で呼ぶたびに読みが変わるのを確かめた）。
    - 有給の言葉がある一言の day_off（日付あり）は、その日を有給にする set_cell にする
    - 同じ人の日付つきの頼みがあれば、日数の無い（か同じ日数の）leave_count は写しとして落とす
    - leave_count だけで日数が無く、一言に「20日は」「20日に」のような日付が1つだけある時は、その日の有給にする"""
    if not any(w in text for w in LEAVE_WORDS):
        return items
    if re.search(r"(有給|有休|年休)(じゃ|では)な", text):
        return items   # 「有給じゃなくて公休で」は有給にしない
    def staff_key(v):
        return tuple(v) if isinstance(v, list) else (v,)
    offs = [it for it in items if it.get("type") == "day_off" and it.get("days")]
    out = []
    for it in items:
        # 有給にするのは、休みの頼みが1つだけの時か、その頼みの理由に有給の言葉がある時だけ
        # （「青木さん20日休み、石田さんは有給で21日」で青木さんまで有給になっていた。敵対レビュー 2026-11-03）
        if it in offs and (len(offs) == 1 or any(w in str(it.get("reason") or "") for w in LEAVE_WORDS)):
            it = {**it, "type": "set_cell", "shift": "有"}
        out.append(it)
    dated = {staff_key(it.get("staff")): len(it.get("days") or []) for it in out
             if it.get("type") == "set_cell" and it.get("shift") == "有" and it.get("days")}
    res = []
    for it in out:
        if it.get("type") == "leave_count":
            k = staff_key(it.get("staff"))
            if k in dated and it.get("count") in (None, dated[k]):
                continue
            days = sorted({int(x) for x in _LEAVE_DAY.findall(text)})
            if it.get("count") is None and len(days) == 1 and not dated:
                it = {"type": "set_cell", "staff": it.get("staff"), "days": days, "shift": "有",
                      "reason": it.get("reason")}
        res.append(it)
    return res


def _tidy(items: list[dict], month: Month, text: str = "") -> list[dict]:
    """読み取りのぶれを手元で直す。
    - 曜日の頼みと一緒に出た、日付の無い日付の型は落とす（「木曜は早番にしないで」が avoid_cell と weekday_pref の2件になる）
    - move の元の日が無ければ、表でその人のその勤務が1日だけの時に補う（「研修、13日に変わった」）"""
    def key(v):
        return tuple(v) if isinstance(v, list) else (v,)
    wd = {(key(it.get("staff")), it.get("shift")) for it in items if it.get("type") == "weekday_pref"}
    out = []
    for it in items:
        t = it.get("type")
        if t in ("set_cell", "avoid_cell", "day_off") and not it.get("days") and \
                (key(it.get("staff")), "公" if t == "day_off" else it.get("shift")) in wd:
            continue   # 同じ人・同じ勤務の曜日の頼みの写し
        if t == "move" and it.get("from_day") is None and month.table:
            st = it.get("staff")
            st = st[0] if isinstance(st, list) and len(st) == 1 else st
            row = month.table.get(st) if isinstance(st, str) else None
            hits = [i + 1 for i, x in enumerate(row or []) if x == it.get("shift") and i + 1 != it.get("to_day")]
            if len(hits) == 1:
                it = {**it, "from_day": hits[0]}
        out.append(it)
    return out


# 移す先の無い「移す」を、その日の勤務の変更と読んでよい言葉（「早番に変えて」「日勤になった」「研修じゃなくて」）
CHANGE_WORDS = r"に変え|に替え|にし|になっ|になる|に直|に動か|に回|にでき|じゃなく|ではなく|でなく"


def _targets(text: str, facility: Facility, not_this: str | None) -> set[str]:
    """一言の中で「〇〇に」「〇〇へ」「〇〇で」と、変える先として言った勤務（すぐ後に打ち消しがある物は除く。
    「早番にはしないで」の早番は、変える先ではない）。移す元の勤務と、ほかの勤務の名前の一部になる名前は数えない。"""
    out = set()
    for t in facility.shift_types:
        if not t.name or not_this in (t.symbol, t.name):
            continue
        if any(o.name != t.name and t.name in o.name and o.name in text for o in facility.shift_types):
            continue
        for m in re.finditer(re.escape(t.name) + r"(?:に|へ|で)", text):
            if not re.search(r"ない|なく|ません|やめ", text[m.end():m.end() + 6]):
                out.add(t.symbol)
    return out


def _same_day_move(items: list[dict], text: str, facility: Facility, names: set[str] | None = None) -> list[dict]:
    """「14日の遅番、早番に変えて」「11日、研修じゃなくて日勤になった」を、移す先の日が無いか元と同じ日の「移す」
    （研修の日を動かす形）と読むことがある（本物の Haiku、2026-11-05。同じ一言で3回に2回）。元と先が同じでは
    「14日は遅番でない」と「14日は遅番」がぶつかって表が作れず、先の日が無いと「日付が読めませんでした」になっていた。
    - 同じ人・同じ日をその勤務にする別の頼み（「14日 早番」など）があれば、移すだけを捨てる（「14日は早番にしない」は
      移す先を決めないので数えない）
    - 無ければ、一言で変える先として言った勤務が1つだけで、一言に出てくる人が1人以下の時だけ、その日をその勤務にする。
      先の日が無い時は、変えると読める言葉（「に変えて」「じゃなくて」など）がある時だけ
    - それでも分からなければ聞く（元と先が同じ日なら、どの勤務にするか。先の日が無ければ、何日に移すか）。
      別の人の勤務や打ち消した勤務を拾って黙って直さない（Codex Astra のレビュー 2026-11-05）"""
    def key(v):
        return tuple(v) if isinstance(v, list) else (v,)
    out = []
    people = sum(1 for n in (names or ()) if n and n in text)
    for it in items:
        d = it.get("from_day")
        if it.get("type") != "move" or d is None or it.get("to_day") not in (None, d):
            out.append(it)
            continue
        st = it.get("staff")
        if any(x is not it and x.get("type") in ("set_cell", "day_off")
               and key(x.get("staff")) == key(st) and d in (x.get("days") or []) for x in items):
            continue
        same_day = it.get("to_day") == d
        said = _targets(text, facility, it.get("shift"))
        if len(said) == 1 and people <= 1 and (same_day or re.search(CHANGE_WORDS, text)):
            out.append({"type": "set_cell", "staff": st, "days": [d], "shift": said.pop(), "strength": "wish",
                        "reason": it.get("reason")})
            continue
        one = st[0] if isinstance(st, list) and len(st) == 1 else st
        who = f"{one}さんの" if isinstance(one, str) and one else ""
        if same_day:
            q = f"{who}{d}日を、どの勤務にしますか。表もルールも変えていません"
        else:
            sym = it.get("shift")
            sname = next((t.name for t in facility.shift_types if sym in (t.symbol, t.name)), "勤務")
            q = f"{who}{d}日の{sname}を、何日に移しますか。表もルールも変えていません"
        out.append({"type": "unclear", "question": q})
    return out


REPHRASE_WORDS = ("じゃなくて", "じゃなく", "ではなくて", "ではなく", "じゃない", "でなくて")
EVERY_WEEK = ("固定", "毎週", "いつも", "ずっと", "決まって", "定休")
ONE_WEEK = ("今度", "次の", "来週", "今週", "再来週", "この", "その", "第")
_WD_ONE = {"月": "mon", "火": "tue", "水": "wed", "木": "thu", "金": "fri", "土": "sat", "日": "sun"}


def _said_weekdays(text: str) -> list[str] | None:
    """一言が言った曜日（「日曜」「土日」「平日」）。祝日を言った時と、読めない時は None。"""
    if "祝" in text:
        return None
    out: list[str] = []
    if "平日" in text:
        out += ["mon", "tue", "wed", "thu", "fri"]
    if re.search(r"土日(?!曜)", text) or "週末" in text:
        out += ["sat", "sun"]
    out += [_WD_ONE[c] for c in re.findall(r"([月火水木金土日])曜", text)]
    out = [w for w in WEEKDAYS if w in out]
    return out or None


def _weekday_off(items: list[dict], text: str) -> list[dict]:
    """日付の無い休み・勤務の指定で、一言が曜日を言っている時は、その曜日の頼み（weekday_pref）に読み替える
    （本物の Haiku は「遠藤さんは日曜日は休みで固定してほしい」を日付の無い day_off で返した。2026-11-03）。
    件が1つで、一言に日付（「〇日」）が無く、曜日が読め、毎週の言葉（固定・毎週・いつも など）がある時だけ。
    「今度の日曜」「来週の土曜」のような1日だけの言い方は読み替えない。ほかは今どおり日付を聞く。"""
    if len(items) != 1 or items[0].get("type") not in ("day_off", "set_cell", "avoid_cell") or items[0].get("days"):
        return items
    if re.search(r"[0-9０-９]+日", text) or not any(w in text for w in EVERY_WEEK) \
            or any(w in text for w in ONE_WEEK):
        return items
    wds = _said_weekdays(text)
    if not wds:
        return items
    it = items[0]
    shift = "公" if it["type"] == "day_off" else it.get("shift")
    return [{"type": "weekday_pref", "staff": it.get("staff"), "weekdays": wds, "shift": shift,
             "avoid": it["type"] == "avoid_cell", "until_month": None, "reason": it.get("reason")}]


# 入れ替えた2つのマスの強さ。2つのマスは1つの約束の両側なので、片方だけ守られると、同じ勤務に2人・誰もいない勤務が
# できる。本人の希望（wish）は、段1では外せないが、段1で作れない時（その人の曜日の希望とぶつかる時など）に1つずつ外れ、
# 片方だけの入れ替えが残る（2026-11-05、斉藤さんの「水曜は早番に入れない」とぶつかる11日の入れ替えで、杉山さんだけが
# 日勤になり、久保さんが早番に回った）。移す（move）の2つのマスと同じく、動かせない（fixed）にして、全部入るか、
# 全部入らないかにする
SWAP_FLEX = "fixed"
SWAP_SAME = "表もルールも変えていません"


def _swap_fill(it: dict, text: str, staff: list[Staff]) -> dict:
    """入れ替えの2人と日を、一言の文から手元で取る（Claude には swap の型の名前だけを書かせる）。指示文に「swap は
    staff と days も書く」と足すと、入れ替えと関係ない「青木さん、10日の早番、公休にしてもらえる？」を正しく直せたのが
    5回中5回から5回中2回に落ちた（本物の Haiku、2026-11-06）ので、指示文は変えない。人は名簿の名前が一言に出てくる順、
    日は「〇日」。返事に人や日が書いてあればそれを使う。取れなければ _swap が聞き返す"""
    out = dict(it)
    norm = unicodedata.normalize("NFKC", text)
    if not out.get("staff"):
        hits = sorted((norm.find(s.name), s.name) for s in staff if s.name and s.name in norm)
        out["staff"] = list(dict.fromkeys(n for _, n in hits))
    if not out.get("days"):
        ds = [int(x) for x in re.findall(r"(?<![0-9])([0-9]{1,2})日", norm)]
        out["days"] = list(dict.fromkeys(ds)) or None
    return out


def _swap(it: dict, text: str, facility: Facility, staff: list[Staff], month: Month, names: set[str],
          rules: list | None) -> tuple[list[dict], str]:
    """2人の勤務の入れ替え（「小川さんと加藤さんの22日、入れ替えて」）。その日の今の表から2人の勤務を取り、
    取り替えたマスの頼み（1人1日に1件）にする。決まりで決められない所は、黙って直さずに聞き返す。
    - 2人でない・日が無い・その日の2人が同じ勤務
    - 一言に出てくる勤務の名前（「小川さんの22日の遅番と」）が、2人のその日の勤務に無い。思い違いのまま取り替えると、
      言った人が思っていない勤務になるので、表の勤務を見せて聞く（2026-11-05、別の系統の AI が表を見ずに書いた2件は、
      どちらも表と違っていた）。読み取りに人ごとの勤務を書かせる案は、「1日休みで」の日を読めなくした（本物の Haiku で
      8回とも。前の指示文では8回とも1日）ので、一言の言葉で見る。指示文は、前からの「type だけ書く」行をそのままにして
      「swap は staff と days も書く」と足しただけにし、例は置かない。入れ替えを別の行に出したり例を足したりすると、
      ほかの一言「28の夜勤、29の明け、そこ外して両方公休に」の読み違いが、14回中1回から6回中5回〜14回中14回に増えた
      （足しただけの形は14回中0回。2026-11-06）
    - 明けは前の日の夜勤とひと続き、研修・有給は本人の予定なので、入れ替えない
    - 相手の勤務に入れない人（夜勤に入れない人など）。表を作り直しても守れない
    - 休みと勤務の入れ替えで、2人の公休の数が変わる。どの日で休みを返すかは決まりで決まらない。2つの日を言って
      数が変わらなければ（「3日と22日、入れ替えて」）入れ替える
    - 前に頼まれた、同じ人・同じ日のマスの頼みとぶつかる。どちらを残すかは決まりで決まらない
    夜勤の入れ替えは、言われた日のマスだけを頼みにする。翌日の明けとその後の休みは、帳面の動かせない並びのルール
    （夜勤の翌日は明け・明けは夜勤の翌日だけ・明けの翌日は公休）に沿って、表を直す時に一緒に動く。移す（move）と
    同じ作りで、明けの後の決まりが施設ごとに違っても、その施設のルールで直るため"""
    who = list(dict.fromkeys(_names(it.get("staff"), names) or []))
    if len(who) != 2:
        raise Clarify(f"入れ替える2人を教えてください。{SWAP_SAME}")
    if it.get("days") in (None, []):
        raise Clarify(f"{_who(who)}の、何日の勤務を入れ替えるか教えてください。{SWAP_SAME}")
    ds = sorted(set(_days(it.get("days"), month)))
    table = month.table or {}
    if any(w not in table for w in who):
        raise Clarify(f"表にない人がいるので、入れ替えられません。{SWAP_SAME}")
    a, b = who
    cell = {(w, d): table[w][d - 1] for w in who for d in ds}

    def nm(sym: str) -> str:
        return facility.shift(sym).name

    def now(days: list[int]) -> str:
        return "、".join(f"{a}さんの{d}日は{nm(cell[a, d])}、{b}さんは{nm(cell[b, d])}" for d in days)

    # 名前が1文字の勤務（取り込みで作る施設にありうる）は「22日」などに紛れるので見ない
    if {st.symbol for st in facility.shift_types if len(st.name or "") >= 2 and st.name in text} - set(cell.values()):
        raise Clarify(f"表では、{now(ds)}です。言われた勤務と違うので、{SWAP_SAME}。人と日を確かめて、もう一度言ってください")
    days = [d for d in ds if cell[a, d] != cell[b, d]]
    if not days:
        what = f"2人とも{nm(cell[a, ds[0]])}" if len(ds) == 1 else "2人とも同じ勤務"
        raise Clarify(f"{a}さんと{b}さんの{'・'.join(map(str, ds))}日は、{what}です。入れ替えても表は変わりません。{SWAP_SAME}")
    for d in days:
        for w in who:
            st = facility.shift(cell[w, d])
            if st.kind == "ake":
                prev = f"前の日（{d - 1}日）" if d > 1 else "前の日"
                raise Clarify(f"{w}さんの{d}日は{st.name}です。{st.name}は前の日の勤務とひと続きなので、{prev}で言ってください。"
                              f"{SWAP_SAME}")
            if st.on_request_only:
                raise Clarify(f"{w}さんの{d}日は{st.name}です。{st.name}は本人の予定なので入れ替えられません。"
                              f"1人ずつ、その日の勤務を言ってください。{SWAP_SAME}")
    ym = f"{month.year:04d}-{month.month:02d}"
    can = {s.name: s.can_on(ym) for s in staff if s.name in who}
    for d in days:
        for w, other in ((a, b), (b, a)):
            st = facility.shift(cell[other, d])
            if st.kind == "work" and st.symbol not in can.get(w, []):
                raise Clarify(f"{w}さんは{st.name}に入れないので、{d}日の入れ替えはできません。{SWAP_SAME}")
    for st in facility.shift_types:
        if st.kind not in ("rest", "leave"):
            continue
        na = sum(1 for d in days if cell[a, d] == st.symbol)
        nb = sum(1 for d in days if cell[b, d] == st.symbol)
        if na != nb:
            gain, lose = (a, b) if nb > na else (b, a)
            n = abs(na - nb)
            raise Clarify(f"表では、{now(days)}です。入れ替えると、{gain}さんの{st.name}が{n}日増え、{lose}さんの{st.name}が"
                          f"{n}日減るので、{SWAP_SAME}。休みを返す日も決まっていれば、その日も入れて言ってください")
    for r in rules or []:
        if r.kind != "cell" or not r.when or r.meta.get("carry"):
            continue
        for d in days:
            for w, other in ((a, b), (b, a)):
                sym = cell[other, d]
                if d in r.when and w in (r.who or []) and \
                        (("in" in r.params and sym not in r.params["in"]) or sym in r.params.get("not_in", [])):
                    raise Clarify(f"{w}さんの{d}日には、前からの「{r.name}」があります。入れ替えるとぶつかるので、{SWAP_SAME}。"
                                  "先にそちらを取り消してから言ってください")
    out = []
    for d in days:
        for w, other in ((a, b), (b, a)):
            out.append({"name": f"{w}さん {d}日 {nm(cell[other, d])}（{other}さんと入れ替え）", "kind": "cell",
                        "flex": SWAP_FLEX, "params": {"in": [cell[other, d]]}, "who": [w], "when": [d],
                        "meta": {"swap_with": other}})
    detail = f"（{a}さん {nm(cell[b, days[0]])}・{b}さん {nm(cell[a, days[0]])}）" if len(days) == 1 else ""
    return out, f"{a}さんと{b}さんの{'・'.join(map(str, days))}日を入れ替え{detail}"


def to_proposal(items: list[dict], text: str, facility: Facility, staff: list[Staff],
                month: Month, leave_caps: dict[str, int] | None = None, rules: list | None = None) -> Proposal:
    """leave_caps は人ごとの有給の上限（帳面の回数の決まりから。日数のない有給の頼みに使う。13章）。
    rules はこの月の帳面のルール（人数の頼みを、今の同じ勤務の人数のルールと同じ数え方にそろえるのに使う）。"""
    names = {s.name for s in staff}
    items = _tidy_leave(_weekday_off(_same_day_move(_tidy(items, month), text, facility, names), text), text)
    real = [it for it in items if it.get("type") != "none"]
    if not real:
        raise Clarify("表を変える頼みが見つかりませんでした。もう一度言ってください")
    for it in real:
        if it["type"] == "unclear":
            raise Clarify(it.get("question") or "うまく聞き取れませんでした。もう一度言ってください")
        if it["type"] in NOT_YET:
            raise Clarify("この形の頼みは、まだ扱えません。別の言い方で言ってください")
    kinds = {"cells" if it["type"] in DATE_TYPES else "staff" if it["type"] == "staff_info"
             else "layer" if it["type"] == "layer_def" else "cancel" if it["type"] == "cancel"
             else "cover" if it["type"] == "cover" else "rule" for it in real}
    if len(kinds) > 1:
        raise Clarify("頼みが混ざっています。1つずつ言ってください")
    kind = kinds.pop()
    p = Proposal(kind=kind)
    sums = []
    for it in real:
        t = it["type"]
        reason = it.get("reason") or None
        p.reason = p.reason or reason
        if t == "layer_def":
            from .standby import DefError, explain, validate_def
            try:
                d = validate_def(it.get("def"), facility)
            except DefError as e:
                raise Clarify(str(e))
            p.rules.append({"name": d["name"], "kind": "layer", "flex": "operational",
                            "params": {"def": d}, "who": None, "when": None,
                            "meta": {"explain": explain(d)}})
            sums.append(f"{d['name']}（" + "・".join(explain(d)) + "）")
        elif t == "day_off":
            who = _names(it.get("staff"), names)
            ds = _days(it.get("days"), month)
            fl = _cell_strength(it.get("strength"), text, t) if len(real) == 1 else _flex(it.get("strength"), "wish")
            p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 休み"
                                    + {"fixed": "（必ず）", "negotiable": "（できれば）"}.get(fl, ""),
                            "kind": "cell", "flex": fl, "params": {"in": ["公"]}, "who": who, "when": ds})
        elif t in ("set_cell", "avoid_cell"):
            who = _names(it.get("staff"), names)
            ds = _days(it.get("days"), month)
            sym = _sym(it.get("shift"), facility)
            sname = facility.shift(sym).name
            on_req = facility.shift(sym).on_request_only
            if t == "set_cell":
                p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 {sname}", "kind": "cell",
                                "flex": "fixed" if on_req else _cell_strength(it.get("strength"), text, t)
                                if len(real) == 1 else _flex(it.get("strength"), "wish"),
                                "params": {"in": [sym]}, "who": who, "when": ds})
            else:
                p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日は{sname}にしない",
                                "kind": "cell", "flex": "wish", "params": {"not_in": [sym]},
                                "who": who, "when": ds})
        elif t == "move":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            a, b = _days(it.get("from_day"), month)[0], _days(it.get("to_day"), month)[0]
            sname = facility.shift(sym).name
            p.rules.append({"name": f"{_who(who)} {a}日は{sname}でない", "kind": "cell", "flex": "fixed",
                            "params": {"not_in": [sym]}, "who": who, "when": [a], "meta": {"moved_to": b}})
            p.rules.append({"name": f"{_who(who)} {b}日 {sname}（{a}日から移す）", "kind": "cell",
                            "flex": "fixed", "params": {"in": [sym]}, "who": who, "when": [b], "meta": {"moved_from": a}})
        elif t == "swap":
            if len(real) != 1:   # 同じ人・同じ日の別の頼みと重なると、どちらを当てるか決まらない
                raise Clarify(f"入れ替えは、ほかの頼みと分けて1つずつ言ってください。{SWAP_SAME}")
            rs, s = _swap(_swap_fill(it, text, staff), text, facility, staff, month, names, rules)
            p.rules.extend(rs)
            sums.append(s)
        elif t == "forbid_sequence":
            who = _names(it.get("staff"), names)
            a = _sym(it.get("first"), facility)
            nx = it.get("next")
            bs = list(dict.fromkeys(_sym(x, facility) for x in (nx if isinstance(nx, list) else [nx])))
            p.rules.append({"name": f"{'' if not who else _who(who) + ' '}{facility.shift(a).name}の次の日に"
                                    f"{'・'.join(facility.shift(b).name for b in bs)}を置かない",
                            "kind": "sequence", "flex": _flex(it.get("strength"), "negotiable"),
                            "params": {"if": [a], "next_not_in": bs}, "who": who, "when": None})
        elif t == "count_limit":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            lo, hi = _num(it.get("min")), _num(it.get("max"))
            if lo is None and hi is None:
                raise Clarify("何回までか教えてください")
            if lo is not None and hi is not None and lo > hi:
                raise Clarify("回数の下限が上限より大きくなっています。もう一度言ってください")
            until_day = it.get("until_day")
            when = None
            label = "月"
            if until_day is not None:
                ud = _days(until_day, month)[0]
                when = list(range(1, ud + 1))
                p.until = f"{month.year:04d}-{month.month:02d}-{ud:02d}"
                label = f"{month.month}月1〜{ud}日の累計"
            if hi == 0 and lo is None and who and len(who) == 1 and month.table and re.search(KEEP_NOW, text) \
                    and not re.search(REMOVE_WORDS, text):
                # 「今月はもう夜勤、追加しないでね。今入ってる分はそのままで」を、上限0回と読むことがある（本物の Haiku で
                # 3回に2回、夜勤を全部外して19マス動いた。2026-11-05、別の系統の AI が書いた言い方）。「追加しない」
                # 「増やさない」「そのまま」と言っている時は、今入っている回数を上限にする（今の分は消さず、増やさない）
                row = month.table.get(who[0], [])
                now = sum(1 for i, x in enumerate(row, 1) if x == sym and (when is None or i in when))
                hi = now or hi
            # 休みの勤務（公休・有給）は「日」で数える。下限と上限が同じなら1つの数で言う（「公休は月10〜10回」と出ていた）
            unit = "日" if facility.shift(sym).kind in ("rest", "leave") else "回"
            rng = (f"{lo}{unit}" if lo is not None and lo == hi else
                   f"{lo}〜{hi}{unit}" if lo is not None and hi is not None else
                   f"{hi}{unit}まで" if hi is not None else f"{lo}{unit}以上")
            params = {"shifts": [sym]}
            if lo is not None:
                params["min"] = lo
            if hi is not None:
                params["max"] = hi
            p.rules.append({"name": f"{_who(who)} {facility.shift(sym).name}は{label}{rng}", "kind": "count",
                            "flex": _count_strength(it.get("strength"), text, len(real) == 1), "params": params,
                            "who": who, "when": when})
        elif t == "weekday_pref":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            wds = [w for w in (it.get("weekdays") or []) if w in WEEKDAYS]
            if not wds:
                raise Clarify("曜日が読めませんでした。何曜日か教えてください")
            avoid = it.get("avoid", True) is not False
            jp = "".join("月火水木金土日"[WEEKDAYS.index(w)] for w in wds)
            um = _num(it.get("until_month"))
            if um and 1 <= um <= 12:   # 「12月いっぱい」。その月の末日まで（翌月の写しに残り、その次の月には残らない）
                import calendar
                uy = month.year + (1 if um < month.month else 0)
                p.until = f"{uy:04d}-{um:02d}-{calendar.monthrange(uy, um)[1]:02d}"
            upto = f"（{um}月まで）" if um and 1 <= um <= 12 else ""
            rest = facility.shift(sym).kind == "rest"
            what = ("休みにしない" if avoid else "休み") if rest else \
                f"{facility.shift(sym).name}{'に入れない' if avoid else 'にする'}"
            p.rules.append({"name": f"{_who(who)} {jp}曜は{what}{upto}",
                            "kind": "weekday", "flex": "wish",
                            "params": {"weekdays": wds, ("not_in" if avoid else "in"): [sym]},
                            "who": who, "when": None})
        elif t == "coverage":
            sym = _sym(it.get("shift"), facility)
            lo, hi = _num(it.get("min")), _num(it.get("max"))
            if lo is None and hi is None:
                raise Clarify("何人にするか教えてください")
            if lo is not None and hi is not None and lo > hi:
                raise Clarify("人数の下限が上限より大きくなっています。もう一度言ってください")
            raw = it.get("weekdays")
            wds = [w for w in (raw or []) if w in WEEKDAYS]
            if raw and len(wds) != len(raw):
                raise Clarify("曜日が読めませんでした。何曜日か教えてください")
            hol = it.get("holidays") is True
            if hol and not wds:
                # 「祝日は日勤を3人に」が曜日の無いルールになり、毎日に効いていた（敵対レビュー 2026-11-03）
                raise Clarify("祝日だけの人数ですか。土日も入れるなら「土日祝は」と言ってください")
            # 今の同じ勤務の人数のルール（曜日の無いもの）と同じ数え方（記号の組・ユニットごとか）にそろえる
            base = next((r for r in rules or [] if r.kind == "coverage" and not r.params.get("weekdays")
                         and sym in (r.params["shifts"] if isinstance(r.params["shifts"], list)
                                     else [r.params["shifts"]])), None)
            if base is not None:
                shifts, scope = list(base.params["shifts"]), base.params.get("scope", "all")
            elif len(facility.units) > 1:
                raise Clarify("ユニットごとの人数か、施設全体の人数か教えてください")
            else:
                shifts, scope = [sym], "all"
            ops = ([("==", lo)] if lo is not None and lo == hi else
                   [(">=", lo)] * (lo is not None) + [("<=", hi)] * (hi is not None))
            jp = ("".join("月火水木金土日"[WEEKDAYS.index(w)] for w in wds) + ("祝" if hol else "")
                  if wds else "毎日")
            sname = facility.shift(sym).name
            for op, n in ops:
                params = {"shifts": shifts, "scope": scope, "op": op, "n": n}
                if wds:
                    params["weekdays"] = wds
                    params["holidays"] = hol
                each = "（ユニットごと）" if scope == "each_unit" else ""   # 2ユニットで合計が倍になることを名前で見せる
                p.rules.append({"name": f"{jp}の{sname}は{n}人" + {"==": "", ">=": "以上", "<=": "まで"}[op] + each,
                                "kind": "coverage", "flex": _count_strength(it.get("strength"), text, len(real) == 1),
                                "params": params, "who": None, "when": None})
        elif t == "consecutive_limit":
            who = _names(it.get("staff"), names)
            mx = _num(it.get("max"))
            if not mx:
                raise Clarify("何日まで続けてよいか教えてください")
            p.rules.append({"name": f"{_who(who)} 連勤は{mx}日まで", "kind": "max_run",
                            "flex": _flex(it.get("strength"), "operational"),
                            "params": {"shifts": "work", "max": mx}, "who": who, "when": None})
        elif t == "cancel":
            # 取り消しは指定を外すだけ。反対の勤務を命じない（13章。T2-03）
            who = _names(it.get("staff"), names)
            if not who and it.get("days") in (None, []) and len(real) == 1:
                # 人も日も無い取り消し（「さっきのやっぱりなし」）。この月に話したマスの頼みが1件に決まる時だけ、
                # 確かめてから外す（session._cancel_offer）。一言に名簿の人が出ていれば下で聞き返す
                sym = _sym(it.get("shift"), facility) if it.get("shift") else None
                p.removes.append({"who": None, "days": None, "shift": sym})
                continue
            if not who:
                raise Clarify("誰の、何日の頼みを取り消すか教えてください")
            sym = _sym(it.get("shift"), facility) if it.get("shift") else None
            if it.get("days") in (None, []):
                # 日付の無い取り消し（「さっきの〇〇さんの休み、やっぱりなし」）。当てる先は帳面で探し、
                # 1つに決まる時だけ確かめてから外す（session._cancel）
                if len(who) != 1 or len(real) != 1:
                    raise Clarify("誰の、何日の頼みを取り消すか教えてください")
                p.removes.append({"who": who, "days": None, "shift": sym})
                continue
            ds = _days(it.get("days"), month)
            p.removes.append({"who": who, "days": ds, "shift": sym})
            what = f"{facility.shift(sym).name}の" if sym and sym != "公" else "休みの" if sym == "公" else ""
            sums.append(f"{_who(who)}の{'・'.join(map(str, ds))}日の{what}指定を外しました")
        elif t == "leave_count":
            # 日数がなければその人の上限まで、日付は任せる下限だけの決まり（13章。T2-06）
            who = _names(it.get("staff"), names)
            if not who or len(who) != 1:
                raise Clarify("誰の有給か教えてください")
            n = _num(it.get("count"))
            if n is None:
                cap = (leave_caps or {}).get(who[0])
                if cap is None:
                    raise Clarify(f"{who[0]}さんの有給は何日ですか")
                if cap == 0:
                    raise Clarify(f"{who[0]}さんの有給の上限は0日になっています。何日にするか教えてください")
                n = cap
            elif n <= 0:
                raise Clarify("有給の日数を教えてください")
            lv = _leave_sym(facility)
            if lv is None:
                raise Clarify(NO_LEAVE)
            p.rules.append({"name": f"{who[0]}さん 有給{n}日（日付は任せる）", "kind": "count", "flex": "wish",
                            "params": {"shifts": [lv], "min": n}, "who": who, "when": None})
        elif t == "staff_info":
            who = _names(it.get("staff"), names) or []
            fm = _num(it.get("from_month"))
            if fm is None:
                fm = month.month   # 月を言っていなければ、開いている月から（前は聞き返して止まった。2026-11-06）
            if not who or not fm or fm > 12:
                raise Clarify("誰の、何月からの変更か教えてください")
            year = month.year + (1 if fm < month.month else 0)
            add = [_sym(s, facility) for s in it.get("can_add") or []]
            rem = [_sym(s, facility) for s in it.get("can_remove") or []]
            work = [st.symbol for st in facility.shift_types if st.kind == "work" and not st.on_request_only]
            only = it.get("only")
            if only is None and len(add) == 1 and re.search(r"だけ|のみ|専属|専従|しか", text):
                only = add[0]   # 「日勤だけ」を、できる勤務を足す形で読んだ時
            if only is None and rem and not add:   # 外す形で読んだ時は、その人に残る勤務が1つなら「〇〇だけ」
                left = {s for x in staff if x.name in who for s in x.can_on(f"{year:04d}-{fm:02d}")
                        if s in work and s not in rem}
                only = left.pop() if len(left) == 1 else None
            if only is not None:
                add, rem = [only], [s for s in work if s != only]
            ym = f"{year:04d}-{fm:02d}"
            for n in who:
                p.staff_changes.append({"staff": n, "from": ym, "can_add": add, "can_remove": rem, "said": text})
            nm = lambda syms: "・".join(facility.shift(s).name for s in syms)
            if only is not None:
                had = [s for s in work if s != only and any(
                    s in x.can_on(ym) for x in staff if x.name in who)]
                line = f"{_who(who)} {fm}月から{nm([only])}だけ" + (f"（{nm(had)}に入れません）" if had else "")
            else:
                parts = ([nm(add) + "もできる"] if add else []) + ([nm(rem) + "に入れない"] if rem else [])
                line = f"{_who(who)} {fm}月から" + "・".join(parts)
            if (year, fm) > (month.year, month.month):
                line += f"。{fm}月の表を作る時から効きます（{month.month}月の表は変えていません）"
            sums.append(line)
        elif t == "cover":
            # すでに起きた穴埋め（残業・早出など）。表も帳面も変えず、引き受けの記録の候補にする。人が無ければ
            # ここで聞き返す（下の「誰の頼みか」の守りは p.rules しか見ない）。1人・1日ずつ（まとめては聞き返す）。
            # 日を言っていなければ None のまま返す（今日は session が基準日で決める）
            who = _names(it.get("staff"), names)
            if not who:
                raise Clarify(NOBODY_COVER)
            if len(who) != 1 or len(real) != 1:
                raise Clarify("1人ずつ言ってください。表も記録も変えていません")
            v = it.get("day")
            if v in (None, []) and it.get("days"):   # 日付の型と同じ書き方（days）で返した時
                v = it["days"]
            day = None
            if v not in (None, []):
                ds = _days(v, month)
                if len(ds) != 1:
                    raise Clarify("1日ずつ言ってください。表も記録も変えていません")
                day = ds[0]
            how = _how(it.get("how"))
            p.covers.append({"staff": who[0], "day": day, "how": how})
            sums.append(f"{who[0]}さん {how}で穴埋め")
    # 一言に名簿の人が出ているのに、返事が人を省いて「全員」に効く形なら、当てずに聞き返す（レビュー b の指摘。
    # 「岡田さん、15と16は休み」が全員の休みになる道を塞ぐ）
    # 人が出ているかは、伏せる処理と同じ見方（名前・呼び方・名前の頭）で見る（再レビュー b の指摘）
    from .names import Masker
    # 全員向けのルールは「利用者さん」「みなさん」を含むことが多いので、「さん」だけでは人が出たとみなさない
    # （再レビュー2回目 a の指摘）。マスの頼みは下で、人が無ければいつでも聞き返す
    # ただし「おかださん」のように名簿の表記に無いかなの呼びかけは伏せられないので、「さん」などの前の語が
    # 名前でない語（利用者・みな など）でなければ、人が出たとみなす（周8 レビュー b の指摘）
    from .names import FAMILY_WORDS, NOT_NAMES
    not_name = NOT_NAMES | FAMILY_WORDS | {"みな", "みんな", "お客", "医者", "先生", "看護", "介護", "職員", "ケアマネ",
                            "施設長", "事務", "主任", "スタッフ", "お子", "おばあ", "おじい"} | {sh.name[:2] for sh in facility.shift_types}
    masker = Masker(staff, facility)
    named = {masker.to_name[c] for c in re.findall(r"職員\d{2}", masker.mask(text)[0]) if c in masker.to_name}
    said = bool(named) or any(
        not any(w.endswith(n) for n in not_name)
        for w in re.findall(r"([ぁ-ゖァ-ヺー一-龥々]{1,6}?)(?:さん|くん|ちゃん)", text))
    # マスの頼み（休み・勤務・移動）と取り消しは、人が無いまま全員に効かせない。「全員15日休み」は頼みとして
    # ありえず、さんの無いかなの名前や「皆」を含む一言でもすり抜けないように（再レビュー2回目 b の指摘）
    cells_nobody = any(not r.get("who") for r in p.rules if r["kind"] == "cell") or any(
        not x.get("who") and (said or x.get("days") is not None) for x in p.removes)
    rules_nobody = any(not r.get("who") for r in p.rules if r["kind"] not in ("layer", "cell"))
    # 回数と連勤は人ごとに言うことが多いので、人が無い返事は「全員」「みんな」と言い、名前も出ていない時だけ
    # 全員に効かせる（言葉の手がかりで人を当てる作りは呼び方ごとに穴が出続けたため。作者の答え 2026-11-03）
    everyone = bool(re.search(r"全員|みんな|皆(?!勤)", text))
    per_nobody = any(not r.get("who") for r in p.rules if r["kind"] in ("count", "max_run"))
    # 「みんな夜勤は月4回までで、井上さんだけは3回までにして」。全員と名指しが1つの一言に入る時は、言った人が
    # 全員、名指しの回数のルールに入っている時だけ通す（名指しは with_overrides で全員向けより優先する）。
    # 名前が出たのに、その人の分の返事が無ければ今どおり聞き返す
    per_named = {w for r in p.rules if r["kind"] in ("count", "max_run") for w in (r.get("who") or [])}
    both = everyone and bool(named) and named <= per_named and not any(
        not r.get("who") for r in p.rules if r["kind"] not in ("layer", "cell", "count", "max_run")) and not any(
        not any(w.endswith(n) for n in not_name) and not any(w.endswith(n) for n in named)
        for w in re.findall(r"([ぁ-ゖァ-ヺー一-龥々]{1,6}?)(?:さん|くん|ちゃん)", text))
    if cells_nobody or (per_nobody and (not everyone or (said and not both))) \
            or (said and not everyone and rules_nobody):
        raise Clarify(WHO_UNKNOWN + ("" if cells_nobody else "（全員のルールなら「全員」と付けてください）"))
    if kind == "cells":
        p.scope = "once"
        # 言い直し（「赤坂さんじゃなくて井上さん、10日が休みで」）。読み取りは新しい人の頼みだけを返すので、
        # 言った人のうち頼みに無い人が1人の時だけ印を付け、前の頼みを外すかは帳面を見て確かめる（session）
        others = sorted(named - {w for r in p.rules for w in (r.get("who") or [])})
        if any(w in text for w in REPHRASE_WORDS) and len(others) == 1 and len(p.rules) == 1 \
                and len(p.rules[0].get("who") or []) == 1:
            p.replace_from = others
    elif kind in ("cancel", "cover"):
        p.scope = "once"   # 取り消しは前の頼みを外すだけ、穴埋めは記録するだけ。聞かない
    elif kind in ("rule", "layer"):
        p.scope = "rule" if p.until else read_period(text)
    else:  # 職員の情報は言われた月から。聞かない
        p.scope = "rule"
    p.summary = "、".join(sums) if sums else "、".join(r["name"] for r in p.rules if "moved_to" not in r.get("meta", {}))
    return p
