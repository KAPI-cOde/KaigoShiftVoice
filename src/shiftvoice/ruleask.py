"""ルールを見る・確かめる・整理する問いに、Claude へ送らずに手元で答える（作者 2026-11-08「ルールの管理なども、
すべてチャットのやりとりを通じてできると認知負荷が低くて助かる」）。足す・数を変える・取り消すは今までどおり一言の流れ。

- broken  外れを聞く（「外れているルールは？」）。今の表で守れていないルールを、ルールごとに1行（どこで外れたか）
- tidy    整理を頼む（「ルールを整理して」「いらないルールある？」）。整理の画面と同じ見立て（tidy.py と cleanup.py）を1件1行
- list    一覧を頼む（「ルールを見せて」）。右の一覧と同じ分け方で、種類ごとの件数を1行ずつ
- undo・next・table  「今のなしで」「12月の表を作って」「表を見せて」。押すボタンの場所だけを言う（where_answer）
- help    使い方を聞く（「使い方を教えて」「何ができるの？」）・あいさつ（「こんにちは」）・お礼（「ありがとう」）。言える一言の
          例を返す。初めて触る人の最初の一言が、前は「表を変える頼みが見つかりませんでした」で止まった（2026-11-08、本物の Claude で確かめた）

一言の全文が問いの形の時だけ答える（文の中の言葉では拾わない）。頼みと混ざった一言（「ルールを整理して、それから
岡田さん15日休み」）や、ルールの中身を言う一言（「ルールで夜勤は4回まで」）、外す・消す頼み（「いらないルールを消して」）は
今までどおり Claude へ送る。どの答えも表とルールを変えない。
整理の答えは計算で確かに言える物だけにする。Claude の見立て（tidy の tidier）は数十秒かかり、一言の返事はサーバーの
lock の中で作るので、その間ほかの操作が止まる。AI の見立ては整理の画面で見る。

返事の添え物（Reply.extra）
- topic    "broken" / "tidy" / "list"
- lines    1件1行の文の並び（message にも同じ行を改行でつないで入れる）
- ids      答えに出たルールの id（画面が一覧で光らせる時に使う）
- actions  画面がボタンにできる物 [{"label": "ルールのページを開く", "open": "rules"}]。open は "rules" か "tidy"
"""
from __future__ import annotations

import calendar
import re

from .build import month_rules
from .rules import _syms, check_table
from .store import Store

LIST_SHOWN = 8       # 整理の候補を1行ずつ出す数の上限（残りは整理の画面へ）
NAMES_AT = 3         # 一覧の種類ごとの行で、ルールの名前まで出す件数の上限
PLACES = 3           # 外れたルールごとに、外れた所を出す数の上限
OPEN_RULES = {"label": "ルールのページを開く", "open": "rules"}
OPEN_TIDY = {"label": "整理の画面を開く", "open": "tidy"}

# 句読点と空白。声の文字起こしの「？」もここで外す
_NOISE = re.compile(r"[\s、。，．,.!！?？…・「」『』]")
_F = r"(?:えっと|えーと|ねえ|ねぇ|あの|じゃあ|では|それじゃ|あと|ちょっと|ところで|すみません|すいません|それと)*"
_R = r"(?:ルール|決まり|きまり)"
_NOW = (r"(?:今の表で|この表で|今月の表で|表で|今の表の|この表の|表の|今の|いまの|今月の|この月の|今ある|いまある|"
        r"全部の|ぜんぶの|すべての|全ての|毎月の|今月だけの)?")
_POL = r"(?:ください|下さい|ほしい|欲しい|もらえる|もらえますか|くれる|くれますか)?"
_END = r"(?:の|か|かな|かね|ですか|でしょうか|だっけ|っけ|ね|よ|な)?"
_ASK = (r"(?:ある|あります|ありますか|あるの|あるかな|あるか|ありそう|ない|ないか|ないかな|ありませんか|"
        r"どれ|どれですか|どこ|なに|何|なん|何ですか|なんですか|"
        r"教えて|見せて|出して|知りたい|見たい|確認したい|確かめたい|挙げて|あげて|並べて)?")
_VB = (r"(?:外れて(?:い)?る|外れた|はずれて(?:い)?る|守れて(?:い)?ない|守れなかった|守られて(?:い)?ない|まもれて(?:い)?ない|"
       r"破れて(?:い)?る|破って(?:い)?る|違反して(?:い)?る|満たせて(?:い)?ない|満たして(?:い)?ない)")
_SPOT = r"(?:の|もの|物|ところ|所|とこ|こと)"
_TIDY_N = r"(?:整理|せいり|片付け|片づけ|かたづけ|掃除|そうじ)"
_TIDY_DO = (r"(?:して|しよう|したい|してほしい|して欲しい|してください|して下さい|お願い|お願いします|する|しようか|"
            r"できる|できますか|しといて|しておいて)?")
_NEEDLESS = (r"(?:いらない|要らない|いらなそうな|不要な|不要の|余計な|無駄な|むだな|使われて(?:い)?ない|使って(?:い)?ない|"
             r"効いて(?:い)?ない|きいて(?:い)?ない|働いて(?:い)?ない|役に立って(?:い)?ない|まとめられる|まとめられそうな|"
             r"重なって(?:い)?る|かぶって(?:い)?る|ダブって(?:い)?る|重複して(?:い)?る|同じことを決めて(?:い)?る|"
             r"片付けて(?:も)?いい|片付けられる|消して(?:も)?いい|消せる|外して(?:も)?いい|減らせる|古い)")
_SHOW = r"(?:見せて|見たい|教えて|出して|表示して|確認したい|確認させて|確かめたい|知りたい|並べて|開いて|見る)"
_HOW_MANY_W = r"何件|なんけん|いくつ|何個|なんこ|どのくらい|どれくらい"
_HOW_MANY = rf"(?:{_HOW_MANY_W})"

_TOPICS = (
    ("broken", (
        _F + _NOW + _VB + r"(?:今の)?" + _R + r"(?:は|が|って|を)?" + _ASK + _POL + _END,
        _F + r"(?:どの|どんな|何の|なんの)" + _R + r"(?:が|は)" + _VB + r"(?:の|か|かな|ですか|んですか|のですか)?",
        _F + _NOW + _R + r"(?:で|の中で|のうち|に)" + _VB + _SPOT + r"(?:は|が|って)?" + _ASK + _POL + _END,
        _F + _NOW + _R + r"(?:は|って)?(?:全部|ぜんぶ|すべて|みんな|ちゃんと|きちんと)?"
        r"(?:守れて(?:い)?る|守れて(?:い)?ます|守られて(?:い)?る|守られて(?:い)?ます)" + _END,
        _F + _NOW + r"(?:ルール)?違反(?:は|が|して(?:い)?る" + _SPOT + r"?)?" + _ASK + _END,
        _F + _NOW + r"(?:外れ|" + _VB + _SPOT + r")(?:は|が|って)?" + _ASK + _POL + _END,
        _F + _NOW + _R + r"の外れ(?:は|が|って|を)?" + _ASK + _POL + _END,     # 帯の「ルールの外れ」と同じ言い方
        _F + _NOW + r"外れ(?:は|が|って)?" + _HOW_MANY + r"(?:ある|あります|ありますか|あるの)?" + _END,
        _F + _NOW + _R + r"(?:が|は)" + _VB + _SPOT + r"(?:は|が|って)?" + _ASK + _POL + _END,
    )),
    ("tidy", (
        _F + _NOW + _R + r"(?:を|の)?" + _TIDY_N + r"(?:を)?" + _TIDY_DO + _END,
        _F + _NOW + _NEEDLESS + _R + r"(?:は|が|って|を)?" + _ASK + _POL + _END,
        _F + _NOW + _R + r"(?:を|は)?(?:減らしたい|減らして|減らせる|減らせますか|まとめて|まとめたい|まとめられる|"
        r"まとめられますか|すっきりさせて|すっきりさせたい|すっきりさせよう)" + _POL + _END,
        _F + _NOW + _R + r"(?:が|は)?(?:多すぎる|多すぎ|多い|増えすぎ|増えすぎた|増えすぎてる|ごちゃごちゃ|"
        r"ごちゃごちゃして(?:い)?る|散らかって(?:い)?る)" + _END,
    )),
    ("list", (
        _F + _NOW + _R + r"(?:を|の)?(?:一覧|リスト|全部|ぜんぶ|すべて|全て|全体)?(?:を)?" + _SHOW + _POL + _END,
        _F + _NOW + _R + r"(?:の)?(?:一覧|リスト|全部|ぜんぶ|全体)(?:は|を)?(?:どこ|ある|ありますか)?" + _END,
        _F + _NOW + _R + r"(?:は|って)(?:何|なに|なん|どれ|どんな|" + _HOW_MANY_W + r"|どうなって(?:い)?る)?"
        r"(?:が|ですか|がある|がありますか|ある|あります|ありますか|あるの)?" + _END,
        _F + _NOW + r"(?:どんな|何の|なんの|どういう|どの)" + _R + r"(?:が|は)?"
        r"(?:入って(?:い)?る|効いて(?:い)?る|ある|あります|ありますか|あるの)?" + _END,
        _F + _NOW + _R + r"(?:は|が)?(?:全部で|ぜんぶで|合計で)?" + _HOW_MANY + r"(?:ある|あります|ありますか|あるの)?" + _END,
        _F + r"(?:今|いま)?(?:ある|入って(?:い)?る|効いて(?:い)?る|使って(?:い)?る)" + _R + r"(?:は|を|って)?" + _ASK + _POL + _END,
    )),
)
_HELLO = r"(?:こんにちは|こんにちわ|こんばんは|こんばんわ|おはよう|おはようございます|はじめまして|初めまして|よろしく|よろしくね|" \
         r"よろしくお願いします|よろしくおねがいします|どうも|ハロー|やあ)"
_THANKS = r"(?:ありがとう|ありがとうございます|ありがとうございました|ありがと|助かりました|たすかりました|助かる|サンキュー)"
_HELP = (r"(?:使い方|つかいかた|使いかた)(?:を|は|が)?(?:教えて|おしえて|知りたい|わからない|分からない|わかりません|分かりません|"
         r"どうするの|どうするんですか|は)?" + _POL + _END,
         r"(?:何|なに|なん)(?:が|を)(?:できる|出来る|できます|出来ます|頼める|言える)(?:の|んですか|のですか|か)?" + _END,
         r"(?:どう|どうやって)(?:使う|使えば|使えばいい|つかう|すれば|すればいい|したら|したらいい)(?:の|んですか|ですか)?" + _END,
         r"(?:ヘルプ|help|へるぷ|説明して|せつめいして)" + _POL + _END,
         r"(?:例|れい|話し方|言い方)(?:を|は)?(?:教えて|見せて|出して)" + _POL + _END)
_UNDO = (r"(?:やっぱり|やっぱ|あ)?(?:今の|いまの|さっきの|直前の|最後の)(?:は|も)?(?:なし|無し|ナシ|取り消し|取り消して|とりけし|とりけして|"
         r"やめ|やめて|キャンセル|戻して|もどして|なかったことに|なかったことにして)(?:で|にして|に)?" + _POL + _END,
         r"(?:元に|もとに)(?:戻して|もどして|戻したい)" + _POL + _END,
         r"(?:取り消して|取り消したい|とりけして|キャンセルして|やり直したい)" + _POL + _END)
_NEXT = (r"(?:来月|次の月|翌月|\d{1,2}月)(?:の)?(?:表|勤務表|シフト)(?:を|は)?(?:作って|つくって|作りたい|作れる|作れますか|"
         r"どうやって作るの|どう作るの|どうやって作る)" + _POL + _END,)
_SHOW_TABLE = (r"(?:今の|いまの|この月の)?(?:表|勤務表|シフト表)(?:を|は)?(?:見せて|出して|表示して|見たい|どこ|どこですか)?" + _POL + _END,)
_TOPICS = _TOPICS + (
    ("undo", tuple(_F + u for u in _UNDO)),
    ("next", tuple(_F + n for n in _NEXT)),
    ("table", tuple(_F + t for t in _SHOW_TABLE)),
    ("help", tuple(_F + h for h in _HELP)),
    ("hello", (_F + _HELLO + r"(?:です|ございます)?" + _END,)),
    ("thanks", (_F + _THANKS + r"(?:です|ございます)?" + _END,)),
)
_COMPILED = tuple((t, tuple(re.compile(p) for p in ps)) for t, ps in _TOPICS)


def rule_question(text: str) -> str | None:
    """一言の全文が、ルールを見る・確かめる・整理する問いなら "broken" / "tidy" / "list"。そうでなければ None。"""
    t = _NOISE.sub("", text or "")
    if not t:
        return None
    for topic, pats in _COMPILED:
        if any(p.fullmatch(t) for p in pats):
            return topic
    return None


# ---- 右の一覧と同じ分け方（static/index.html の monthOnly・groupCards を写した物。名前の並べ方は見ない）

def month_only(c: dict, year: int, month: int) -> bool:
    """今月だけのルール（今月の中で終わる回数や並び）。日付つきのマスの頼みは表に出るので入れない。"""
    until = (c.get("meta") or {}).get("until") or ""
    return c.get("kind") != "cell" and (c.get("scope") == "once" or until[:7] == f"{year:04d}-{month:02d}")


def group_cards(cards: list[dict], roster: list[str], year: int, month: int) -> dict:
    """カード（state の cards と同じ形）を、今月だけ・施設のルール（守る・できれば）・人ごと・日付の頼みに分ける。"""
    only = [c for c in cards if month_only(c, year, month)]
    monthly = [c for c in cards if c.get("scope") == "rule" and not month_only(c, year, month)]
    rest = [c for c in cards if not month_only(c, year, month) and c.get("scope") != "rule"]
    fac_must, fac_soft, people, persons = [], [], [], []
    for c in monthly:
        who = c.get("who") or []
        if not who or (len(who) > 1 and len(who) * 2 > len(roster)):
            (fac_must if c.get("flex") in ("fixed", "operational") else fac_soft).append(c)
            continue
        people.append(c)
        if len(who) == 1 and who[0] not in persons:
            persons.append(who[0])
    return {"only": only, "fac_must": fac_must, "fac_soft": fac_soft, "people": people, "persons": persons,
            "dated": rest, "total": len(cards)}


def shown_total(g: dict) -> int:
    """ルールのページに並ぶ数（表の上の［ルール（N件）］と同じ）。日付の頼みは表のマスに出るので数えない。"""
    return g["total"] - len(g["dated"])


def _quoted(cs: list[dict]) -> str:
    return "".join(f"「{c['name']}」" for c in cs)


def _card(r) -> dict:
    return {"id": r.id, "name": r.name, "flex": r.flex, "scope": r.scope, "kind": r.kind, "who": r.who, "meta": r.meta}


def list_answer(store: Store, year: int, month: int, rules) -> tuple[str, dict]:
    """「ルールを見せて」。種類ごとの件数を1行ずつ。少ない種類はルールの名前まで言う。"""
    g = group_cards([_card(r) for r in rules], [s.name for s in store.staff()], year, month)
    if not g["total"]:
        return "ルールはまだありません。表もルールも変えていません", _extra("list", [], [], [OPEN_RULES])
    lines, hidden = [], False
    fac = g["fac_must"] + g["fac_soft"]
    if fac:
        parts = [f"守る{len(g['fac_must'])}件"] if g["fac_must"] else []
        parts += [f"できれば{len(g['fac_soft'])}件"] if g["fac_soft"] else []
        tail = _quoted(fac) if len(fac) <= NAMES_AT else "（" + "・".join(parts) + "）"
        lines.append(f"施設のルール（毎月）{len(fac)}件{tail}")
        hidden |= len(fac) > NAMES_AT
    if g["people"]:
        n = len(g["persons"])
        tail = _quoted(g["people"]) if len(g["people"]) <= NAMES_AT else (f"（{n}人）" if n else "")
        lines.append(f"人ごとのルール（毎月）{len(g['people'])}件{tail}")
        hidden |= len(g["people"]) > NAMES_AT
    if g["only"]:
        tail = _quoted(g["only"]) if len(g["only"]) <= NAMES_AT else ""
        lines.append(f"今月だけのルール{len(g['only'])}件{tail}")
        hidden |= len(g["only"]) > NAMES_AT
    if g["dated"]:
        lines.append(f"ほかに、日付の頼み（希望休など）が{len(g['dated'])}件あり、表のマスに出ています")
    head = f"ルールは全部で{shown_total(g)}件です"
    foot = ["全部はルールのページで見られます"] if hidden else []
    return _message(head, lines, foot), _extra("list", lines, [r.id for r in rules], [OPEN_RULES])


# ---- 外れ

_SPAN = re.compile(r"(-?\d+)-(\d+|翌月1)")


def _days_of(right: str) -> str:
    m = _SPAN.fullmatch(right)
    if m:
        a, b = int(m[1]), m[2]
        end = "翌月1日" if b == "翌月1" else f"{b}日"
        return f"前の月の末〜{end}" if a < 1 else f"{a}〜{end}"
    if right.isdigit():
        return "1日（前の月の末から）" if int(right) == 0 else f"{int(right)}日"
    return right


def _place(b: dict, rule, names: set[str], fac) -> str:
    """外れた場面の key（"東/12"・"小島"・"小島/3-8"）を、どこで外れたかの短い言葉にする。"""
    key = b["key"]
    if "/" not in key:
        if key in names and rule is not None and rule.kind == "count":
            off = {st.symbol for st in fac.shift_types if st.kind in ("rest", "leave")}
            syms = _syms(rule.params.get("shifts"), fac)
            return f"{key}さん{b['value']}{'日' if syms and set(syms) <= off else '回'}"
        return f"{key}さん" if key in names else key
    left, right = key.rsplit("/", 1)
    when = _days_of(right)
    if left in names:
        return f"{left}さんの{when}"
    if left.endswith("の外"):   # ユニットに分ける（unit_apart）。その日に、その勤務の人が1つのユニットに固まった
        return when
    where = "施設全体" if left == "全体" else left.split("（")[0]
    many = f"（{b['value']}人）" if rule is not None and rule.kind == "coverage" else ""
    return f"{where}の{when}{many}"


def broken_answer(store: Store, year: int, month: int, rules, total: int | None = None) -> tuple[str, dict]:
    """「外れているルールは？」。右の一覧の赤い「外れ」と同じ数え方（state の broken と同じ check_table。
    できれば守るルール＝相談できる強さは、一覧と同じく外れの件数に入れず、別の1行で言う）。"""
    m = store.month(year, month)
    total = len(rules) if total is None else total   # 見出しの（全部でN件）は、ルールのページに並ぶ数
    if not m.table:
        return "表がまだありません。表もルールも変えていません", _extra("broken", [], [], [OPEN_RULES])
    fac, staff = store.facility(), store.staff_on(year, month)
    by_id = {r.id: r for r in rules}
    names = {s.name for s in staff}
    hits: dict[str, list[dict]] = {}
    for b in check_table(rules, fac, staff, m, m.table):
        hits.setdefault(b["rule"], []).append(b)
    must = [i for i, bs in hits.items() if bs[0]["flex"] != "negotiable"]
    soft = [i for i, bs in hits.items() if bs[0]["flex"] == "negotiable"]
    lines = []
    for i in must:
        bs = hits[i]
        places = list(dict.fromkeys(_place(b, by_id.get(i), names, fac) for b in bs))
        shown = "・".join(places[:PLACES]) + (f"ほか{len(places) - PLACES}か所" if len(places) > PLACES else "")
        name = bs[0]["name"]
        lines.append(f"「{name}」は、{shown}で外れています" if len(places) == 1
                     else f"「{name}」は{len(places)}か所で外れています（{shown}）")
    if soft:
        lines.append(f"ほかに、できれば守るルールが{len(soft)}件外れています"
                     f"（{''.join('「' + hits[i][0]['name'] + '」' for i in soft[:PLACES])}{'ほか' if len(soft) > PLACES else ''}）")
    if must:
        head = f"今の表で守れていないルールは{len(must)}件です（全部で{total}件）"
    elif soft:
        head = f"守るルールは、今の表で全部守れています（全部で{total}件）"
    else:
        head = f"今の表で守れていないルールはありません（全部で{total}件）"
    return _message(head, lines, []), _extra("broken", lines, must + soft, [OPEN_RULES])


# ---- 整理

def tidy_answer(store: Store, year: int, month: int, ai_on_screen: bool = False) -> tuple[str, dict]:
    """「ルールを整理して」。整理の画面と同じ見立てを1件1行。何も片付けない（片付けは整理の画面で押した物だけ）。"""
    from .cleanup import view as clean_view
    from .tidy import view as tidy_view
    td = tidy_view(store, year, month, ai=None)
    lines, ids, seen = [], [], set()

    def add(line: str, rule_ids: list[str]) -> None:
        lines.append(line)
        ids.extend(i for i in rule_ids if i not in ids)
        seen.update(rule_ids)

    for p in td["piles"]:
        for x in p["items"]:
            if p["pile"] == "merge":
                add(f"{_q(x['drop_names'], '')}は、{x['why']}", x["drop"] + x["keep"])
            elif p["pile"] == "conflict":
                add(f"{_q(x['names'], 'と')}は、{x['why']}", x["ids"])
            else:
                add(f"{_q(x['names'], '')}は、{x['why']}", x["ids"])
    g = clean_view(store, year, month)["groups"]
    for x in g["absent"]:
        if not set(x["ids"]) <= seen:
            add(f"{_q(x['names'], '')}は、今の名簿にいない{x['who']}さんのルールです", x["ids"])
    for x in g["overlap"]:
        if x["loose"] not in seen:
            add(f"「{x['loose_name']}」は、「{x['strict_name']}」と同じことを決めています", [x["loose"], x["strict"]])
    for x in g["quiet"]:
        if x["id"] not in seen:
            add(f"「{x['name']}」は、{x['months']}か月のあいだ表を作るのに効いていません", [x["id"]])
    for x in g["aged"]:
        if x["id"] not in seen:
            add(f"「{x['name']}」は、一時的な事情で足したルールです。今も要るかを確かめてください", [x["id"]])
    ai = ["AI の見立ては整理の画面で見られます"] if ai_on_screen else []
    if not lines:
        head = f"毎月のルール{td['total']}件の中に、いま片付けやまとめの候補は見つかりませんでした"
        return _message(head, [], ai), _extra("tidy", [], [], [OPEN_TIDY])
    shown = lines[:LIST_SHOWN]
    more = [f"ほか{len(lines) - LIST_SHOWN}件は整理の画面で見られます"] if len(lines) > LIST_SHOWN else []
    head = f"整理の候補は{len(lines)}件です。まだ何も片付けていません"
    foot = more + ["整理の画面で、選んだ物だけを片付けられます。片付けた物はあとから戻せます"] + ai
    return _message(head, shown, foot), _extra("tidy", shown, ids, [OPEN_TIDY])


def _q(names: list[str], sep: str) -> str:
    return sep.join(f"「{n}」" for n in names)


def _message(head: str, lines: list[str], foot: list[str]) -> str:
    return "\n".join([head] + [f"・{x}" for x in lines] + foot)


def _extra(topic: str, lines: list[str], ids: list[str], actions: list[dict]) -> dict:
    return {"topic": topic, "lines": lines, "ids": ids, "actions": [dict(a) for a in actions]}


def help_answer(store: Store, topic: str) -> tuple[str, dict]:
    """使い方・あいさつ・お礼に、言える一言の例を返す。例の名前は、その施設の名簿の最初の人にする。"""
    names = [s.name for s in store.staff()]
    who = names[0] if names else "岡田"
    lines = ["ルールを足す　「遅番の次の日に早番はきついから、やめてあげて」",
             f"人と日を決めて直す　「{who}さん、15と16は法事で休み」",
             "回数をならす　「早番も遅番も、可能な範囲で平均化して」",
             "ルールを聞く　「外れているルールは？」「ルールを見せて」"]
    foot = ["右下の欄に打って［伝える］を押してください（声で起動した時は、スペースキーを押している間に話せます）。直した後も［ひとつ戻す］で戻せます"]
    if topic == "thanks":
        return ("どういたしまして。ほかにも直したい所があれば、一言で伝えてください", _extra("thanks", [], [], []))
    head = ("一言で、表のルールを足したり、表を直したりできます。たとえば次のように伝えてください"
            if topic == "help" else "こんにちは。一言で、表のルールを足したり、表を直したりできます。たとえば次のように伝えてください")
    return _message(head, lines, foot), _extra(topic, lines, [], [OPEN_RULES])


def where_answer(topic: str, month: int) -> tuple[str, dict]:
    """どこを押せばよいかだけを言う。表もルールも変えない。前は Claude へ送り、「取り消したいのはどの頼みですか」と聞き返したり、
    「表を変える頼みが見つかりませんでした」と返したりして、押せば済む所で止まった（2026-11-08、本物の Claude で確かめた）。"""
    nxt = month % 12 + 1
    if topic == "undo":
        msg = "直前の直しを取り消す時は、チャットの下の［ひとつ戻す］を押してください。押すたびに、表とルールが1つ前に戻ります"
    elif topic == "next":
        msg = (f"次の月の表は、画面の右上の［{nxt}月へ］を押して作ります。持ち越すルールの見直しと希望休を確かめてから、"
               f"［{nxt}月の表を作る］を押してください")
    else:
        msg = "表は画面の左に出ています。直したい所を一言で伝えてください（「使い方を教えて」で、言える一言の例が出ます）"
    return msg, _extra(topic, [], [], [])


def answer(store: Store, year: int, month: int, topic: str, ai_on_screen: bool = False) -> tuple[str, dict]:
    """問いの答え（返事の文と、Reply.extra に入れる添え物）。表とルールは変えない。"""
    if topic in ("help", "hello", "thanks"):
        return help_answer(store, topic)
    if topic in ("undo", "next", "table"):
        return where_answer(topic, month)
    last = calendar.monthrange(year, month)[1]
    span = (f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}")   # state の cards と同じ並び
    if topic == "tidy":
        return tidy_answer(store, year, month, ai_on_screen)
    listed = list(store.ledger().rules_at(between=span).values())
    if topic == "broken":
        g = group_cards([_card(r) for r in listed], [s.name for s in store.staff()], year, month)
        return broken_answer(store, year, month, month_rules(store, year, month, with_premise=False), shown_total(g))
    return list_answer(store, year, month, listed)
