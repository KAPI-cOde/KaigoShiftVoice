"""話の続きを手元で読む。聞き返しへの答えと、言い直し（「20日じゃなくて21日」）。Claude へ送る指示文は変えない。

- 聞き返しへの答え。「篠原さんの有給は何日ですか」に「2日分です」と答えても、答えだけでは誰の何か分からず、読み取りは
  「頼みが見つかりません」と返した（2026-11-06 AI のテストプレイ）。聞いた時の読み取り結果を覚えておき、答えを足りない所に入れる。
  答えの形は全文で見る。ほかの中身が残る一言は答えにせず、今までどおりの一言として読む
- 言い直し。本物の Haiku は「20日じゃなくて21日」を 移す（move）か、取り消しと休みの2件で返し、前の頼みが残るか
  「頼みが混ざっています」で止まった。前に話した頼みが当たる時だけ、新しい日の頼みと、外す日にそろえる
"""
from __future__ import annotations

import re

from .addshift import _named, tight

_ZEN_KANJI = str.maketrans({"二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "一": "1"})
_START = r"(?:はい|うん|ええ|えっと|えーと|えー|あ|じゃあ)?"
_END = r"(?:だけ|ほど|くらい|ぐらい|です|でした|だった|で|を|に|ね|よ|ほしい|欲しい|ください|お願いします|そうです|って|して|と?聞いて(?:い)?ます|と?言って(?:い)?ます|と?聞いた|と?言ってた)*"
_ANY_DAY = r"(?:日(?:付|にち)?は?(?:いつ|どこ|どの日|どれ|お?任せ).*)?"
_LEAVE_DAYS = re.compile(_START + r"(?:有給|有休|年休)?(?:は|を)?(\d{1,2})日(?:分|間)?" + _END + _ANY_DAY)
_DATES = re.compile(_START + r"(?:有給|有休|年休)?(?:は|を)?(\d{1,2})日?((?:と|、|,)\d{1,2}日?)+" + _END + _ANY_DAY)
_DAYS = re.compile(_START + r"(\d{1,2})日?((?:と|、|,)\d{1,2}日?)*" + _END)
_DIRECT = re.compile(_START + r"(?:すみません|ごめん|ごめんなさい)?(\d{1,2})日(?:は|の|を)?"
                     r"(?:じゃなくて|じゃなく|ではなくて|ではなく|でなくて)(\d{1,2})日" + _END)
_ORDER = {"day_off", "set_cell", "avoid_cell"}
BACK_WORDS = re.compile(r"(?:元|もと|前)の(?:勤務|シフト|まま)|(?:元|もと)に|戻")   # 「元の勤務に戻して」


def _plain(masked: str) -> str:
    """数の読み方をそろえ、空白と読点を外す（「二日分」→「2日分」）。"""
    return tight(re.sub(r"[一二三四五六七八九](?=日|回)", lambda m: m[0].translate(_ZEN_KANJI), masked))


def _shift_said(t: str, facility) -> str | None:
    """全文が勤務の名前そのもの（「早番です」）なら、その記号。"""
    words = {"休み": "公", "お休み": "公"} | {st.name: st.symbol for st in facility.shift_types} | \
        {st.symbol: st.symbol for st in facility.shift_types if len(st.symbol) > 1}
    names = "|".join(map(re.escape, sorted(words, key=len, reverse=True)))
    m = re.fullmatch(_START + f"({names})" + _END, t)
    return words[m[1]] if m and words[m[1]] in facility.symbols else None


def fill_answer(more: dict, masked: str, facility) -> list[dict] | None:
    """聞き返しへの答えを、聞いた時の読み取り結果に入れる。答えの形でなければ None（今までどおりの一言として読む）。"""
    t, need, items = _plain(masked), more["need"], more.get("items") or []
    if need == "leave_count":
        one = next((it for it in items if it.get("type") == "leave_count"), None)
        if one is None:
            return None
        if _DATES.fullmatch(t):
            return [{"type": "set_cell", "staff": one.get("staff"), "days": [int(x) for x in re.findall(r"\d{1,2}", t)],
                     "shift": "有", "reason": one.get("reason")}]
        m = _LEAVE_DAYS.fullmatch(t)
        return [{**it, "count": int(m[1])} if it is one else it for it in items] if m else None
    if need == "days":
        if not _DAYS.fullmatch(t):
            return None
        days = [int(x) for x in re.findall(r"\d{1,2}", t)]
        out = [{**it, "days": days} if it.get("type") in _ORDER and not it.get("days") else it for it in items]
        return out if out != items else None
    if need == "shift":
        sym = _shift_said(t, facility)
        if sym is None:
            return None
        out = [{**it, "shift": sym} if it.get("type") in ("set_cell", "avoid_cell", "move") and not it.get("shift") else it
               for it in items]
        return out if out != items else None
    if need == "back_shift":
        sym = _shift_said(t, facility)
        return None if sym is None else [{"type": "set_cell", "staff": more["who"], "days": [more["day"]], "shift": sym,
                                          "strength": "wish", "reason": None}]
    if need == "shift_name":
        m = re.fullmatch(_START + r"(?:名前は)?([一-龥々ァ-ヺーA-Za-z0-9]{1,6})" + _END, t)
        return _named(m[1], more["times"], True, facility) if m else None
    return None


def _spoken(rules: dict, who: str | None, day: int) -> list:
    """その日に、この月に話したマスの頼み（who が None なら誰のでも）。1人の頼みだけ。"""
    return [r for r in rules.values() if r.kind == "cell" and r.when and day in r.when and len(r.who or []) == 1
            and (who is None or r.who == [who]) and str(r.meta.get("source", "")).startswith("話した")]


def _one(v) -> str | None:
    if isinstance(v, list):
        return v[0] if len(v) == 1 else None
    return v


def bare_redo(masked: str, rules: dict) -> list[dict] | None:
    """人を言わない「20日じゃなくて21日」。読み取りは何も読めないので、その日に話した頼みの人が1人の時だけ手元で読む。"""
    m = None if "職員" in masked else _DIRECT.fullmatch(_plain(masked))
    if not m:
        return None
    who = {r.who[0] for r in _spoken(rules, None, int(m[1]))}
    if len(who) != 1:
        return None
    return [{"type": "move", "staff": who.pop(), "shift": None, "from_day": int(m[1]), "to_day": int(m[2]), "reason": None}]


def as_redo(items: list[dict], rules: dict) -> list[dict]:
    """同じ人の言い直しを、新しい日の頼み1件（redo_from に外す日）にそろえる。前に話した頼みが無ければ items のまま
    （研修の日を移す一言は、話した頼みが無いので今までどおり移す）。"""
    real = [it for it in items if it.get("type") != "none"]
    if len(real) == 1 and real[0].get("type") == "move":
        it = real[0]
        who, a, b = _one(it.get("staff")), it.get("from_day"), it.get("to_day")
        old = [r for r in _spoken(rules, who, a) if "in" in r.params] if who and a and b and a != b else []
        if not old:
            return items
        was, said = old[0].params["in"][0], it.get("shift")
        sym = was if not said or (said == "公" and was != "公") else said
        new = {"staff": who, "days": [b], "strength": old[0].flex, "reason": it.get("reason"), "redo_from": [a]}
        return [{"type": "day_off", **new} if sym == "公" else {"type": "set_cell", "shift": sym, **new}]
    drop = [it for it in real if it.get("type") == "cancel" or (it.get("type") == "set_cell" and not it.get("shift"))]
    keep = [it for it in real if it.get("type") in ("day_off", "set_cell") and it not in drop]
    if len(drop) != 1 or len(keep) != 1 or not drop[0].get("days") or _one(drop[0].get("staff")) != _one(keep[0].get("staff")):
        return items
    who, days = _one(keep[0].get("staff")), drop[0]["days"]
    if not who or not all(_spoken(rules, who, d) for d in days):
        return items
    return [{**keep[0], "redo_from": list(days)}]


def narrowed(r, gone: set[int]) -> dict:
    """前の頼みから、gone の日を外す形（残る日が無ければ頼みごと外す）。"""
    rest = [d for d in r.when if d not in gone]
    name = re.sub(r"[\d・]+日", "・".join(map(str, rest)) + "日", r.name, count=1) if rest else r.name
    return {"rule_id": r.id, "rest": rest, "name": name}


def same_cell_drops(new_rules: list[dict], rules: dict) -> list[dict]:
    """新しいマスの頼みに置き換わる、同じ人・同じ日の前の頼み。前は両方が残り、守れない方が「希望を外せる形で作りました」の
    赤字を以後の直しのたびに出した（2026-11-06 AI のテストプレイで1回の場面で8回）。
    置き換えるのは話した頼みだけで、作る前に貼った希望は残す。「〇日は早番にしない」は、ぶつかる前の頼みだけ置き換える。"""
    out = []
    for r in rules.values():
        if r.kind != "cell" or not r.when or len(r.who or []) != 1 \
                or not str(r.meta.get("source", "")).startswith(("話した", "直した")):
            continue
        gone: set[int] = set()
        for rd in new_rules:
            q = rd["params"]
            if rd["kind"] == "cell" and r.who[0] in (rd.get("who") or []) \
                    and ("in" in q or set(r.params.get("in", [])) & set(q.get("not_in", []))):
                gone |= set(r.when) & set(rd.get("when") or [])
        if gone:
            out.append(narrowed(r, gone))
    return out


def same_count(rd: dict, r, scope: str, until: str | None) -> bool:
    """新しい回数のルールが、同じ人・同じ勤務の前の回数のルール r に代わるか。前は3回と2回の2件が右の枠に並んだ
    （2026-11-06 AI のテストプレイ）。話したルールだけで、言った側（上限・下限）を全部言い直している時に代わる。
    毎月のルールを今月だけの数では消さない（来月の決めが消えるため。両方残り、今月は厳しい方が効く）。
    今月だけの厳しい数も、毎月のゆるい数では消さない。"""
    if rd.get("kind") != "count" or r.kind != "count" or not rd.get("who") or sorted(rd["who"]) != sorted(r.who or []):
        return False
    if rd.get("when") is not None and (r.when is None or not set(r.when) <= set(rd["when"])):
        return False   # 15日までの回数で、月全体の回数を消さない（新しいルールが前のルールの日を全部含む時だけ代わる）
    p, q = rd["params"], r.params
    if set(p.get("shifts") or []) != set(q.get("shifts") or []) or not str(r.meta.get("source", "")).startswith("話した"):
        return False
    if r.scope == "rule" and not r.meta.get("until") and (scope != "rule" or until):
        return False
    if (r.scope == "once" or r.meta.get("until")) and scope == "rule" and not until and \
            (p.get("max", 0) > q.get("max", p.get("max", 0)) or p.get("min", 0) < q.get("min", p.get("min", 0))):
        return False   # 今月の厳しい数を、毎月のゆるい数では消さない（29日まで3回の後の「毎月4回まで」で、試験の前に4回入らないように）
    return {s for s in ("min", "max") if q.get(s) is not None} <= {s for s in ("min", "max") if p.get(s) is not None}
