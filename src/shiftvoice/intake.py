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

from .addshift import said_times
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
    replace_days: list[int] | None = None   # 同じ人の言い直し（「20日じゃなくて21日」）で、前の頼みを外す日
    covers: list[dict] = field(default_factory=list)   # 穴埋めの記録の候補 {staff, day（言っていなければ None）, how}
    support: dict[str, str] = field(default_factory=dict)   # ユニット間の応援 "名前/日" → その日に働くユニット
    shift_type: dict | None = None     # 話して足す勤務の種類（facility.json の1件の形）
    from_next: bool = False            # 「来月から」の毎月のルール。帳面の始まりを翌月1日にする（今月の表は変えない）


class Clarify(Exception):
    """聞き返す（表も帳面も変えない）。need は答えを前の頼みにつなぐ時の、足りない物（redo.fill_answer）。"""

    def __init__(self, message: str = "", need: str | None = None):
        super().__init__(message)
        self.need = need


# 名前の無い一言（「私、13日の日勤、有給に」）に、Claude が人の欄へ入れることがある埋め草（2026-11-05、本物の Haiku で
# 「職員不明」）。名簿にない人ではなく、誰の頼みか分からないと聞く
# 回数の上限の頼みで「今の分は残して、これ以上増やさない」と言っている言葉。外す・0回と言っている時は使わない
# （「今入っている夜勤も全部外して」「夜勤は0回、日勤はそのまま」。Codex Astra のレビュー 2026-11-05）
KEEP_NOW = r"追加しない|追加せず|増やさない|増やさず|そのまま|今入って|今の分"
REMOVE_WORDS = r"外し|外す|はずし|はずす|0回|０回|ゼロ|なし|無し|抜い|抜く|消し|消す|全部"
NOBODY = re.compile(r"^(職員)?(不明|未定|未指定|NN|ＮＮ|XX|ＸＸ|〇〇|○○)(さん)?$|^(本人|自分|私|わたし|僕|俺|話者|依頼者)$")
WHO_UNKNOWN = "誰の頼みか読み取れませんでした。表もルールも変えていません。名前を入れて、もう一度言ってください"
EVERYONE_HINT = "（全員のルールなら「全員」と付けてください）"


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
        raise Clarify("日付が読めませんでした。何日か教えてください", need="days")
    if not ds or any(d < 1 or d > month.days for d in ds):
        raise Clarify(f"{month.month}月にない日付です。何日か教えてください", need="days")
    return ds


NO_LEAVE = "この施設には有給の記号がありません。表もルールも変えていません"


def _sym(v, facility: Facility) -> str:
    if v not in facility.symbols:
        if v in ("有", "有給") and _leave_sym(facility) is None:
            raise Clarify(NO_LEAVE)   # 取り込んで作った施設には、有給の記号が無いことがある
        # None は読み取りが勤務を書かなかった時。機械の文字を画面に出さない（2026-11-06 AI のテストプレイ）
        said = f"（{v}）" if v and isinstance(v, str) else ""
        raise Clarify(f"勤務が読めませんでした{said}。どの勤務にするか教えてください。表もルールも変えていません", need="shift")
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


FIXED_WORDS = ("必ず", "かならず", "絶対", "ぜったい", "どうしても", "外せない", "動かせない", "ハードルール", "ハードで", "ハードに")
MAYBE_WORDS = ("できれば", "出来れば", "できるだけ", "出来るだけ", "なるべく", "可能なら", "できたら", "出来たら", "ソフトルール", "ソフトで", "ソフトに")
# 強さを言う言葉そのもの。理由の欄に入れない（「ハードルールで夜勤3回まで」の理由が「ハードル」と出た。作者 2026-11-07）
_STRENGTH_TALK = re.compile(r"強めのソフト(?:ルール)?|ハードルール|ソフトルール|ハードル|ハード|ソフト")
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


def _seq_strength(v, text: str, single: bool) -> str:
    """並びの禁止（「遅番の次の日に早番を入れないで」）の強さ。言い切ったら運用の都合（operational）。
    「きつい」「あげて」「できれば」「なるべく」と言った時は配慮（negotiable）。必ず系の言葉がある時は、読み取りの値のまま。
    読み取りが negotiable と返しても、配慮の言葉が無ければ operational にする（作者 2026-11-07）。
    並びの禁止が2件以上ある時は、どの件の言い方か分からないので、前どおり読み取りの値を使う。名前つきも同じ決め方
    （個人の都合の押し付けでなく、管理者が言い切った決まりなので。解けなければ段を下げて知らせる）。"""
    if not single or any(w in text for w in FIXED_WORDS):
        return _flex(v, "negotiable")
    if any(w in text for w in MAYBE_WORDS + CARE_WORDS):
        return "negotiable"
    return "operational"


def _said_units(text: str, facility: Facility, unit=None) -> list[str]:
    """一言で名指ししたユニット（「東ユニットは」「東は」）。伏せ方（names.Masker）と同じ区切りで探す。
    読み取りの返事に unit があれば（名前に戻した後の値）それも使う。ユニットが1つの施設は名指ししない。"""
    if len(facility.units) < 2:
        return []
    out = [u for u in facility.units
           if re.search(re.escape(u) + r"(?=ユニット|フロア|[、。,.\s　のはがにでもとへやを]|$)", text or "")]
    for v in ([unit] if isinstance(unit, str) else unit if isinstance(unit, list) else []):
        if v in facility.units and v not in out:
            out.append(v)
    return [u for u in facility.units if u in out]


def _said_whole(text: str) -> bool:
    """施設全体の人数と言ったか（「施設全体で」「全体で」「合計」「合わせて」「施設で」）。"""
    return bool(re.search(r"全体|合計|合わせて|施設で|全ユニットで", text or ""))


def _count_strength(v, text: str, single: bool) -> str:
    """回数の強さ。必ず系の言葉があれば fixed（件が1つの時だけ）。本人の希望・できればと言った時は読み取りの値を残す。
    ほかは施設の運用（operational）にそろえる（「今月は夜勤0で」が wish だと全員向けの下限に負ける）。
    「強めのソフト」は運用の都合（守るが、どうしても組めない時だけ外す）"""
    if single and "強めのソフト" in text:
        return "operational"
    if single and any(w in text for w in FIXED_WORDS):
        return "fixed"
    if single and any(w in text for w in MAYBE_WORDS):
        # 「できれば」は配慮の強さ。読み取りが wish（本人の希望。運用より強い）や operational と返しても、配慮にする
        # （「日勤はできれば毎日3人」が本人の希望の強さで入り、夜勤2人の決まりを押しのけた。2026-11-07 の通しの試し）。
        # 頼みが2件以上ある時は、どの件の「できれば」か分からないので、読み取りの値を残す（Astra）
        return "negotiable"
    if v in ("wish", "negotiable") and any(w in text for w in OWN_WORDS + MAYBE_WORDS):
        return v
    return "operational"


def said_strength(text: str) -> bool:
    """一言が強さ（必ず・できれば・本人の希望・ハード・ソフト）を言ったか。"""
    return any(w in text for w in FIXED_WORDS + MAYBE_WORDS + OWN_WORDS) or bool(_STRENGTH_TALK.search(text or ""))


# 名指しの上限を、全員の上限の例外として頼む言い方（「千葉さん、今月だけ6連勤まではいい」「6日まで許して」）
PERMIT_WORDS = r"今月だけ|今月は|てもいい|ても良い|てもよい|でもいい|でいい|許|かまわない|構わない|大丈夫|OK"


# 日付を決めた一言（「28日は」「今月の1日だけ」）の日。「1日2人」「1日あたり」の1日（1日ごと）と、「3日続けて」「2日間」は除く
_DATE_IN_TEXT = re.compile(r"(?<![0-9０-９])([0-9]{1,2})日(?![勤間続連分]|[0-9]|あたり|当たり|ごと|につき|まで|以内|以上)")


def _said_dates(text: str) -> list[int]:
    text = unicodedata.normalize("NFKC", text or "")
    out = [int(d) for d in _DATE_IN_TEXT.findall(text)
           if not (d == "1" and re.search(r"(?<![0-9])1日(?:に|で)?[0-9一二三四五六七八九十]", text))]
    if not re.search(r"連勤|続け|連続|日間|以内|有給|公休|休み", text):
        # 「今月は28日まで早番3人」の28日は日付（Astra）。「連勤は5日まで」の5日は日数なので数えない
        out += [int(d) for d in re.findall(r"(?<![0-9])([0-9]{1,2})日まで", text) if int(d) not in out]
    return out


_RANGE = re.compile(r"(?<![0-9])([0-9]{1,2})日?\s*(?:から|〜|～|~|－|-)\s*([0-9]{1,2})日(?:まで|の間)?")
_FIX_WORDS = re.compile(r"違う|ちがう|じゃなく|ではなく|間違|まちが|いや[、,]|訂正")


def _coverage_days_said(text: str, month: Month, n_items: int) -> list[int]:
    """日付を決めた人数の日（「28日は早番を3人」→[28]、「24日と25日は」→[24, 25]、「1日から5日まで」→[1..5]）。日付が無ければ []。
    期間か日付か分からない言い方は聞き返す（Astra 2026-11-07 の穴5・6。前の作りは「28日まで」を28日だけ、「1日から5日まで」を
    1日と5日だけ、「28日は早番3人、29日は夜勤1人」を両方の勤務の両日、言い直しの「14日、あ、違う、18日」を両日にした）。"""
    t = unicodedata.normalize("NFKC", text or "")
    if (_RANGE.search(t) or _said_dates(t)) and re.search(r"以外|除|のぞ|抜いて|抜き|外して|のほか", t):
        # 「28日以外は早番3人」が28日だけの人数になった（Astra 2026-11-07 朝）
        raise Clarify("〇日以外という言い方の人数は、まだ扱えません。人数を変える日を、日付で言ってください")
    ranges = list(_RANGE.finditer(t))
    if ranges:
        m = ranges[0]
        if len(ranges) > 1 or _FIX_WORDS.search(t) or _said_dates(t[:m.start()] + t[m.end():]):
            # 「1日から5日までと10日」の10日、「1日から5日まで、いや、3日から5日まで」の言い直しが消えた（Astra）
            raise Clarify("期間と日付を一緒に言った時や言い直した時は、人数を変える日を、1つの言い方でもう一度言ってください")
        a, b = int(m.group(1)), int(m.group(2))
        if not 1 <= a < b <= month.days:
            raise Clarify(f"{a}日から{b}日までの日付が読めませんでした。もう一度言ってください")
        days = list(range(a, b + 1))
    else:
        days = _said_dates(t)
        if not days:
            return []
        if re.search(r"(?:月|週)に?\s*[0-9]{1,2}日|[0-9]{1,2}日(?:間|分|ぶん|ほど|くらい|ぐらい)", t):
            raise Clarify("日数で決める人数は、まだ扱えません。人数を変える日を、日付で言ってください（例「28日は早番を3人」）")
        if re.search(r"[0-9]{1,2}日(?:まで|以降|以前|から|より前|より後|以後)", t):
            raise Clarify("期間で決める人数は、何日から何日までかを言ってください（例「1日から5日まで早番を3人」）")
        if len(set(days)) > 1 and _FIX_WORDS.search(t):
            raise Clarify("日付を言い直した時は、人数を変える日を、もう一度1つの言い方で言ってください")
        days = sorted(set(days))
    if n_items > 1:
        raise Clarify("日付を決めた人数は、1つずつ言ってください（例「28日は早番を3人」）")
    if any(not 1 <= d <= month.days for d in days):
        raise Clarify("日付が月の中にありません。もう一度言ってください")
    return days


def _days_jp(days: list[int]) -> str:
    """[28]→「28日」、[24, 25]→「24・25日」、[1, 2, 3, 4, 5]→「1〜5日」。"""
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{days[0]}〜{days[-1]}日"
    return "・".join(map(str, days)) + "日"


def _syms_of_rule(r) -> list[str]:
    v = r.params.get("shifts")
    return v if isinstance(v, list) else [v]


def _typical_scope(n: int, syms: list[str], month: Month, staff: list[Staff], units: list[str]) -> str | None:
    """言った人数が、今の表のいつもの数（施設全体で1日に何人か・1つのユニットで1日に何人か）のどちらに当たるか。
    片方だけに当たる時だけ返す。「夜勤は毎日ちょうど2人」が、取り込みのユニットごとの数え方にそろえられて
    ユニットごと2人になり、35か所破れた（2026-11-07 の通しの試し。施設全体ではいつも2人、ユニットでは0〜1人）"""
    from collections import Counter
    if not month.table or len(units) < 2:
        return None
    unit_of = {s.name: s.unit for s in staff}
    tot, per = Counter(), Counter()
    for d in range(month.days):
        tot[sum(1 for nm, row in month.table.items() if d < len(row) and row[d] in syms)] += 1
        for u in units:
            per[sum(1 for nm, row in month.table.items() if unit_of.get(nm) == u and d < len(row) and row[d] in syms)] += 1
    t, u = tot.most_common(1)[0][0], per.most_common(1)[0][0]
    if n == t and n != u:
        return "all"
    if n == u and n != t:
        return "each_unit"
    return None


def _said_run_shift(text: str, facility: Facility) -> str | None:
    """「早番を3日続けない」「遅番が3日連続にならないように」の勤務。勤務の名前が1つだけで、続け・連続と言った時だけ。
    続けて働く日数の上限の型は勤務の欄を持たず、全員の連勤の上限になっていた（2026-11-07 の通しの試し。
    「早番を3日続けて入れないで」が「全員 連勤は2日まで」になり124か所破れた）"""
    if not re.search(r"続け|連続", text or "") or "連勤" in (text or "") or "含め" in (text or ""):
        return None
    found = []
    for st in facility.shift_types:
        if st.kind != "work":
            continue
        # 勤務の名前のすぐ後に「3日続け」「連続」が来る時だけ（「夜勤も含めて5日連続」は全部の勤務の連勤。Astra）
        if st.name and re.search(re.escape(st.name) + r"[をはがの]?(?:最大|多くても|上限|最長)?[0-9０-９一二三四五六七八九十]*日?"
                                 r"(?:まで)?(?:続け|連続)", text) and st.symbol not in found:
            found.append(st.symbol)
    return found[0] if len(found) == 1 else None


def _named_run_shift_unclear(text: str, facility: Facility) -> bool:
    """勤務の名前が1つあり「続け・連続」と言ったのに、どの勤務の続く日数か読めない時（全部の勤務の連勤にしない。Astra）"""
    if not re.search(r"続け|連続", text or "") or "連勤" in (text or "") or "含め" in (text or ""):
        return False
    return sum(1 for st in facility.shift_types if st.kind == "work" and st.name and st.name in text) == 1


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
    if "公休" not in text and not offs:   # 「あさっては有給取るって」を公休のマスと読むことがある（本物の Haiku、2026-11-06）
        rest = [it for it in items if it.get("type") == "set_cell" and it.get("shift") == "公" and it.get("days")]
        if len(rest) == 1:
            items = [{**it, "shift": "有"} if it is rest[0] else it for it in items]
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


def _self_fix(items: list[dict], text: str) -> list[dict]:
    """「25、あ、ごめん26日」は話の途中の言い直しで、25日から移す頼みではない。読み取りが移す形で返したら、後の日の頼みにする
    （本物の Haiku、2026-11-06。3回に1回、25日のマスまで動いた）。"""
    out = []
    for it in items:
        a, b = it.get("from_day"), it.get("to_day")
        if it.get("type") == "move" and type(a) is int and type(b) is int and it.get("shift") and re.search(
                rf"(?<!\d){a}日?[、,\s]*(?:あ|え|いや)?[、,\s]*(?:ごめん|違う|ちがう|間違え|まちがえ|じゃなく)[^\d]*(?<!\d){b}日", text):
            it = {"type": "set_cell", "staff": it.get("staff"), "days": [b], "shift": it["shift"], "strength": "wish",
                  "reason": it.get("reason")}
        out.append(it)
    return out


def _was_really(items: list[dict], text: str, facility: Facility) -> list[dict]:
    """「16日、公休になってるけど研修だった」を、今の表の勤務（公休）をそのまま頼みと読むことがある（本物の Haiku、
    2026-11-06。3回に2回）。「AになってるけどBだった」と言っていて、読みが A なら、B に置き換える。"""
    kinds = {t.name: t.symbol for t in facility.shift_types} | {t.symbol: t.symbol for t in facility.shift_types}
    names = "|".join(sorted(map(re.escape, kinds), key=len, reverse=True))
    m = re.search(rf"({names})に?(?:なって(?:い)?る|なって(?:い)?た)(?:けど|けれど|が)、?({names})(?:だった|でした|です|のはず|が正し)", text)
    if not m or kinds[m[1]] == kinds[m[2]]:
        return items
    was, real = kinds[m[1]], kinds[m[2]]
    rest = next((t.symbol for t in facility.shift_types if t.kind == "rest"), None)
    out = []
    for it in items:
        if it.get("type") == "set_cell" and it.get("shift") == was or it.get("type") == "day_off" and was == rest:
            it = {"type": "set_cell", "staff": it.get("staff"), "days": it.get("days"), "shift": real,
                  "strength": it.get("strength") or "wish", "reason": it.get("reason")}
        out.append(it)
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


_SOME_DAY = re.compile(r"[0-9０-９]|[一二三四五六七八九十]+日|曜|週|土日|今日|明日|あした|あす|明後日|あさって|今度|"
                       r"月末|月初|上旬|中旬|下旬|頃|ごろ|その日|この日|同じ日")


_SEQUENCE_WORDS = re.compile(r"次の日|翌日|翌朝|あと|後|続け|連続|連チャン|明け|前の日|前日|[0-9０-９]+回|[一二三四五六七八九十]+回")


def _no_day_avoid(items: list[dict], text: str) -> list[dict]:
    """日にちを何も言っていない「〇〇さんは早番には入れないで」を、その人のその勤務の月0回にする。
    本物の Haiku は日付の無い avoid_cell で返し、「何日か」を聞き返して止まった（2026-11-08）。
    件が1つで、人と勤務があり、一言に日付・曜日・日を指す言葉が1つも無い時だけ。
    同じ一言を、並びの禁止（前の勤務が空、または同じ勤務の並び）と読むこともある（6回に2回。2026-11-08）。
    一言に「次の日」「続けて」のような並びの言葉が無ければ、同じく勤務に入れない頼みと読む。"""
    if len(items) == 1 and items[0].get("type") == "forbid_sequence" and items[0].get("staff") and items[0].get("next") \
            and items[0].get("first") in (None, "", items[0]["next"]) and not _SEQUENCE_WORDS.search(text):
        items = [{"type": "avoid_cell", "staff": items[0]["staff"], "days": None, "shift": items[0]["next"],
                  "reason": items[0].get("reason")}]
    if len(items) != 1 or items[0].get("type") != "avoid_cell" or items[0].get("days") \
            or not items[0].get("staff") or not items[0].get("shift") or _SOME_DAY.search(text):
        return items
    it = items[0]
    staff = it["staff"] if isinstance(it["staff"], list) else [it["staff"]]
    return [{"type": "count_limit", "staff": staff, "shift": it["shift"], "min": None, "max": 0, "until_day": None,
             "strength": it.get("strength") or "operational", "reason": it.get("reason")}]


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


def _support_cells(it: dict, facility: Facility, staff: list[Staff], month: Month, names: set[str], p: Proposal) -> None:
    """ユニット間の応援（「12日、青木さんは西の遅番に入って」）を、その日のマスの頼みと応援の印にする。
    作成者が言った時だけ入るので動かせない頼みにする（守れなければ「直せませんでした」で何も変えない）。
    勤務を言っていなければ（「西へ応援」）その日の勤務のまま。自分のユニットを言った時はふつうのマスの頼み。"""
    who = _names(it.get("staff"), names)
    if not who or len(who) != 1:
        raise Clarify(WHO_UNKNOWN)
    name = who[0]
    unit = it["unit"]
    if unit not in facility.units:
        raise Clarify("どのユニットか読み取れませんでした。表もルールも変えていません。もう一度言ってください")
    own = next(s.unit for s in staff if s.name == name)
    by_sym: dict[str, list[int]] = {}
    for d in _days(it.get("days"), month):
        sym = _sym(it["shift"], facility) if it.get("shift") else (month.table or {}).get(name, [""] * d)[d - 1]
        if not it.get("shift") and (sym not in facility.symbols or facility.shift(sym).kind != "work"):
            raise Clarify(f"{name}さんの{d}日は勤務がありません。何の勤務で入るかも言ってください。表もルールも変えていません")
        by_sym.setdefault(sym, []).append(d)
    for sym, ds in by_sym.items():
        away = unit != own
        p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 {facility.shift(sym).name}"
                                + (f"（{unit}へ応援）" if away else ""),
                        "kind": "cell", "flex": "fixed", "params": {"in": [sym], **({"unit": unit} if away else {})},
                        "who": who, "when": ds})
        if away:
            p.support.update({f"{name}/{d}": unit for d in ds})


def to_proposal(items: list[dict], text: str, facility: Facility, staff: list[Staff],
                month: Month, leave_caps: dict[str, int] | None = None, rules: list | None = None) -> Proposal:
    """leave_caps は人ごとの有給の上限（帳面の回数の決まりから。日数のない有給の頼みに使う。13章）。
    rules はこの月の帳面のルール（人数の頼みを、今の同じ勤務の人数のルールと同じ数え方にそろえるのに使う）。"""
    names = {s.name for s in staff}
    items = _tidy_leave(_no_day_avoid(_weekday_off(_was_really(_same_day_move(_self_fix(_tidy(items, month), text), text, facility, names), text, facility), text), text), text)
    real = [it for it in items if it.get("type") != "none"]
    if not real:
        raise Clarify("表を変える頼みが見つかりませんでした。もう一度言ってください")
    if (len(real) == 2 and all(it["type"] == "count_limit" for it in real)
            and re.search(r"と[^、。に]{0,8}(?:合わせて|あわせて|併せて|合計)|合計", text)
            and real[0].get("staff") == real[1].get("staff") and real[0].get("shift") != real[1].get("shift")
            and (real[0].get("min"), real[0].get("max"), real[0].get("until_day")) ==
                (real[1].get("min"), real[1].get("max"), real[1].get("until_day"))):
        # 「日勤と遅番は合わせて月8回まで」を、日勤8回・遅番8回の2本に分けていた（2026-11-07 の通しの試し）。1本の回数にする
        real = [{**real[0], "shift": [real[0].get("shift"), real[1].get("shift")]}]
    for it in real:
        if it["type"] == "unclear":
            raise Clarify(it.get("question") or "うまく聞き取れませんでした。もう一度言ってください")
        if it["type"] in NOT_YET:
            raise Clarify("この形の頼みは、まだ扱えません。別の言い方で言ってください")
    kinds = {"cells" if it["type"] in DATE_TYPES else "staff" if it["type"] == "staff_info"
             else "layer" if it["type"] == "layer_def" else "cancel" if it["type"] == "cancel"
             else "cover" if it["type"] == "cover" else "add_shift" if it["type"] == "add_shift"
             else "rule" for it in real}
    if len(kinds) > 1:
        raise Clarify("頼みが混ざっています。1つずつ言ってください")
    kind = kinds.pop()
    p = Proposal(kind=kind)
    sums = []
    for it in real:
        if it.get("reason") and _STRENGTH_TALK.search(str(it["reason"])):
            it["reason"] = _STRENGTH_TALK.sub("", str(it["reason"])).strip("、。 で") or None
        t = it["type"]
        reason = it.get("reason") or None
        p.reason = p.reason or reason
        if t == "add_shift":
            # 手元で読んだ頼み（addshift.py）。読み取りの返事はこの型にならない。頼まれた時だけ置き、全員が入れる
            if it["name"] in facility.symbols or any(st.name == it["name"] for st in facility.shift_types):
                raise Clarify(f"{it['name']}はもうある勤務です。足していません")
            p.shift_type = {"symbol": it["name"], "name": it["name"], "kind": "work", "start": it["start"],
                            "hours": it["hours"], "on_request_only": True, "spoken": True}
            sums.append(f"{it['name']}（{said_times(p.shift_type)}）を足しました")
        elif t == "layer_def":
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
            if len(ds) == 1 and re.search(rf"公休(?:は|を)?(?:今月|月)?(?:は|に)?{ds[0]}日(?!は|に|の|、|だけ)", text):
                # 「公休は今月は10日」を「10日 休み」と読んだ（2回目の通しの試し）。日数か日付かを聞く
                raise Clarify(f"公休の日数（月{ds[0]}日）ですか、{ds[0]}日の休みですか。日数なら「全員、公休は月{ds[0]}日」の"
                              "ように言ってください")
            fl = _cell_strength(it.get("strength"), text, t) if len(real) == 1 else _flex(it.get("strength"), "wish")
            p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 休み"
                                    + {"fixed": "（必ず）", "negotiable": "（できれば）"}.get(fl, ""),
                            "kind": "cell", "flex": fl, "params": {"in": ["公"]}, "who": who, "when": ds})
        elif t == "set_cell" and it.get("unit"):
            _support_cells(it, facility, staff, month, names, p)
        elif t in ("set_cell", "avoid_cell"):
            who = _names(it.get("staff"), names)
            ds = _days(it.get("days"), month)
            sym = _sym(it.get("shift"), facility)
            sname = facility.shift(sym).name
            on_req = facility.shift(sym).on_request_only and not facility.shift(sym).spoken   # 足した勤務はふつうの勤務の頼みと同じ強さ
            if t == "set_cell":
                p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日 {sname}", "kind": "cell",
                                "flex": "fixed" if on_req else _cell_strength(it.get("strength"), text, t)
                                if len(real) == 1 else _flex(it.get("strength"), "wish"),
                                "params": {"in": [sym]}, "who": who, "when": ds})
            else:
                # 「できれば休みにしないで」は配慮の強さ（相談できる）。前は言い方によらず本人の希望の強さで入り、年末の
                # 3日を「できれば」で頼んでも3日とも必ず勤務になった（作者 2026-11-07「年末年始に入れてしまって構わない」）。
                # 動かせない強さにはしない（読み取りが fixed と返しても、今までどおり本人の希望の強さ）
                # 読み取りが強さを返さないこともある（本物の Haiku は「できれば休みにしないで」に強さを付けなかった）ので、
                # 一言の言葉でも決める
                soft = len(real) == 1 and (_cell_strength(it.get("strength"), text, t) == "negotiable"
                                           or any(w in text for w in MAYBE_WORDS))
                p.rules.append({"name": f"{_who(who)} {'・'.join(map(str, ds))}日は{sname}にしない" + ("（できれば）" if soft else ""),
                                "kind": "cell", "flex": "negotiable" if soft else "wish", "params": {"not_in": [sym]},
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
            if re.search(r"日以内|日以上|間隔|あいだ|空け|あけ", text) and not re.search(r"次の日|翌日", text):
                # 「夜勤のあと3日以内に次の夜勤を入れないで」を「夜勤の次の日に夜勤を置かない」（全員の2連夜勤の禁止）に
                # していた（2回目の通しの試し）。勤務の間を何日空けるかは、まだ扱えない
                raise Clarify("勤務と勤務の間を何日空けるかは、まだ扱えません。表は変えていません")
            who = _names(it.get("staff"), names)
            a = _sym(it.get("first"), facility)
            nx = it.get("next")
            bs = list(dict.fromkeys(_sym(x, facility) for x in (nx if isinstance(nx, list) else [nx])))
            p.rules.append({"name": f"{'' if not who else _who(who) + ' '}{facility.shift(a).name}の次の日に"
                                    f"{'・'.join(facility.shift(b).name for b in bs)}を置かない",
                            "kind": "sequence", "flex": _seq_strength(it.get("strength"), text, len(real) == 1),
                            "params": {"if": [a], "next_not_in": bs}, "who": who, "when": None})
        elif t == "count_limit":
            who = _names(it.get("staff"), names)
            together = it.get("shift") if isinstance(it.get("shift"), list) else None
            if together is None and isinstance(it.get("shift"), str) and it["shift"] not in facility.symbols:
                # 「日勤と遅番合わせて月12回まで」を、本物の Haiku は "日・遅" の1つの文字で返した（2026-11-08）
                parts = [x for x in re.split(r"[・、,，/と＋+\s]+", it["shift"]) if x]
                if len(parts) > 1 and all(x in facility.symbols for x in parts):
                    together = parts
            if together is None and not it.get("shift") and re.search(r"合わせて|あわせて|合計|合算|トータル", text):
                # 「日勤と遅番合わせて月12回まで」を、本物の Haiku は勤務を空で返すこともある（2026-11-08）
                named = sorted((text.index(st.name), st.symbol) for st in facility.shift_types if st.name in text)
                if len(named) > 1:
                    together = [sym_ for _, sym_ in named]
            if it.get("shift") in ("現場", "出勤", "勤務", "出"):
                # 「〇〇さんの現場勤務は8日まで」。現場に入る勤務の全部（研修など頼まれた時だけの勤務と休みは数えない）の月の日数。
                # 前は「8日まで」を期限か「8」の記号と読んで聞き返した（作者 2026-11-07「月8日までだと分からないのはどうかしている」）
                together = [st.symbol for st in facility.shift_types if st.kind == "work" and not st.on_request_only]
            sym = _sym(together[0] if together else it.get("shift"), facility)
            syms = [_sym(x, facility) for x in together] if together else [sym]
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
                now = sum(1 for i, x in enumerate(row, 1) if x in syms and (when is None or i in when))
                hi = now or hi
            # 休みの勤務（公休・有給）は「日」で数える。下限と上限が同じなら1つの数で言う（「公休は月10〜10回」と出ていた）
            unit = "日" if facility.shift(sym).kind in ("rest", "leave") else "回"
            rng = (f"{lo}{unit}" if lo is not None and lo == hi else
                   f"{lo}〜{hi}{unit}" if lo is not None and hi is not None else
                   f"{hi}{unit}まで" if hi is not None else f"{lo}{unit}以上")
            params = {"shifts": syms}
            if lo is not None:
                params["min"] = lo
            if hi is not None:
                params["max"] = hi
            if not who and all(facility.shift(x).kind == "work" for x in syms) \
                    and any(not any(x in s.can for x in syms) for s in staff):
                # 夜勤に入れない人に、全員の夜勤の下限を付けない。休みの勤務は can に無いので付けない。
                # 全員が入れる勤務には付けない（前から帳面にある全員の回数と同じ物として「もう入っています」が当たるため）
                params["capable_only"] = True
            what = ("現場の勤務は" if it.get("shift") in ("現場", "出勤", "勤務", "出")
                    else "と".join(facility.shift(x).name for x in syms) + ("は合わせて" if len(syms) > 1 else "は"))
            p.rules.append({"name": f"{_who(who)} {what}{label}{rng}", "kind": "count",
                            "flex": _count_strength(it.get("strength"), text, len(real) == 1), "params": params,
                            "who": who, "when": when})
        elif t == "weekday_pref":
            who = _names(it.get("staff"), names)
            sym = _sym(it.get("shift"), facility)
            wds = [w for w in (it.get("weekdays") or []) if w in WEEKDAYS]
            if it.get("avoid") is False and facility.shift(sym).kind == "work" and \
                    re.search(r"(?:[月火水木金土日]曜日?|土日|週末|平日)(?:だけ|のみ|以外は)", text):
                # 「夜勤は金曜と土曜だけ」は、ほかの曜日に入れない頼み。毎週その曜日を夜勤にする頼みにすると、
                # 104マス動いた（2回目の通しの試し）。ほかの曜日に入れない形にする（同じ頼みが別にあれば足さない）
                rest = [w for w in WEEKDAYS if w not in wds]
                if any(o.get("type") == "weekday_pref" and o.get("avoid") is not False and o.get("shift") == it.get("shift")
                       and o.get("staff") == it.get("staff") and set(o.get("weekdays") or []) >= set(rest) for o in real):
                    continue   # 同じ人の同じ頼みがもうある（別の人の頼みは消さない）
                it = {**it, "avoid": True, "weekdays": rest}
                wds = rest
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
            # 「できれば」「なるべく」「望ましい」の曜日の頼みは配慮の強さ（前は言い方に関わらず本人の希望の強さで、「青木さん、
            # 木曜はできれば遅番」が毎週木曜の遅番を強く求め、ほかの頼みとぶつかって表が作れなかった。2回目の通しの試し）
            soft = len(real) == 1 and any(w in text for w in MAYBE_WORDS + ("望ましい", "が良い", "がいい"))
            p.rules.append({"name": f"{_who(who)} {jp}曜は{what}{upto}" + ("（できれば）" if soft else ""),
                            "kind": "weekday", "flex": "negotiable" if soft else "wish",
                            "params": {"weekdays": wds, ("not_in" if avoid else "in"): [sym]},
                            "who": who, "when": None})
        elif t == "coverage":
            sym = _sym(it.get("shift"), facility)
            lo, hi = _num(it.get("min")), _num(it.get("max"))
            if lo is None and hi is None:
                raise Clarify("何人にするか教えてください")
            # 日付を決めた人数（「28日は行事なので早番を3人」「1日はU2の早番はいなくていい」）は、その日だけの人数のルール
            # （今月だけ）。読み取りの型に日付の欄が無いので、手元で一言の文から日を読む。前は毎日の人数として入って表を
            # 大きく崩し（2026-11-07 の通しの試し）、次は聞き返すだけで、施設の日付の例外を言えなかった。その日は、同じ勤務の
            # 毎日・曜日の人数のルールが両立しない側だけ日付の数までゆるむ（rules._coverage_dates）
            dated = _coverage_days_said(text, month, len(real))
            if facility.shift(sym).spoken and lo and hi is None and "以上" not in text:
                hi = lo   # 足した勤務は頼まれた分だけ置く。下限だけだと、表を作る時に日勤の代わりに多く置かれる
            if lo is not None and hi is not None and lo > hi:
                raise Clarify("人数の下限が上限より大きくなっています。もう一度言ってください")
            raw = it.get("weekdays")
            wds = [w for w in (raw or []) if w in WEEKDAYS]
            if raw and len(wds) != len(raw):
                raise Clarify("曜日が読めませんでした。何曜日か教えてください")
            hol = it.get("holidays") is True
            if dated:
                # 日付と曜日を両方言った時（「27日の日曜は早番3人」）は日付だけ。曜日が日付と合わない時は聞き返す
                bad = [d for d in dated if wds and month.weekday(d) not in wds]
                if bad:
                    raise Clarify(f"{bad[0]}日は{'月火水木金土日'[WEEKDAYS.index(month.weekday(bad[0]))]}曜です。"
                                  "日付か曜日のどちらかで、もう一度言ってください")
                wds, hol = [], False
            if hol and not wds:
                # 祝日だけの人数は、人数のルールの型で表せない（曜日の無い人数は毎日に効く。敵対レビュー 2026-11-03）。
                # 前は「祝日だけの人数ですか」と言ったことを聞き返すだけで、どう言えば通るかが分からなかった（2026-11-08
                # 言い回し150件の試し）。毎日の同じ勤務の人数のルールで、もう祝日にも足りていればそう言い、
                # 足りなければ今月の祝日の日付で言い直す形を示す
                sname = facility.shift(sym).name
                every = next((r for r in rules or [] if r.kind == "coverage" and not r.params.get("weekdays")
                              and not r.when and r.params.get("op") in (">=", "==")
                              and (r.params.get("scope") == "all" or len(facility.units) <= 1)
                              and sym in _syms_of_rule(r)), None)
                if every is not None and lo is not None and hi is None and every.params["n"] >= lo:
                    raise Clarify(f"祝日の{sname}{lo}人以上は、もう入っています（毎日のルール「{every.name}」が祝日にも効いて"
                                  f"います）。表もルールも変えていません。祝日だけ人数を変えるルールは、まだ作れません")
                hols = [d for d in month.holidays if 1 <= d <= month.days]
                said = f"{sname}{lo if lo is not None else hi}人"
                raise Clarify("祝日だけの人数は、まだルールにできません。" + (
                    f"今月の祝日（{'・'.join(f'{d}日' for d in hols)}）だけなら「{'日と'.join(map(str, hols))}日は{said}」、"
                    if hols else "") + f"土日も同じでよければ「土日祝は{said}」と言ってください")
            # 今の同じ勤務の人数のルール（曜日の無いもの）と同じ数え方（記号の組・ユニットごとか）にそろえる
            base = next((r for r in rules or [] if r.kind == "coverage" and not r.params.get("weekdays")
                         and sym in (r.params["shifts"] if isinstance(r.params["shifts"], list)
                                     else [r.params["shifts"]])), None)
            # ユニットを言った（「東ユニットは早番を2人」）時はそのユニットだけ、「施設全体で」「合計」と言った時は施設全体。
            # 言っていなければ今のルールの数え方、無ければユニットごと（ユニットのある施設はユニットで見るのがふつう。
            # 2026-11-06 作者）。ユニットの名前は伏せる前の一言から手元で探す（Claude へ送る指示文は変えない）
            said_units = _said_units(text, facility, it.get("unit"))
            whole = len(facility.units) > 1 and not said_units and _said_whole(text)
            each_said = len(facility.units) > 1 and bool(re.search(
                r"どのユニットも|各ユニット|ユニットごと|それぞれのユニット|ユニットにつき|ユニットあたり|ユニット当たり|1ユニット", text))
            shifts = list(base.params["shifts"]) if base is not None else [sym]
            # 曜日の無い毎日の人数だけ、今の表のいつもの数に当てる（「土日は早番を2人に」は変えたい頼みなので当てない）
            n_said = lo if lo is not None else hi
            same_all = any(r.kind == "coverage" and r.params.get("scope") == "all" and not r.params.get("weekdays")
                           and not r.when and set(_syms_of_rule(r)) == set(shifts) and r.params.get("n") == n_said
                           for r in rules or [])
            # 施設全体の同じ人数のルールがもうあれば施設全体（作った表が段を下げて崩れていると、いつもの数を表から
            # 当てられず、「夜勤は毎日ちょうど2人」がユニットごと2人のままだった。2回目の通しの試し）
            typical = (None if said_units or whole or each_said or it.get("weekdays") or dated else
                       "all" if same_all and len(facility.units) > 1 else
                       _typical_scope(n_said, shifts, month, staff, list(facility.units)))
            if said_units or each_said:
                scope = "each_unit"
            elif dated or whole or typical == "all":
                # ユニットを言わない日付の人数（「28日は行事なので早番を3人」）は、その日の施設全体の人数
                whole = len(facility.units) > 1
                scope = "all"
            elif typical == "each_unit":
                scope = "each_unit"
            elif base is not None:
                scope = base.params.get("scope", "all")
            else:
                scope = "each_unit" if len(facility.units) > 1 else "all"
            ops = ([("==", lo)] if lo is not None and lo == hi else
                   [(">=", lo)] * (lo is not None) + [("<=", hi)] * (hi is not None))
            jp = ("".join("月火水木金土日"[WEEKDAYS.index(w)] for w in wds) + ("祝" if hol else "")
                  if wds else _days_jp(dated) if dated else "毎日")
            sname = facility.shift(sym).name
            for op, n in ops:
                params = {"shifts": shifts, "scope": scope, "op": op, "n": n}
                if said_units:
                    params["units"] = said_units
                if wds:
                    params["weekdays"] = wds
                    params["holidays"] = hol
                # 2ユニットで合計が倍になることを名前で見せる
                each = (f"（{'・'.join(said_units)}）" if said_units else
                        "（ユニットごと）" if scope == "each_unit" else
                        "（施設全体）" if whole else "")
                p.rules.append({"name": f"{jp}の{sname}は{n}人" + {"==": "", ">=": "以上", "<=": "まで"}[op] + each,
                                "kind": "coverage", "flex": _count_strength(it.get("strength"), text, len(real) == 1),
                                "params": params, "who": None, "when": dated or None})
        elif t == "consecutive_limit":
            who = _names(it.get("staff"), names)
            mx = _num(it.get("max"))
            if not mx:
                raise Clarify("何日まで続けてよいか教えてください")
            run = _sym(it["shift"], facility) if it.get("shift") else _said_run_shift(text, facility)
            if not run and _named_run_shift_unclear(text, facility):
                raise Clarify("どの勤務を続けて何日までにするか、「早番は続けて2日まで」のように言ってください")
            if run:
                nm = facility.shift(run).name
                # 人を名指しした勤務の続く日の上限（「〇〇さんの夜勤はできるだけ単発で」）は、その人の毎月の強めのルール。
                # 「できるだけ」でも配慮にせず運用の都合にする（作者 2026-11-07「この場合は個人のハードルール。強めのソフトか
                # ハードに」）。必ず系の言葉なら動かせない。「ソフトルールで」と強さを言った時は、言ったとおり相談できるのまま
                fl = _count_strength(it.get("strength"), text, len(real) == 1)
                if who and fl == "negotiable" and not any(w in text for w in ("ソフトルール", "ソフトで", "ソフトに")):
                    fl = "operational"
                p.rules.append({"name": f"{_who(who)} {nm}を2日続けない" if mx == 1 else f"{_who(who)} {nm}は続けて{mx}日まで",
                                "kind": "max_run", "flex": fl,
                                "params": {"shifts": [run], "max": mx}, "who": who, "when": None})
                ex = [n for n in (_names(it.get("except_staff"), names) or []) if n not in (who or [])]
                if ex and not who:
                    # 「ただし〇〇さんは当たらない」。全員の上限から、その人を外す（rules.expand の max_run の except_who）。
                    # 前は除く人を受ける所が無く、「この形の頼みは、まだ扱えません」で止まった（作者 2026-11-07）
                    p.rules[-1]["name"] += f"（{_who(ex)}を除く）"
                    p.rules[-1]["meta"] = {**p.rules[-1].get("meta", {}), "except_who": ex}
            else:
                p.rules.append({"name": f"{_who(who)} 連勤は{mx}日まで", "kind": "max_run",
                                "flex": _count_strength(it.get("strength"), text, len(real) == 1),
                                "params": {"shifts": "work", "max": mx}, "who": who, "when": None})
            if who and re.search(PERMIT_WORDS, text):
                # 名指しで「今月だけ」「〜てもいい」「許す」と言った上限は、全員の上限の例外の頼み（rules._run_exceptions）。
                # ただ「小島さん、連勤6日まで」と言った時は、全員の連勤5日がもう決めているので今までどおり
                p.rules[-1]["meta"] = {**p.rules[-1].get("meta", {}), "exception": True}
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
                    raise Clarify(f"{who[0]}さんの有給は何日ですか", need="leave_count")
                if cap == 0:
                    raise Clarify(f"{who[0]}さんの有給の上限は0日になっています。何日にするか教えてください")
                n = cap
            elif n <= 0:
                raise Clarify("有給の日数を教えてください")
            lv = _leave_sym(facility)
            if lv is None:
                raise Clarify(NO_LEAVE)
            if re.search(r"多くても|上限|超えない|[0-9０-９一二三四五六七八九十]+日(?:まで(?!に)|以内)", text):
                # 「有給は多くても月2日まで」は上限。下限と読むと有給が増えた（2026-11-07 の通しの試し）
                p.rules.append({"name": f"{who[0]}さん 有給は月{n}日まで", "kind": "count", "flex": "operational",
                                "params": {"shifts": [lv], "max": n}, "who": who, "when": None})
            else:
                # 日数を言った有給は、その日数ちょうど。下限だけだと、表を作る計算が有給を空いた日の埋め草に使い、
                # 「有給を1日」の人が8日になった（2026-11-07 の通しの試し）
                p.rules.append({"name": f"{who[0]}さん 有給{n}日（日付は任せる）", "kind": "count", "flex": "wish",
                                "params": {"shifts": [lv], "min": n, "max": n}, "who": who, "when": None})
        elif t == "staff_info":
            who = _names(it.get("staff"), names) or []
            fm = _num(it.get("from_month"))
            if fm is None and re.search(r"(?<!再)来月", text):
                fm = month.month % 12 + 1   # 読み取りは開いている月を知らないので、「来月から」は月を空で返す（2026-11-08）
            elif fm is None and re.search(r"再来月", text):
                fm = (month.month + 1) % 12 + 1
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
    named = {masker.to_name[c] for c in re.findall(r"職員\d{2,3}", masker.mask(text)[0]) if c in masker.to_name}
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
    # everyone は読み直し（施設の文脈を見た Sonnet）が、人を言っていない一言を全員のルールと読んだ印。session が名前の無い一言にだけ残す
    everyone = bool(re.search(r"全員|みんな|皆(?!勤)", text)) or any(it.get("everyone") is True for it in real)
    per_nobody = any(not r.get("who") for r in p.rules if r["kind"] in ("count", "max_run"))
    # 「みんな夜勤は月4回までで、井上さんだけは3回までにして」。全員と名指しが1つの一言に入る時は、言った人が
    # 全員、名指しの回数のルールに入っている時だけ通す（名指しは with_overrides で全員向けより優先する）。
    # 名前が出たのに、その人の分の返事が無ければ今どおり聞き返す
    per_named = {w for r in p.rules if r["kind"] in ("count", "max_run") for w in (r.get("who") or [])}
    both = everyone and bool(named) and named <= per_named and not any(
        not r.get("who") for r in p.rules if r["kind"] not in ("layer", "cell", "count", "max_run")) and not any(
        not any(w.endswith(n) for n in not_name) and not any(w.endswith(n) for n in named)
        for w in re.findall(r"([ぁ-ゖァ-ヺー一-龥々]{1,6}?)(?:さん|くん|ちゃん)", text))
    # 「ただし〇〇さんは当たらない」で名前が出た全員の上限（except_who）。言った名前が全部、除く人に入っている時は全員のルール
    excepted = {w for r in p.rules for w in (r.get("meta") or {}).get("except_who", [])}
    except_ok = bool(named) and named <= excepted and all(
        (r.get("meta") or {}).get("except_who") for r in p.rules if r["kind"] in ("count", "max_run") and not r.get("who"))
    if cells_nobody or (not except_ok and ((per_nobody and (not everyone or (said and not both)))
                                           or (said and not everyone and rules_nobody))):
        raise Clarify(WHO_UNKNOWN + ("" if cells_nobody else EVERYONE_HINT))
    if kind == "cells":
        p.scope = "once"
        # 言い直し（「赤坂さんじゃなくて井上さん、10日が休みで」）。読み取りは新しい人の頼みだけを返すので、
        # 言った人のうち頼みに無い人が1人の時だけ印を付け、前の頼みを外すかは帳面を見て確かめる（session）
        others = sorted(named - {w for r in p.rules for w in (r.get("who") or [])})
        if any(w in text for w in REPHRASE_WORDS) and len(others) == 1 and len(p.rules) == 1 \
                and len(p.rules[0].get("who") or []) == 1:
            p.replace_from = others
        redo = next((it["redo_from"] for it in real if it.get("redo_from")), None)
        if redo and len(p.rules) == 1 and len(p.rules[0].get("who") or []) == 1:
            p.replace_from, p.replace_days = list(p.rules[0]["who"]), list(redo)
    elif kind in ("cancel", "cover"):
        p.scope = "once"   # 取り消しは前の頼みを外すだけ、穴埋めは記録するだけ。聞かない
    elif kind in ("rule", "layer"):
        p.scope = "rule" if p.until else read_period(text)
        if any(r.get("kind") == "coverage" and r.get("when") for r in p.rules):
            p.scope = "once"   # 日付を決めた人数は、その月の頼み（毎月か聞かない）
        p.from_next = p.scope == "rule" and not p.until and bool(re.search(r"(?:来月|翌月)(?:から|以降)(?!.*元に戻)", text))
    else:  # 職員の情報は言われた月から。聞かない
        p.scope = "rule"
    if kind == "cells" and "か" in text:
        # 「篠原さん、5日は休みか早番」を、休みと早番の2つの指定（ぶつかる）にしていた（2回目の通しの試し）。同じ人の
        # 同じ1日に2つの「その勤務にする」がある時は、どちらかでよい1つのマスの頼みにまとめる
        merged, seen = [], {}
        for r in p.rules:
            key = (tuple(r.get("who") or []), tuple(r.get("when") or []))
            if r.get("kind") == "cell" and "in" in r.get("params", {}) and len(r.get("when") or []) == 1 \
                    and len(r.get("who") or []) == 1 and key in seen:
                first = seen[key]
                first["params"] = {"in": list(dict.fromkeys(first["params"]["in"] + r["params"]["in"]))}
                first["name"] = f"{_who(first['who'])} {first['when'][0]}日は" + "か".join(
                    facility.shift(x).name for x in first["params"]["in"])
                continue
            if r.get("kind") == "cell" and "in" in r.get("params", {}):
                seen[key] = r
            merged.append(r)
        p.rules = merged
    p.summary = "、".join(sums) if sums else "、".join(r["name"] for r in p.rules if "moved_to" not in r.get("meta", {}))
    return p
