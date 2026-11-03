"""Excel の勤務表を貼るだけで、自分の施設のデータを作る（取り込みの芯）。

流れは3つ。
- parse_paste  Excel からコピーした文字（タブ区切り。カンマ区切りも可）を読む
- infer        貼った月の表から、施設の形（勤務の種類・職員）とルールの候補を作る
- create_facility  候補から施設の置き場（facility.json など）を書き、翌月の表を作る

画面とサーバーはここには無い。決め打ちを避けるため、施設の名前はコードに書かない。
勤務の種類の雛形だけ、初めのデータの facility.json から読む。
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import hashlib
import io
import re
import time
import unicodedata
from collections import Counter
from itertools import combinations
from dataclasses import dataclass, field
from pathlib import Path

from .build import C3_MESSAGE, TIMEOUT_MESSAGE, build_original
from .ledger import Ledger, today
from .model import Month, ShiftType
from .nextmonth import TAIL, next_ym
from .store import SEED, Store, atomic_write_json, read_json

# 別名 → 記号
ALIASES = {
    "休": "公", "公休": "公", "明け": "明", "夜勤": "夜", "早番": "早", "日勤": "日", "遅番": "遅",
    "有給": "有", "有休": "有", "年休": "有", "研修": "研",
    "希": "公", "希望": "公", "希休": "公", "希望休": "公", "入": "夜",
}
# 名前の列の見出し／職種の言葉（名前の列を選ぶ時に外す）
NAME_HEADS = {"氏名", "名前", "職員", "職員名"}
ROLE_WORDS = {"介護", "看護", "看護師", "介護福祉士", "パート", "アルバイト", "常勤", "非常勤", "正職員", "準職員",
              "契約職員", "リーダー"}
# 名前の欄に書かれていても人ではない行（1人2行の表の下の行）
NOTE_WORDS = {"備考", "時間", "メモ", "勤務時間"}
# ユニットの値が入る左の列の見出し
UNIT_HEADS = {"ユニット", "棟", "フロア", "ユニット名", "所属", "部署", "階", "グループ", "チーム"}
# 集計の行とみなす名前の言葉
SUMMARY_WORDS = ("計", "合計", "人数")
# 雛形に無い勤務の名前で、行の名前に使われたら集計の行とみなす言葉
SHIFT_WORDS = {"休み", "休暇", "出勤"}
# 答えの kind
ANSWER_KINDS = ("work", "rest", "leave", "ake")
# 日本の祝日（2026-11〜2027-05）。表に無い月は空にして知らせる
HOLIDAYS = {
    "2026-11": [3, 23],
    "2026-12": [],
    "2027-01": [1, 11],
    "2027-02": [11, 23],
    "2027-03": [21, 22],
    "2027-04": [29],
    "2027-05": [3, 4, 5],
}
# 初めのデータから写す設定（solve の中は下の4つだけ）
SETTING_KEYS = ("weights", "light_weight", "quality", "keep_weight", "resolve_quality")
SOLVE_KEYS = ("workers", "seed", "max_deterministic_time", "resolve_max_deterministic_time")
ALWAYS_TYPES = ("公", "有", "研")   # 表に出ていなくても、頼まれた時に置けるよう勤務の種類に必ず入れる物
LONG_REST_RATIO = 1.5   # 公休がいちばん多い日数のこの倍以上の人は、途中入職・長い休みかもしれない
RATE_LINE = 0.9   # 夜明公の決まりを候補にする割合
SERIAL_RANGE = (40000, 60000)   # Excel の日付の通し番号（1900年式）の範囲

_WEEKDAY = re.compile(r"^[(（]?[月火水木金土日](?:曜日?)?[)）]?$")
_DAY = re.compile(r"^(?:\d{4}[/-])?(?:(\d{1,2})[/-])?(\d{1,2})日?(?:[(（][月火水木金土日][)）])?$")
_DAY_MD = re.compile(r"^(?:(\d{1,2})月)?(\d{1,2})日(?:[(（][月火水木金土日][)）])?$")


class ParseError(ValueError):
    """貼り付けの読み取りエラー。どの行の何がおかしいかを普通の日本語の一文で言う。"""


class OnboardError(ValueError):
    """施設を作れない時（知らない記号の答えが足りない など）。"""


@dataclass
class Pasted:
    table: dict[str, list[str]]       # 名前 → 日ごとの記号
    days: int
    skipped: int = 0                  # 集計の行として飛ばした数
    blank_as_rest: int = 0            # 空のセルを公にした数
    unknown_symbols: list[str] = field(default_factory=list)
    had_header: bool = False
    aliases: dict[str, str] = field(default_factory=dict)   # 別名で置き換えた記号 → 置き換え先
    unit_of: dict[str, str] = field(default_factory=dict)   # 名前 → ユニット
    unit_list: list[str] = field(default_factory=list)      # ユニットの順
    blank_by: dict[str, int] = field(default_factory=dict)  # 名前 → 空欄を公にした数


@dataclass
class Proposal:
    year: int
    month: int
    table: dict[str, list[str]]
    shift_types: list[dict]
    units: list[str]
    staff: list[dict]
    rules: list[dict]
    unknown_symbols: list[str] = field(default_factory=list)
    same_as: dict[str, str] = field(default_factory=dict)   # 答えで「既存の勤務と同じ」にした記号 → 先
    problems: list[str] = field(default_factory=list)       # このままでは作れない理由（一文ずつ）
    suggestions: dict[str, str] = field(default_factory=dict)   # 知らない記号 → おすすめの選び方（same:夜 / night / same:明）
    raw: dict[str, list[str]] | None = None                 # 貼ったままの表（答えで置き換える前）
    staff_evidence: str = ""                                # 人ごとの入れる勤務を貼った月に入った物だけにした根拠
    long_rest: dict[str, int] = field(default_factory=dict)  # 公休が最頻値の1.5倍以上の人 → 公休の日数
    rest_mode: int = 0                                      # 公休のいちばん多い日数
    rest_notes: list[str] = field(default_factory=list)     # 途中入職・長い休みの人の知らせ（一文ずつ）


@dataclass
class Created:
    root: Path
    facility_id: str
    staff_count: int
    rule_count: int
    next_table_made: bool
    stage_note: str | None            # 段の知らせ（段を下げて作った時、作れなかった時の一文）
    seconds: float
    notes: list[str] = field(default_factory=list)
    diffs: list[dict] = field(default_factory=list)   # 貼った月と翌月の表の違い（text＝一文、kind＝種類）


# ---------------------------------------------------------------- 読む

def _nfkc(s: str) -> str:
    """全角・半角をそろえる。丸数字（①〜⑳）は施設の記号に使われるので、数字に変えずに残す（本人の表と見比べられるように）。"""
    return "".join(c if "\u2460" <= c <= "\u2473" else unicodedata.normalize("NFKC", c) for c in s)


def _norm(s: str) -> str:
    return _nfkc(s).replace("　", " ").strip()


def _day_number(label: str) -> int | None:
    """日の見出し（1・1日・11/1・11月1日・2026-11-01・Excel の通し番号 など）なら日にちの数、違えば None。"""
    if re.fullmatch(r"\d{5}", label) and SERIAL_RANGE[0] <= int(label) <= SERIAL_RANGE[1]:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(label))).day
    m = _DAY_MD.match(label) or _DAY.match(label)
    if not m:
        return None
    d = int(m.group(2))
    return d if 1 <= d <= 31 else None


def _find_header(cells: list[str]) -> tuple[list[int], list[int]] | None:
    """「1から始まって1ずつ増える日の並びが28個以上続く」行なら (日の列, 並びの中の空の見出しの列)。"""
    for st in range(len(cells)):
        if _day_number(cells[st]) != 1:
            continue
        run, blanks, pending, expect = [st], [], [], 2
        for j in range(st + 1, len(cells)):
            c = cells[j]
            if c == "":
                pending.append(j)
            elif _day_number(c) == expect:
                run.append(j)
                blanks += pending
                pending = []
                expect += 1
            else:
                break
        if len(run) >= 28:
            return run, blanks
    return None


def _is_wrapped_header(cells: list[str]) -> bool:
    """16, 17 … 末日, 1 … 15 のように途中で1に戻る見出しか。"""
    nums = [n for n in (_day_number(c) for c in cells if c) if n is not None]
    for i in range(len(nums) - 1):
        if nums[i] >= 28 and nums[i + 1] == 1:
            head, tail = nums[:i + 1], nums[i + 1:]
            return (len(head) >= 10 and head == list(range(head[0], head[0] + len(head)))
                    and tail == list(range(1, len(tail) + 1)) and len(head) + len(tail) >= 28)
    return False


def _is_weekday_row(cells: list[str]) -> bool:
    ne = [c for c in cells if c]
    if len(ne) < 5:
        return False
    for rest in (ne[1:], ne):
        if all(_WEEKDAY.match(c) for c in rest) and any(not c.strip("()（）").startswith("日") for c in rest):
            return True
    return False


def _clean_name(raw: str) -> str:
    name = _norm(raw).replace('"', "").strip()
    if name.endswith("さん"):
        name = name[:-2].strip()
    return re.sub(r"\s+", "", name)


def _is_role_heading(name: str) -> bool:
    """名前の列だけの区切り行が職種の見出し（看護・パート・介護職員 など）か。ユニットではない。"""
    return name in ROLE_WORDS or re.sub(r"(?:職員|スタッフ)$", "", name) in ROLE_WORDS


def _cell_text(c: str) -> str:
    """セルの中の改行は、最初の行だけ読む（「赤坂（改行）リーダー」「早（改行）7:00」）。"""
    lines = [x for x in c.replace("\r", "\n").split("\n") if x.strip()]
    return (lines[0] if lines else "").strip().strip('"').strip()


def _unit_name(label: str) -> str:
    u = re.sub(r"ユニット$", "", label).strip()
    return u or label


def parse_paste(text: str) -> Pasted:
    text = _nfkc(text)
    delim = "\t" if "\t" in text else ","

    rows: list[tuple[int, list[str]]] = []   # (行番号, セル)。引用符の中の改行は行の区切りにしない
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    prev_end = 0
    for cells in reader:
        first, prev_end = prev_end + 1, reader.line_num
        cells = [_cell_text(c) for c in cells]
        if not any(cells):
            continue
        rows.append((first, cells))

    # 見出しの行を探す。見出しより前の行（表題など）は読まない
    header = None
    start = 0
    for idx, (_, cells) in enumerate(rows):
        found = _find_header(cells)
        if found:
            header, start = (found, cells), idx + 1
            break
        if _is_wrapped_header(cells):
            raise ParseError("締め日が月末でない表（16日から翌月15日など）は、まだ読めません。"
                             "1日から月末までの表を貼ってください")
    body = rows[start:]

    shift_names = set(ALIASES) | set(SHIFT_WORDS) | _template_words()

    def is_label(name: str) -> bool:
        return (not name or any(w in name for w in SUMMARY_WORDS) or name in shift_names or name in NOTE_WORDS
                or name.isdigit())

    def is_total_row(cells: list[str], name_idx: int) -> bool:
        """名前以外のセルが空でなく、全部が数字の行は集計の行（出勤者数・公休数 など）。"""
        vals = [c for j, c in enumerate(cells) if j != name_idx and c]
        return bool(vals) and all(re.fullmatch(r"\d+(?:\.\d+)?", v) for v in vals)

    # items は ("unit", 名前) か ("staff", 行番号, 名前, 日のセル, 左のユニット列の値)
    items: list[tuple] = []
    skipped = 0
    n_header: int | None = None
    if header and header[0][0][0] > 0:
        (cols, blanks), hcells = header
        n_header = len(cols)
        left = range(cols[0])
        uc = next((j for j in left if re.sub(r"\s", "", hcells[j]) in UNIT_HEADS), None)   # ユニットの列
        nc = next((j for j in left if re.sub(r"\s", "", hcells[j]) in NAME_HEADS), None)
        if nc is None:
            def score(j: int) -> int:
                vals = [_clean_name(c[j]) for _, c in body if j < len(c) and not _is_weekday_row(c)]
                return sum(1 for v in vals if v and not v.isdigit() and not _is_role_heading(v))
            nc = max((j for j in left if j != uc), key=lambda j: (score(j), -j))
        for lineno, cells in body:
            if _is_weekday_row(cells):
                continue
            name = _clean_name(cells[nc]) if nc < len(cells) else ""
            if is_label(name) or is_total_row(cells, nc):
                skipped += 1
                continue
            days = [cells[c] if c < len(cells) else "" for c in cols]
            if not any(days):
                if not _is_role_heading(name):
                    items.append(("unit", name))
                continue
            if len(cells) <= cols[-1]:
                k = sum(1 for c in cols if c < len(cells))
                raise ParseError(f"{lineno}行目の「{name}」は日の数が{k}日分しかなく、"
                                 f"ほかの行の{n_header}日に足りません")
            if any(c < len(cells) and cells[c] for c in blanks):
                raise ParseError(f"{lineno}行目の「{name}」に、日の見出しが空の列の中身があります。"
                                 "その列を外してコピーしてください")
            ulabel = _clean_name(cells[uc]) if uc is not None and uc < len(cells) else ""
            items.append(("staff", lineno, name, days, ulabel))
    else:
        if header:
            n_header = len(header[0][0])
        for lineno, cells in body:
            if _is_weekday_row(cells):
                continue
            k = next((j for j, c in enumerate(cells) if c), None)
            name = _clean_name(cells[k]) if k is not None else ""
            if is_label(name) or is_total_row(cells, k):
                skipped += 1
                continue
            rest = cells[k + 1:]
            if any(rest):
                items.append(("staff", lineno, name, rest, ""))
            elif not _is_role_heading(name):
                items.append(("unit", name))
        if n_header is None:
            # 全部空の列を取り除く（見出しが無い時の読み方）
            sc = [it[3] for it in items if it[0] == "staff"]
            width = max((len(c) for c in sc), default=0)
            empty = {i for i in range(width) if all(i >= len(c) or not c[i] for c in sc)}
            if empty:
                items = [it if it[0] == "unit" else (it[0], it[1], it[2], [c for i, c in enumerate(it[3]) if i not in empty], it[4])
                         for it in items]

    staff_rows = [it for it in items if it[0] == "staff"]
    if not staff_rows:
        raise ParseError("職員の行が1つも見つかりませんでした。名前の列と日ごとの勤務をまとめてコピーしてください")

    n = n_header
    if n is None:
        n = Counter(len(it[3]) for it in staff_rows).most_common(1)[0][0]
    if not 28 <= n <= 31:
        raise ParseError(f"日の数が{n}日分しかありません。1か月分（28〜31日）をまとめてコピーしてください"
                         if n < 28 else f"日の数が{n}日分あります。1か月分（28〜31日）だけをコピーしてください")

    seen: dict[str, int] = {}
    table: dict[str, list[str]] = {}
    unit_of: dict[str, str] = {}
    unit_list: list[str] = []
    aliases: dict[str, str] = {}
    cur_unit: str | None = None
    col_unit: str | None = None   # 左のユニット列の値（空のセルは上と同じ）
    blank = 0
    blank_by: dict[str, int] = {}
    unknown: list[str] = []
    for it in items:
        if it[0] == "unit":
            cur_unit = _unit_name(it[1])
            continue
        _, lineno, name, cells, ulabel = it
        extra = cells[n:]
        if len(cells) > n and any(extra):
            raise ParseError(f"{lineno}行目の「{name}」は日の数が{len(cells)}日分あり、"
                             f"ほかの行の{n}日より多くなっています")
        if len(cells) < n:
            raise ParseError(f"{lineno}行目の「{name}」は日の数が{len(cells)}日分しかなく、"
                             f"ほかの行の{n}日に足りません")
        if name in seen:
            raise ParseError(f"{seen[name]}行目と{lineno}行目で名前「{name}」が重なっています")
        seen[name] = lineno
        row = []
        for c in cells[:n]:
            if not c:
                blank += 1
                blank_by[name] = blank_by.get(name, 0) + 1
                row.append("公")
                continue
            sym = ALIASES.get(c, c)
            if sym != c:
                aliases.setdefault(c, sym)
            if sym not in _template_symbols() and sym not in unknown:
                unknown.append(sym)
            row.append(sym)
        table[name] = row
        if ulabel:
            col_unit = _unit_name(ulabel)
        u = col_unit or cur_unit or "全体"
        unit_of[name] = u
        if u not in unit_list:
            unit_list.append(u)
    return Pasted(table=table, days=n, skipped=skipped, blank_as_rest=blank,
                  unknown_symbols=unknown, had_header=header is not None,
                  aliases=aliases, unit_of=unit_of, unit_list=unit_list, blank_by=blank_by)


# ---------------------------------------------------------------- 雛形

def _template() -> list[dict]:
    return read_json(SEED / "facility.json")["shift_types"]


def _template_symbols() -> set[str]:
    return {s["symbol"] for s in _template()}


def _template_words() -> set[str]:
    return {s["symbol"] for s in _template()} | {s["name"] for s in _template()}


# ---------------------------------------------------------------- 見つける

def _rule(rid: str, name: str, kind: str, flex: str, params: dict, evidence: str) -> dict:
    return {"id": rid, "ver": 1, "name": name, "kind": kind, "flex": flex, "params": params,
            "who": None, "when": None, "scope": "rule",
            "meta": {"how": "貼った表から見つけた", "evidence": evidence}}


def _answer_type(sym: str, a, daily: bool) -> dict | None:
    """知らない記号の答え（名前と種類）から勤務の種類を作る。答えが足りない・おかしければ None。
    新しい勤務は、貼った月の半分以上の日に出ていればふだんの勤務、それより少なければ頼まれた時だけ置く。"""
    if not isinstance(a, dict) or a.get("kind") not in ANSWER_KINDS or not str(a.get("name", "")).strip():
        return None
    st = {"symbol": sym, "name": str(a["name"]).strip(), "kind": a["kind"]}
    if a["kind"] == "work":
        if a.get("next_is_ake"):
            st["next_is_ake"] = True
        if not daily:
            st["on_request_only"] = True
    elif a["kind"] != "ake":
        st["on_request_only"] = True
    return st


def _run_ends(table: dict[str, list[str]], sym: str) -> list[str | None]:
    """記号の連なりの最後の日の、次の日の記号（月末は None）。"""
    out = []
    for row in table.values():
        for d, c in enumerate(row):
            if c == sym and (d + 1 >= len(row) or row[d + 1] != sym):
                out.append(row[d + 1] if d + 1 < len(row) else None)
    return out


def infer(pasted: Pasted, year: int, month: int, answers: dict | None = None) -> Proposal:
    days = calendar.monthrange(year, month)[1]
    if pasted.days != days:
        raise ParseError(f"{month}月は{days}日までですが、貼った表は{pasted.days}日分あります。月を確かめてください")
    answers = dict(answers or {})
    # 知らない記号の答えのうち「既存の勤務と同じ」は、先に表の中で置き換えてから見つける
    # 「休み」の答えは公休と同じ（公休の数・連勤・明けの次の日の数え方にそのまま入る）
    same: dict[str, str] = {}
    for sym in pasted.unknown_symbols:
        a = answers.get(sym)
        if isinstance(a, dict) and a.get("kind") == "rest" and not a.get("same_as"):
            a = answers[sym] = {"same_as": "公"}
        if isinstance(a, dict) and a.get("same_as"):
            if a["same_as"] not in _template_symbols():
                raise OnboardError(f"記号「{sym}」を同じにする先「{a['same_as']}」は、知らない勤務です")
            same[sym] = a["same_as"]
    table = {k: [same.get(c, c) for c in row] for k, row in pasted.table.items()}
    n = pasted.days
    used = {s for row in table.values() for s in row}

    def days_with(sym: str) -> int:
        return sum(1 for d in range(n) if any(row[d] == sym for row in table.values()))

    # 知らない記号の答えから勤務の種類を作る（明けの種類の記号は、夜勤の翌日の見方に先に要る）
    unknown = [s for s in pasted.unknown_symbols if s not in same]
    answered: dict[str, dict] = {}
    for sym in unknown:
        st = _answer_type(sym, answers.get(sym), days_with(sym) * 2 >= n)
        if st:
            answered[sym] = st
    ake_pre = (["明"] if "明" in used else []) + [s for s, st in answered.items() if st["kind"] == "ake"]

    # 夜の翌日が明けの種類の記号の割合（月末の夜は翌日が表の外なので数えない）
    def after(sym: str, aks: list[str]) -> tuple[int, int]:
        pairs = [row[d + 1] for row in table.values() for d in range(n - 1) if row[d] == sym]
        return len(pairs), sum(1 for nx in pairs if nx in aks)

    night_n, night_ake = after("夜", ake_pre)
    ake_follows_night = night_n > 0 and night_ake / night_n >= RATE_LINE

    # 勤務の種類。表に出た記号と公だけ。答えのある知らない記号も入れる
    shift_types = []
    for s in _template():
        if s["symbol"] not in used and s["symbol"] not in ALWAYS_TYPES:
            continue
        s = dict(s)
        if s["symbol"] == "夜":
            s["next_is_ake"] = ake_follows_night
        shift_types.append(s)
    shift_types += list(answered.values())
    by_sym = {s["symbol"]: ShiftType(**s) for s in shift_types}
    names = {s["symbol"]: s["name"] for s in shift_types}

    # 夜勤の記号。夜と、答えで「夜勤（次の日は明け）」にした記号
    night_syms = [s.symbol for s in by_sym.values()
                  if s.kind == "work" and (s.symbol == "夜" or (s.next_is_ake and s.symbol in answered))]

    # 職員。夜勤以外のふつうの勤務は全員、夜勤はその月にした人だけ
    ordinary = [s.symbol for s in by_sym.values() if s.kind == "work" and not s.on_request_only]
    # 夜勤以外は、その人が貼った月に入った勤務だけ（1つも入っていない人は全部）
    staff = []
    narrowed = []
    for name, row in table.items():
        plain = [s for s in ordinary if s not in night_syms]
        mine = [s for s in plain if s in row] or plain
        if len(mine) < len(plain):
            narrowed.append(name)
        can = mine + [s for s in night_syms if s in ordinary and s in row]
        staff.append({"name": name, "unit": pasted.unit_of.get(name, "全体"), "can": can})
    staff_evidence = ""
    if narrowed:
        staff_evidence = (f"{narrowed[0]}さん{'たち' if len(narrowed) > 1 else ''}は、{month}月に入った勤務だけにしました。"
                          "変える時は一言で")

    rules: list[dict] = []

    def add(name, kind, flex, params, evidence, who=None):
        r = _rule(f"r{len(rules) + 1:03d}", name, kind, flex, params, evidence)
        r["who"] = who
        rules.append(r)

    # 人数。ユニットが2つ以上ある時は、ユニットごとの人数で見る
    units = list(pasted.unit_list) or ["全体"]
    each = len(units) >= 2
    scope = "each_unit" if each else "all"
    where = "各ユニットで" if each else ""
    def day_counts(syms: list[str]) -> list[int]:
        if each:
            return [sum(1 for nm, row in table.items() if row[d] in syms and pasted.unit_of.get(nm) == u)
                    for u in units for d in range(n)]
        return [sum(1 for row in table.values() if row[d] in syms) for d in range(n)]

    cover = {sym: day_counts([sym]) for sym in ordinary}
    for sym in ordinary:
        counts = cover[sym]
        lo, hi = min(counts), max(counts)
        if lo == hi:
            add(f"{names[sym]}の人数", "coverage", "fixed",
                {"shifts": [sym], "scope": scope, "op": "==", "n": lo},
                f"{month}月は毎日、{where}{names[sym]}が{lo}人")
        else:
            if lo > 0:
                add(f"{names[sym]}の人数（下限）", "coverage", "fixed",
                    {"shifts": [sym], "scope": scope, "op": ">=", "n": lo},
                    f"{month}月の{names[sym]}は日によって{where}{lo}〜{hi}人。いちばん少ない日の{lo}人を下限にした")
            add(f"{names[sym]}の人数（上限）", "coverage", "negotiable",
                {"shifts": [sym], "scope": scope, "op": "<=", "n": hi},
                f"{month}月の{names[sym]}は日によって{where}{lo}〜{hi}人。いちばん多い日の{hi}人を上限にした")
    # 記号の最初の1文字が同じ2つ（早1と早2など）は、合わせた人数が毎日同じなら、その合計も守る
    groups: dict[str, list[str]] = {}
    for sym in ordinary:
        groups.setdefault(sym[0], []).append(sym)
    for grp in groups.values():
        for a, b in combinations(grp, 2):
            total = [x + y for x, y in zip(cover[a], cover[b])]
            if min(total) == max(total) > 0 and not (len(set(cover[a])) == 1 and len(set(cover[b])) == 1):
                add(f"{names[a]}と{names[b]}の人数の合計", "coverage", "fixed",
                    {"shifts": [a, b], "scope": scope, "op": "==", "n": total[0]},
                    f"{month}月は毎日、{where}{names[a]}と{names[b]}を合わせて{total[0]}人")

    # 公休の数。いちばん多い数の人たちに1本、違う人は人ごとに1本（パートの人が翌月フルで働く表を防ぐ）
    rests = {nm: row.count("公") for nm, row in table.items()}
    mode, hit = sorted(Counter(rests.values()).items(), key=lambda kv: (-kv[1], kv[0]))[0]
    ny, nmo = next_ym(year, month)
    note = ""
    if calendar.monthrange(ny, nmo)[1] != days:
        note = (f"。{nmo}月は{calendar.monthrange(ny, nmo)[1]}日あるので、公休を{mode}日のままでよいか確かめてください"
                f"（変える時は、作った後に「全員、公休は月{mode + 1}日」のように一言で）")
    outliers = {nm: v for nm, v in rests.items() if v != mode}
    long_rest = {nm: v for nm, v in rests.items() if mode > 0 and v > mode and v >= mode * LONG_REST_RATIO}
    rest_notes = [f"{nm}さん 公休{v}日（ほかの人は{mode}日）。月の途中から入った人や長く休んだ人なら、"
                  f"作った後に一言で直してください（例「{nm}さん、公休は{mode}日」）" for nm, v in long_rest.items()]
    add("公休の数", "count", "operational", {"shifts": ["公"], "min": mode, "max": mode},
        f"公休は{len(rests)}人中{hit}人が{mode}日" + note,
        who=[nm for nm, v in rests.items() if v == mode] if outliers else None)
    for nm, v in outliers.items():
        add(f"公休の数（{nm}さん）", "count", "operational", {"shifts": ["公"], "min": v, "max": v},
            f"{nm}さんの公休は{v}日" + note, who=[nm])

    # 夜勤→明け・明けは夜勤の翌日だけ・明け→公。明けは「明けの種類（kind ake）の記号」ならどれでも
    ake_syms = [s.symbol for s in by_sym.values() if s.kind == "ake"]
    ake_names = "・".join(names[a] for a in ake_syms)
    for sym in night_syms:
        tot, ok = after(sym, ake_syms)
        if ake_syms and tot and ok / tot >= RATE_LINE:
            add("夜勤の翌日は明け" if sym == "夜" else f"{names[sym]}の翌日は明け", "sequence", "fixed",
                {"if": [sym], "next_in": list(ake_syms)}, f"{names[sym]}の翌日は{tot}回中{ok}回が{ake_names}")
    for ak in ake_syms:
        ake_prev = [(row[d - 1], row[d]) for row in table.values() for d in range(1, n) if row[d] == ak]
        if night_syms and ake_prev:
            ok = sum(1 for pv, _ in ake_prev if pv in night_syms)
            if ok / len(ake_prev) >= RATE_LINE:
                add(f"{names[ak]}は夜勤の翌日だけ", "sequence", "fixed", {"if": [ak], "prev_in": list(night_syms)},
                    f"{names[ak]}の前の日は{len(ake_prev)}回中{ok}回が夜勤")
        ake_next = [(row[d], row[d + 1]) for row in table.values() for d in range(n - 1) if row[d] == ak]
        if ake_next:
            ok = sum(1 for _, nx in ake_next if nx == "公")
            if ok / len(ake_next) >= RATE_LINE:
                add(f"{names[ak]}の翌日は公休", "sequence", "fixed", {"if": [ak], "next_in": ["公"]},
                    f"{names[ak]}の翌日は{len(ake_next)}回中{ok}回が公休")

    # 連勤の上限。数え方は ShiftType.counts_as_workday。答えの無い記号は勤務の日として数える（ゆるい側）
    def works(sym: str) -> bool:
        if sym in by_sym:
            return by_sym[sym].counts_as_workday
        return sym not in _template_symbols()

    best, who = 0, ""
    for name, row in table.items():
        run = 0
        for sym in row:
            run = run + 1 if works(sym) else 0
            if run > best:
                best, who = run, name
    if best > 0:
        add("連勤の上限", "max_run", "fixed", {"shifts": "work", "max": best},
            f"{month}月でいちばん長い連勤は{best}日（{who}）")

    problems = _problems(table, by_sym, rules, staff, month)
    problems += [p for p in _answer_problems(pasted, same, answers, month) if p not in problems]
    suggestions = {}
    nights = {"夜"} | set(night_syms)
    for sym in unknown:
        tot, ok = after(sym, ake_syms)
        prev = [row[d - 1] for row in table.values() for d in range(1, n) if row[d] == sym]
        if tot >= 3 and ok / tot >= RATE_LINE:
            suggestions[sym] = "same:夜" if "夜" in used else "night"
        elif "明" in used and len(prev) >= 3 and sum(1 for p in prev if p in nights) / len(prev) >= RATE_LINE:
            suggestions[sym] = "same:明"

    return Proposal(year=year, month=month, table=table, shift_types=shift_types, units=units,
                    staff=staff, rules=rules, unknown_symbols=unknown, same_as=same,
                    problems=problems, suggestions=suggestions, raw=pasted.table, staff_evidence=staff_evidence,
                    long_rest=long_rest, rest_mode=mode, rest_notes=rest_notes)


def _answer_problems(pasted: Pasted, same: dict[str, str], answers: dict, month: int) -> list[str]:
    """知らない記号の答えが、表の並びと合わない物。貼ったままの表で、答えた記号ごとに見る。"""
    raw, n = pasted.table, pasted.days
    kinds = {s["symbol"]: s["kind"] for s in _template()}

    def mapped(c: str) -> str:
        return same.get(c, c)

    def role(sym: str) -> str | None:
        if sym in same:
            return "night" if same[sym] == "夜" else kinds.get(same[sym], "work")
        a = answers.get(sym)
        if not isinstance(a, dict) or a.get("kind") not in ANSWER_KINDS:
            return None
        return "night" if a["kind"] == "work" and a.get("next_is_ake") else a["kind"]

    nights = {"夜"} | {s for s in pasted.unknown_symbols if s not in same and role(s) == "night"}
    akes = {"明"} | {s for s in pasted.unknown_symbols if s not in same and role(s) == "ake"}
    out: list[str] = []
    for sym in pasted.unknown_symbols:
        r = role(sym)
        if r is None:
            continue
        prev = [mapped(row[d - 1]) for row in raw.values() for d in range(1, n) if row[d] == sym]
        nxt = [mapped(row[d + 1]) for row in raw.values() for d in range(n - 1) if row[d] == sym]
        after_night = len(prev) >= 3 and sum(1 for p in prev if p in nights) / len(prev) >= RATE_LINE
        before_ake = len(nxt) >= 3 and sum(1 for x in nxt if x in akes) / len(nxt) >= RATE_LINE
        flagged = False
        if after_night and r != "ake":
            out.append(f"『{sym}』は、いつも夜勤の翌日に出ています。明けではありませんか。選び直してください")
            flagged = True
        elif before_ake and r not in ("night", "ake") and not (sym not in same and r in ("rest", "leave")):
            out.append(f"『{sym}』は、いつも翌日が明けです。夜勤ではありませんか。選び直してください")
            flagged = True
        # 休みの種類で答えたのに、毎日同じ人数いた記号（公休は日によって人数が変わるので当たらない）
        per_day = [sum(1 for row in raw.values() if row[d] == sym) for d in range(n)]
        users = [row for row in raw.values() if sym in row]
        sole_leave = len(users) == 1 and all(c == sym for c in users[0])   # 1人だけが1か月ずっと（長期の病休など）
        if (not flagged and not sole_leave and not after_night and r in ("rest", "leave", "ake")
                and min(per_day) == max(per_day) > 0 and not (sym not in same and r in ("rest", "leave") and before_ake)):
            out.append(f"『{sym}』は{month}月に毎日{per_day[0]}人いた勤務です。休みでよいか確かめてください")
    return out


def _problems(table: dict[str, list[str]], by_sym: dict[str, ShiftType], rules: list[dict],
              staff: list[dict], month: int) -> list[str]:
    """黙って違う表になる物を止める。貼った月に毎日いた勤務が来月の表に入らない・夜勤が2種類ある、など。"""
    n = len(next(iter(table.values())))
    placed = {s for r in rules if r["kind"] == "coverage" for s in r["params"]["shifts"]}
    placed |= {s for st in staff for s in st["can"]}
    out: list[str] = []
    order = list(dict.fromkeys(c for row in table.values() for c in row))
    ake_syms = {s.symbol for s in by_sym.values() if s.kind == "ake"} | {"明"}

    def followed_by_ake(sym: str) -> tuple[int, int]:
        ends = [nx for nx in _run_ends(table, sym) if nx is not None]
        return len(ends), sum(1 for nx in ends if nx in ake_syms)

    candidates = []
    for sym in order:
        st = by_sym.get(sym)
        if st is not None and st.kind in ("rest", "leave"):
            # 休みと答えたのに、いつも翌日が明け＝夜勤のはず
            tot, ok = followed_by_ake(sym) if sym not in _template_symbols() else (0, 0)
            if tot >= 3 and ok / tot >= RATE_LINE:
                out.append(f"『{sym}』は休みと答えましたが、いつも翌日が明けです。夜勤ではありませんか。選び直してください")
            continue
        if st is not None and st.kind == "ake":
            continue
        day_cnt = [sum(1 for row in table.values() if row[d] == sym) for d in range(n)]
        present = sum(1 for c in day_cnt if c)
        if present * 2 >= n and sym not in placed:
            typical = Counter(c for c in day_cnt if c).most_common(1)[0][0]
            when = "ほぼ毎日" if present / n >= RATE_LINE else "半分以上の日に"
            out.append(f"『{sym}』は{month}月に{when}{typical}人いましたが、来月の表には入りません。"
                       "何の勤務か選び直してください")
        tot, ok = followed_by_ake(sym)
        if tot >= 3 and ok / tot >= RATE_LINE:
            candidates.append(sym)
    if len(candidates) >= 2:
        out.append("夜勤が2種類ある表（準夜・深夜など）は、まだ読めません。1種類の夜勤にまとめてから貼ってください")
    return out


# ---------------------------------------------------------------- 作る

def _short_id(name: str) -> str:
    return "f" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:7]


def _settings(year: int, month: int, ny: int, nm: int, notes: list[str]) -> dict:
    src = read_json(SEED / "facility.json")["settings"]
    out: dict = {"solve": {k: src["solve"][k] for k in SOLVE_KEYS if k in src.get("solve", {})}}
    for k in SETTING_KEYS:
        if k in src:
            out[k] = src[k]
    hol = {k: list(v) for k, v in HOLIDAYS.items()}
    for y, m in ((year, month), (ny, nm)):
        key = f"{y:04d}-{m:02d}"
        if key not in HOLIDAYS:
            hol[key] = []
            notes.append(f"{y}年{m}月の祝日は入っていません")
    out["holidays"] = hol
    return out


def create_facility(proposal: Proposal, root: Path, *, name: str, next_year: int, next_month: int,
                    answers: dict | None = None) -> Created:
    root = Path(root)
    answers = answers or {}
    if (next_year, next_month) != next_ym(proposal.year, proposal.month):
        raise OnboardError(f"翌月は{next_ym(proposal.year, proposal.month)[0]}年"
                           f"{next_ym(proposal.year, proposal.month)[1]}月にしてください")
    if not name.strip():
        raise OnboardError("施設の名前を入れてください")
    if (root / "facility.json").exists():
        raise OnboardError("その場所にはもう施設のデータがあります")

    # 知らない記号の答え。足りなければ作らない（infer が答えから勤務の種類を作った記号は、そのまま使う）
    shift_types = [dict(s) for s in proposal.shift_types]
    have = {s["symbol"] for s in shift_types}
    missing = [s for s in proposal.unknown_symbols if s not in have and s not in answers]
    if missing:
        raise OnboardError("次の記号が何か教えてください。" + "・".join(missing))
    for sym in proposal.unknown_symbols:
        if sym in have:
            continue
        a = answers[sym]
        if not isinstance(a, dict) or a.get("kind") not in ANSWER_KINDS or not str(a.get("name", "")).strip():
            raise OnboardError(f"記号「{sym}」の答えは、名前と種類（{'・'.join(ANSWER_KINDS)}）が要ります")
        st = {"symbol": sym, "name": str(a["name"]).strip(), "kind": a["kind"]}
        if a["kind"] != "ake":
            st["on_request_only"] = True   # 頼まれた時だけ置く。作る時に勝手に使わない
        shift_types.append(st)
    if proposal.problems:
        raise OnboardError("。".join(proposal.problems))

    notes: list[str] = []
    t0 = time.perf_counter()
    settings = _settings(proposal.year, proposal.month, next_year, next_month, notes)
    fac = {"id": _short_id(name), "name": name.strip(), "units": list(proposal.units),
           "shift_types": shift_types, "settings": settings}
    atomic_write_json(root / "facility.json", fac)
    atomic_write_json(root / "staff.json", proposal.staff)

    ledger = Ledger()
    for r in proposal.rules:
        ledger.append("add", rule=r, op_id=f"onboard-{r['id']}",
                      meta={"by": "作成者", "at": today(), "how": r["meta"]["how"],
                            "reason": r["meta"]["evidence"]})
    atomic_write_json(root / "ledger.json", ledger.to_list())

    store = Store(root)
    key = f"{proposal.year:04d}-{proposal.month:02d}"
    store.save_month(Month(year=proposal.year, month=proposal.month, holidays=settings["holidays"][key],
                           prev_tail={}, table={k: list(v) for k, v in proposal.table.items()}))
    nkey = f"{next_year:04d}-{next_month:02d}"
    nxt = Month(year=next_year, month=next_month, holidays=settings["holidays"][nkey],
                prev_tail={k: list(v[-TAIL:]) for k, v in proposal.table.items()})
    store.save_month(nxt)

    r = build_original(store, next_year, next_month, with_premise=False)
    made = r.ok
    if made:
        nxt = store.month(next_year, next_month)
        nxt.table = r.result.table
        nxt.built_rev = store.ledger().rev
        store.save_month(nxt)
        placed = {c for row in nxt.table.values() for c in row}
        pasted_syms = {c for row in proposal.table.values() for c in row}
        n_days = len(next(iter(proposal.table.values()))) if proposal.table else 0
        missing_daily = []
        missing_syms: list[str] = []
        for st in shift_types:
            if (st["kind"] in ("work", "ake") and not st.get("on_request_only")
                    and st["symbol"] in pasted_syms and st["symbol"] not in placed):
                days_seen = sum(1 for d in range(n_days)
                                if any(row[d] == st["symbol"] for row in proposal.table.values()))
                if n_days and days_seen * 2 >= n_days:
                    missing_daily.append(f"{st['name']}（{st['symbol']}）")
                    missing_syms.append(st["symbol"])
        if missing_daily:
            # 出口の確かめ。貼った月に毎日のようにいた勤務が翌月に1回も無い表は、どの答え方から来たかに関わらず
            # 「できた」と言わない（入口の確かめをすり抜けた黙って違う表を、ここで必ず止める）
            made = False
            r.note = None
            culprits = _culprit_answers(proposal, missing_syms)
            r.message = ("・".join(missing_daily) + f"は{proposal.month}月にほぼ毎日いましたが、"
                         f"{next_month}月の表に1回も入りませんでした。"
                         + (f"答えた記号" + "".join(f"『{c}』" for c in culprits) + "を確かめてください" if culprits
                            else "記号の答え（何の勤務か）を確かめてください"))
    diffs: list[dict] = []
    if made:
        diffs = compare_months(proposal.table, nxt.table, shift_types, proposal.month, next_month,
                               raw=proposal.raw, year=proposal.year, next_year=next_year,
                               holidays=settings["holidays"],
                               unit_of={st["name"]: st.get("unit", "") for st in proposal.staff})
        gone = _rest_misses(proposal.rules, nxt.table)
        if gone:
            notes.append("公休の数のルールを守りきれなかった人がいます")
    elif r.message in (C3_MESSAGE, TIMEOUT_MESSAGE):
        r.message = _unsolved_message(store, next_year, next_month, r.seconds)
    if not made:
        notes.append("翌月の表はまだ作れていません")
    note = r.note if made else r.message
    return Created(root=root, facility_id=fac["id"], staff_count=len(proposal.staff),
                   rule_count=len(proposal.rules), next_table_made=made, stage_note=note,
                   seconds=round(r.seconds, 2), notes=notes, diffs=diffs)


# ---------------------------------------------------------------- 貼った月との違い

def _rest_misses(rules: list[dict], table: dict[str, list[str]]) -> list[str]:
    """公休の数のルールを守れていない人。"""
    out = []
    for r in rules:
        if r["kind"] != "count" or r["params"].get("shifts") != ["公"]:
            continue
        for nm in (r.get("who") or list(table)):
            if nm in table and not (r["params"]["min"] <= table[nm].count("公") <= r["params"]["max"]):
                out.append(nm)
    return out


def _culprit_answers(proposal: Proposal, missing: list[str]) -> list[str]:
    """入らなかった勤務に関わる、答えた記号。その勤務の記号そのものと、その前の日にいつも出ていた記号。"""
    raw = proposal.raw or {}
    answered = list(dict.fromkeys(list(proposal.unknown_symbols) + list(proposal.same_as)))

    def mapped(c: str) -> str:
        return proposal.same_as.get(c, c)

    out = [m for m in missing if m in answered]
    for u in answered:
        nxt = [mapped(row[d + 1]) for row in raw.values() for d in range(len(row) - 1) if row[d] == u]
        if len(nxt) >= 3 and any(sum(1 for x in nxt if x == m) / len(nxt) >= RATE_LINE for m in missing) and u not in out:
            out.append(u)
    return out


_GROUPS = (("連勤の上限", lambda r: r.kind == "max_run"),
           ("人数のルール", lambda r: r.kind == "coverage"),
           ("夜勤と明けのルール", lambda r: r.kind == "sequence"),
           ("公休の数", lambda r: r.kind == "count"))


def _unsolved_message(store: Store, year: int, month: int, spent: float) -> str:
    """作れなかった時の一文。ルールを1種類ずつ外して解き直し、外せば作れる物を名指しする。"""
    from .build import month_rules, solve_staged
    found: list[str] = []
    if spent < 20:   # 時間切れで来た時は、何度も解き直さない
        fac, staff, m = store.facility(), store.staff_on(year, month), store.month(year, month)
        rules = month_rules(store, year, month, with_premise=False)
        for label, is_it in _GROUPS:
            rest = [r for r in rules if not is_it(r)]
            if len(rest) < len(rules) and solve_staged(fac, staff, rest, m).ok:
                found.append(label)
    if found:
        return ("この形では見つかりませんでした。次のルールを外すと作れます。" + "・".join(found)
                + "。「見つけたルール」のチェックを外して、もう一度作ってください")
    return ("この形では見つかりませんでした。ルールを1種類外すだけでは作れません。"
            "貼った表の月と、勤務の記号の答えを確かめてください")


def _label(who: list[str]) -> str:
    return f"{who[0]}さん" if len(who) == 1 else f"{who[0]}さんほか{len(who) - 1}人"


def compare_months(cur: dict[str, list[str]], nxt: dict[str, list[str]], shift_types: list[dict],
                   month: int, next_month: int, *, raw: dict[str, list[str]] | None = None,
                   year: int | None = None, next_year: int | None = None,
                   holidays: dict[str, list[int]] | None = None,
                   unit_of: dict[str, str] | None = None) -> list[dict]:
    """貼った月と翌月の表を並べて、違う所を一覧にする。違いは作るのを止めない。
    raw は答えで置き換える前の貼った表（曜日の形だけ、これと比べる）。year・next_year があれば曜日の形も見る。"""
    names = {s["symbol"]: s["name"] for s in shift_types}
    kind = {s["symbol"]: s["kind"] for s in shift_types}
    daily = [s["symbol"] for s in shift_types if s["kind"] == "work" and not s.get("on_request_only")]
    ake = {s["symbol"] for s in shift_types if s["kind"] == "ake"}
    night = [s["symbol"] for s in shift_types if s["kind"] == "work" and s.get("next_is_ake")]
    out: list[dict] = []

    def per_day(t, sym, members=None):
        days = len(next(iter(t.values())))
        return [sum(1 for nm, row in t.items() if row[d] == sym and (members is None or nm in members))
                for d in range(days)]

    def span(lo, hi, every):
        return f"毎日{lo}人" if lo == hi and every else (f"{lo}人" if lo == hi else f"{lo}〜{hi}人")

    # a. ふだんの勤務の、1日の人数の幅
    for sym in daily:
        c, x = per_day(cur, sym), per_day(nxt, sym)
        lo, hi, nlo, nhi = min(c), max(c), min(x), max(x)
        if nlo < lo or nhi > hi:
            out.append({"kind": "count_range",
                        "text": f"{names[sym]}　{month}月は{span(lo, hi, True)} → {next_month}月は{span(nlo, nhi, False)}"})

    # a2. ユニットごとの、1日の人数の幅（ユニットが2つ以上の時）
    units = list(dict.fromkeys((unit_of or {}).get(nm, "") for nm in cur))
    if unit_of and len(units) >= 2:
        for sym in daily:
            for u in units:
                mem = {nm for nm in cur if unit_of.get(nm, "") == u}
                c, x = per_day(cur, sym, mem), per_day(nxt, sym, mem)
                lo, hi, nlo, nhi = min(c), max(c), min(x), max(x)
                if nlo < lo or nhi > hi:
                    out.append({"kind": "unit_count_range",
                                "text": f"{names[sym]}（{u}）　{month}月は{span(lo, hi, True)} → {next_month}月は{span(nlo, nhi, False)}"})

    # a3. 曜日の形。ふだんの勤務ごとの、平日と土日祝の平均人数（答えで置き換える前の表と比べる）
    if year is not None and next_year is not None:
        base = raw if raw is not None else cur

        def off_days(y, m, n):
            hol = set((holidays or {}).get(f"{y:04d}-{m:02d}", []))
            return [calendar.weekday(y, m, d + 1) >= 5 or (d + 1) in hol for d in range(n)]

        def shape(t, sym, y, m):
            c = per_day(t, sym)
            off = off_days(y, m, len(c))
            wd = [v for v, o in zip(c, off) if not o]
            we = [v for v, o in zip(c, off) if o]
            return (sum(wd) / len(wd) if wd else 0.0, sum(we) / len(we) if we else 0.0)

        for sym in daily:
            if not any(sym in row for row in base.values()):
                continue   # 貼った月に無い記号（答えで同じにした先など）は、形を比べられない
            cw, ce = shape(base, sym, year, month)
            nw, ne = shape(nxt, sym, next_year, next_month)
            gap_c, gap_n = cw - ce, nw - ne
            swapped = gap_c * gap_n < 0 and max(abs(gap_c), abs(gap_n)) >= 0.2
            if abs(gap_n - gap_c) >= 0.5 or swapped:
                out.append({"kind": "weekday_shape",
                            "text": f"{names[sym]}　{month}月は平日{cw:.1f}人・土日{ce:.1f}人 → "
                                    f"{next_month}月は平日{nw:.1f}人・土日{ne:.1f}人"})

    # b. 夜勤の翌日が明けの割合
    def rate(t, sym):
        pairs = [row[d + 1] for row in t.values() for d in range(len(row) - 1) if row[d] == sym]
        return (sum(1 for p in pairs if p in ake) / len(pairs)) if pairs else None

    for sym in night:
        a, b = rate(cur, sym), rate(nxt, sym)
        if a is not None and b is not None and b < a - 1e-9:
            out.append({"kind": "ake_rate",
                        "text": f"{names[sym]}の次の日が明け　{month}月は{round(a * 100)}% → {next_month}月は{round(b * 100)}%"})

    # b2. 人ごとの夜勤の回数（2回以上違う人。同じ変わり方の人はまとめる）
    if night:
        moved_n: dict[tuple[int, int], list[str]] = {}
        for nm, row in cur.items():
            if nm in nxt:
                a, b = sum(1 for c in row if c in night), sum(1 for c in nxt[nm] if c in night)
                if abs(a - b) >= 2:
                    moved_n.setdefault((a, b), []).append(nm)
        for (a, b), who in moved_n.items():
            out.append({"kind": "night_count", "text": f"{_label(who)}の夜勤　{month}月は{a}回 → {next_month}月は{b}回"})

    # c. 人ごとの、入った勤務の種類
    def works(row):
        return [s["symbol"] for s in shift_types if kind[s["symbol"]] == "work" and s["symbol"] in row]

    for nm, row in cur.items():
        if nm not in nxt:
            continue
        before, after_ = works(row), works(nxt[nm])
        new = [s for s in after_ if s not in before]
        if new:
            was = ("・".join(names[s] for s in before) + "だけ") if before else "勤務なし"
            out.append({"kind": "person_shifts",
                        "text": f"{nm}さん　{month}月は{was} → {next_month}月は{'・'.join(names[s] for s in new)}にも"})

    # c2. 土日休み固定の人（貼った月に土日がすべて休みだった人が、翌月は土日に勤務する）
    if year is not None and next_year is not None:
        def weekend_idx(y, m, n):
            return [d for d in range(n) if calendar.weekday(y, m, d + 1) >= 5]

        moved_w: dict[int, list[str]] = {}
        wc = weekend_idx(year, month, len(next(iter(cur.values()))))
        wn = weekend_idx(next_year, next_month, len(next(iter(nxt.values()))))
        for nm, row in cur.items():
            if nm not in nxt or not wc or not all(kind.get(row[d]) in ("rest", "leave") for d in wc):
                continue
            k = sum(1 for d in wn if kind.get(nxt[nm][d]) in ("work", "ake"))
            if k:
                moved_w.setdefault(k, []).append(nm)
        for k, who in moved_w.items():
            out.append({"kind": "weekend_fixed",
                        "text": f"{_label(who)}　{month}月は土日がすべて休み → {next_month}月は土日に{k}日勤務"})

    # c3. 消えた勤務（貼った月に出たのに、翌月に1回も無い。頼まれた時だけの勤務も）
    for s in shift_types:
        sym = s["symbol"]
        if s["kind"] == "rest":
            continue
        used = sum(row.count(sym) for row in cur.values())
        if used and not any(sym in row for row in nxt.values()):
            out.append({"kind": "gone_shift",
                        "text": f"{names[sym]}（{sym}）　{month}月は{used}回 → {next_month}月は1回も入っていません"})

    # d. 人ごとの公休の数（同じ変わり方の人はまとめる）
    moved: dict[tuple[int, int], list[str]] = {}
    for nm, row in cur.items():
        if nm in nxt and row.count("公") != nxt[nm].count("公"):
            moved.setdefault((row.count("公"), nxt[nm].count("公")), []).append(nm)
    for (a, b), who in moved.items():
        out.append({"kind": "rest_count", "text": f"{_label(who)}の公休　{month}月は{a}日 → {next_month}月は{b}日"})
    return out
