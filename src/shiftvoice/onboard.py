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
# 標準様式（勤務形態一覧表）の「行の種類」の列の言葉。1人が2〜3行ある表で、シフト記号の行と時間数の行を見分ける
ROWKIND_SYMBOL = "シフト記号"
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
SHORT_REST_WEIGHT = 100   # 2026-11-04 見本の施設Bで測って決めた
SHORT_REST_HOURS = 14
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
    spelled: dict[str, dict[str, int]] = field(default_factory=dict)   # 読み替えた後の記号 → {貼った書き方（空欄は ""）: 回数}
    merged: list[str] = field(default_factory=list)         # 2行（兼務）あって1人にまとめた名前


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
    merge_notes: list[str] = field(default_factory=list)    # 兼務の2行を1人にまとめた知らせ（警告ではない）
    paste_symbols: dict[str, str] = field(default_factory=dict)   # アプリの記号 → 貼った表での書き方（同じ物は入れない。空欄は ""）
    paste_merged: dict[str, dict] = field(default_factory=dict)   # 書き方が2つ以上あった記号 → {"to": 持ち出す書き方, "from": [ほかの書き方]}
    symbol_plan: "SymbolPlan | None" = None                  # シフト記号表から読んだ仮の答え（画面に1行ずつ出す）


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
    short_rest: dict | None = None   # 次の勤務までの間が短い並びの数（line・before・after・month・next_month・untimed＝時刻の無い勤務の名前）


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


def _head_text(raw: str) -> str:
    """見出しのセル。(10) 氏　名 のような番号と空白を外して「氏名」にする（改行も外す）。"""
    t = re.sub(r"\s+", "", _nfkc(raw))
    return re.sub(r"^[(（]\d+[)）]", "", t)


def _row_kind(v: str) -> str | None:
    """行の種類の列の値。シフト記号の行は "sym"、時間数の行（勤務時間数・日中の勤務時間数 など）は "hours"。"""
    t = re.sub(r"\s+", "", _nfkc(v))
    if t == ROWKIND_SYMBOL:
        return "sym"
    if t and ("時間数" in t or t == "勤務時間"):
        return "hours"
    return None


def _merge_rows(a: list[str], b: list[str]) -> list[str]:
    """兼務で2行に分かれた同じ人の日ごとの記号を1行にする。同じ記号か片方が空ならその記号、違えば1行目。"""
    return [x if x or not y else y for x, y in zip(a, b)]


def parse_paste(text: str) -> Pasted:
    text = _nfkc(text)
    delim = "\t" if "\t" in text else ","

    rows: list[tuple[int, list[str]]] = []   # (行番号, セル)。引用符の中の改行は行の区切りにしない
    raws: list[list[str]] = []               # 同じ行の、改行も残した元のセル（見出しを読む時に使う）
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    prev_end = 0
    for raw in reader:
        first, prev_end = prev_end + 1, reader.line_num
        cells = [_cell_text(c) for c in raw]
        if not any(cells):
            continue
        rows.append((first, cells))
        raws.append(raw)

    # 見出しの行を探す。見出しより前の行（表題など）は読まない
    header = None
    start = 0
    head_rows: list[list[str]] = []   # 日の番号の行と、その上の3行までの見出し（元のセル。近い順）
    for idx, (_, cells) in enumerate(rows):
        found = _find_header(cells)
        if found:
            header, start = (found, cells), idx + 1
            head_rows = [raws[i] for i in range(idx, max(-1, idx - 4), -1)]
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
    has_kind = False   # 標準様式（1人が2〜3行で、行の種類の列がある表）か
    n_header: int | None = None
    if header and header[0][0][0] > 0:
        (cols, blanks), hcells = header
        n_header = len(cols)
        left = range(cols[0])
        def find_head(words: set[str]) -> int | None:
            # 日の番号の行を先に、なければその上の3行まで（標準様式は「氏名」が日の番号の2行上にある）
            for hr in head_rows:
                for j in left:
                    if j < len(hr) and (_head_text(hr[j]) in words
                                        or re.sub(r"\s", "", _cell_text(hr[j])) in words):   # 前の版と同じ1行目でも
                        return j
            return None
        uc = find_head(UNIT_HEADS)   # ユニットの列
        nc = find_head(NAME_HEADS)
        # 行の種類の列（シフト記号・勤務時間数 などが並ぶ列）。名前と取り違えない
        kind_counts = {j: sum(1 for _, c in body if j < len(c) and _row_kind(c[j])) for j in left}
        tc = max(kind_counts, key=lambda j: (kind_counts[j], -j), default=None)
        has_sym = tc is not None and any(tc < len(c) and _row_kind(c[tc]) == "sym" for _, c in body)
        if tc is not None and (kind_counts[tc] < 2 or not has_sym or tc == nc):
            tc = None
        if nc is None and tc is not None:
            # 職種・勤務形態・氏名の列が同点になり、職種の列を名前と読んで兼務のまとめと重なると、人が黙って減る（2026-11-04 レビュー）
            raise ParseError("勤務形態一覧表の「氏名」の見出しが見つかりません。見出しの行から最後の人まで、"
                             "全部の列を選んでコピーしてください")
        if nc is None:
            def score(j: int) -> int:
                vals = [_clean_name(c[j]) for _, c in body if j < len(c) and not _is_weekday_row(c)]
                return sum(1 for v in vals if v and not v.isdigit() and not _is_role_heading(v))
            nc = max((j for j in left if j != uc and j != tc), key=lambda j: (score(j), -j))
        for lineno, cells in body:
            if _is_weekday_row(cells):
                continue
            name = _clean_name(cells[nc]) if nc < len(cells) else ""
            kind = _row_kind(cells[tc]) if tc is not None and tc < len(cells) else None
            if kind == "hours" or (tc is not None and not name):
                continue   # 時間数の行と、名前の無い行（人ではない。集計の行にも数えない）
            if is_label(name) or is_total_row(cells, nc):
                skipped += 1
                continue
            days = [cells[c] if c < len(cells) else "" for c in cols]
            if not any(days) and kind != "sym":
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
            if kind == "sym":
                has_kind = True
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

    # 標準様式で同じ名前が2行（兼務。それぞれにシフト記号の行がある）なら、止めずに1人にまとめる
    merged_names: list[str] = []
    if has_kind:
        at: dict[str, int] = {}
        folded: list[tuple] = []
        for it in items:
            if it[0] == "staff" and it[2] in at:
                prev = folded[at[it[2]]]
                if len(prev[3]) == len(it[3]):
                    folded[at[it[2]]] = (prev[0], prev[1], prev[2], _merge_rows(prev[3], it[3]), prev[4])
                    if it[2] not in merged_names:
                        merged_names.append(it[2])
                    continue
            if it[0] == "staff":
                at[it[2]] = len(folded)
            folded.append(it)
        items = folded

    seen: dict[str, int] = {}
    table: dict[str, list[str]] = {}
    unit_of: dict[str, str] = {}
    unit_list: list[str] = []
    aliases: dict[str, str] = {}
    cur_unit: str | None = None
    col_unit: str | None = None   # 左のユニット列の値（空のセルは上と同じ）
    blank = 0
    blank_by: dict[str, int] = {}
    spelled: dict[str, Counter] = {}
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
                spelled.setdefault("公", Counter())[""] += 1
                continue
            sym = ALIASES.get(c, c)
            spelled.setdefault(sym, Counter())[c] += 1
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
                  aliases=aliases, unit_of=unit_of, unit_list=unit_list, blank_by=blank_by,
                  spelled={k: dict(v) for k, v in spelled.items()}, merged=merged_names)


# ---------------------------------------------------------------- 雛形

def _template() -> list[dict]:
    return read_json(SEED / "facility.json")["shift_types"]


def _template_symbols() -> set[str]:
    return {s["symbol"] for s in _template()}


def _template_words() -> set[str]:
    return {s["symbol"] for s in _template()} | {s["name"] for s in _template()}


# ---------------------------------------------------------------- シフト記号表（様式の別のシート）

_CLOCK = re.compile(r"^(\d{1,2})[:：](\d{2})$|^(\d{1,2})時(?:(\d{1,2})分?)?$")
_SYMBOL_CELL = re.compile(r"^(?!\d+$)[A-Za-z0-9ぁ-んァ-ヶ一-鿿\u2460-\u2473]{1,4}$")
_REST_WORD = re.compile(r"^(休|公|休み|公休|休日|休暇|年休|有休|有給|希望休)$")
GUESS_TIMED = ("早", "日", "遅")
SHORT_SHIFT_MIN = 7 * 60   # これより短い勤務は記号表から決めない（2026-11-04 レビュー。10:00〜16:00 のパートが遅番になった）   # 記号表の時刻を勤務の時刻の初めの値にする勤務（夜勤・明けは時刻を聞かない）


def _clock_minutes(cell: str) -> int | None:
    """7:00・07:00・7時・7時30分 のような時刻なら0時からの分、違えば None（24:00 は 1440）。"""
    m = _CLOCK.match(_nfkc(cell).strip())
    if not m:
        return None
    h, mi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4) or 0))
    return h * 60 + mi if h <= 24 and mi <= 59 else None


def _clock_label(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def parse_symbol_table(text: str) -> dict[str, dict]:
    """シフト記号表（記号・始業・終業・休憩・勤務時間の表）を読む。記号 → {"start": 分, "end": 分}。
    休みの行は {"rest": True}。時刻の無い行（空の行・「-」の行）は入れない。同じ記号が2回出たら先の行。
    見出し（記号・始業時刻・終業時刻）があればその列を見る。無ければ、行の中の最初の2つの時刻を始業・終業にする。"""
    text = _nfkc(text)
    delim = "\t" if "\t" in text else ","
    rows = [[_cell_text(c) for c in raw] for raw in csv.reader(io.StringIO(text), delimiter=delim)]
    sc = ec = yc = None
    out: dict[str, dict] = {}
    for cells in rows:
        if not any(cells):
            continue
        heads = [_head_text(c) for c in cells]
        if sc is None:
            s_i = next((j for j, h in enumerate(heads) if h.startswith(("始業", "開始", "始まり"))), None)
            e_i = next((j for j, h in enumerate(heads) if h.startswith(("終業", "終了", "終わり"))), None)
            if s_i is not None and e_i is not None:
                sc, ec = s_i, e_i
                yc = next((j for j, h in enumerate(heads) if h in ("記号", "シフト記号")), None)
                continue
        times = [(j, _clock_minutes(c)) for j, c in enumerate(cells) if c and _clock_minutes(c) is not None]
        if sc is not None:
            s_v = _clock_minutes(cells[sc]) if sc < len(cells) and cells[sc] else None
            e_v = _clock_minutes(cells[ec]) if ec < len(cells) and cells[ec] else None
            first = yc if yc is not None else 0
        else:
            s_v, e_v = (times[0][1], times[1][1]) if len(times) >= 2 else (None, None)
            first = 0
        # 記号。見出しの列から右へ、最初の記号らしいセル（結合された列の写しは同じ記号）
        sym = next((c for c in cells[first:first + 3] if c and _SYMBOL_CELL.match(c)), None)
        if sym is None:
            sym = next((c for c in cells if c and _REST_WORD.match(c)), None)
        if sym is None or sym in out:
            continue
        if s_v is not None and e_v is not None and s_v != e_v:
            out[sym] = {"start": s_v, "end": e_v}
        elif (s_v is not None and s_v == e_v) or (s_v is None and e_v is None and any(_REST_WORD.match(c) for c in cells if c)):
            out[sym] = {"rest": True}
    return out


def _guess_target(info: dict) -> str | None:
    """記号表の1行から、アプリの勤務の記号を決める。決められなければ None。"""
    if info.get("rest"):
        return "公"
    s, e = info["start"], info["end"]
    if s == 0 and e > 0:
        return "明"
    if e in (0, 1440) and s >= 15 * 60:
        return "夜"
    if e < s:
        return "夜"
    if e - s < SHORT_SHIFT_MIN:
        return None   # 7時間より短い勤務（パートなど）は、始まりの時刻だけでは決めない（作成者に聞く）
    if s < 8 * 60:
        return "早"
    if s < 10 * 60:
        return "日"
    return "遅" if e >= 19 * 60 else None   # 10時以降に始まり19時より前に終わる勤務（中番など）も聞く


@dataclass
class SymbolPlan:
    answers: dict[str, dict] = field(default_factory=dict)       # 知らない記号 → {"same_as": 先}
    suggestions: dict[str, str] = field(default_factory=dict)    # 知らない記号 → same:先（画面のおすすめ）
    times: dict[str, dict] = field(default_factory=dict)         # 先の記号（早・日・遅）→ {"start": "07:00", "hours": 9}
    time_from: dict[str, str] = field(default_factory=dict)      # 先の記号 → 時刻を取った記号表の記号
    lines: list[str] = field(default_factory=list)               # 画面に出す1行ずつ（a＝早番（7:00〜16:00））
    undecided: list[str] = field(default_factory=list)           # 決められなかった記号（作成者に聞く）


def plan_symbols(pasted: Pasted, text: str, answers: dict | None = None) -> SymbolPlan:
    """貼った表の知らない記号を、記号表の時刻から仮に決める（作成者は画面で直せる）。
    - 終わりが24:00か0:00で始まりが15時以降は夜、始まりが0:00は明、日をまたぐ1記号の夜勤は夜
    - 7時間より短い勤務は決めない（聞く）。それ以外は始まりが8時より前で早、10時より前で日、
      10時以降で終わりが19時以降なら遅（19時より前に終わる勤務は聞く）
    - 休みの行は公
    answers（作成者がいま選んでいる答え）があれば、その記号は答えのとおりに扱う。早・日・遅の時刻は、
    答え（無ければ仮の決め）でその勤務になった記号のうち、貼った月でいちばん多く出た記号の物
    （2026-11-04 レビュー。選び直しても前の記号の時刻と行が残っていた）。時刻の長さは終わり − 始まり（休憩を含む）。"""
    table = parse_symbol_table(text)
    names = {s["symbol"]: s["name"] for s in _template()}
    uses: Counter = Counter(c for row in pasted.table.values() for c in row)
    given = answers if isinstance(answers, dict) else {}
    plan = SymbolPlan()
    best: dict[str, tuple[int, int, str]] = {}   # 先の記号 → (出た回数, 記号表での順の逆, 記号)
    order = {sym: i for i, sym in enumerate(table)}
    for sym in pasted.unknown_symbols:
        info = table.get(sym)
        guess = _guess_target(info) if info else None
        if guess is not None:
            plan.answers[sym] = {"same_as": guess}
            plan.suggestions[sym] = f"same:{guess}"
        elif info is not None:
            plan.undecided.append(sym)
        elif sym not in given:
            plan.undecided.append(sym)
        if info is None:
            continue
        a = given.get(sym)
        target = (a.get("same_as") if isinstance(a, dict) else None) if a is not None else guess
        when = "" if info.get("rest") else (
            f"（{_clock_label(info['start'])}〜"
            f"{'24:00' if info['end'] in (0, 1440) and info['start'] > 0 else _clock_label(info['end'])}）")
        if target is None and a is not None:
            plan.lines.append(f"{sym}＝新しい勤務{when}")
            continue
        if target is None:
            plan.lines.append(f"{sym}＝まだ決めていません{when}。短い勤務などは上の欄で選んでください")
            continue
        label = "休み" if target == "公" else names.get(target, target)
        plan.lines.append(f"{sym}＝{label}{when}")
        if target in GUESS_TIMED and not info.get("rest"):
            key = (uses[sym], -order[sym], sym)
            if target not in best or key > best[target]:
                best[target] = key
    for target, (_, _, sym) in best.items():
        info = table[sym]
        length = (info["end"] - info["start"]) % 1440 or 1440
        hours = length / 60
        plan.times[target] = {"start": f"{info['start'] // 60:02d}:{info['start'] % 60:02d}",
                              "hours": int(hours) if hours == int(hours) else round(hours, 2)}
        plan.time_from[target] = sym
    # 同じ勤務に記号を2つ以上当てた時（日勤の c と d など）。時刻が違っても1つの勤務にまとまることを言う
    # （2026-11-05 受け止めの主任役・リーダー役がそろって、勝手にまとめられたと読んだ）
    same: dict[str, list[str]] = {}
    for sym in pasted.unknown_symbols:
        if sym in table:
            a = given.get(sym)
            t = (a.get("same_as") if isinstance(a, dict) else None) if a is not None else plan.answers.get(sym, {}).get("same_as")
            if t in GUESS_TIMED:
                same.setdefault(t, []).append(sym)
    for t, syms in same.items():
        if len(syms) >= 2 and t in plan.time_from:
            plan.lines.append(f"{'・'.join(syms)}は、同じ{names.get(t, t)}として読みます。次の勤務までの間は、いちばん多く出た"
                              f"{plan.time_from[t]}の時刻で数えます。分けたい時は、上の欄でどれかを［新しい勤務］にしてください")
    return plan


# ---------------------------------------------------------------- 見つける

def _rule(rid: str, name: str, kind: str, flex: str, params: dict, evidence: str) -> dict:
    return {"id": rid, "ver": 1, "name": name, "kind": kind, "flex": flex, "params": params,
            "who": None, "when": None, "scope": "rule",
            "meta": {"how": "貼った表から見つけた", "evidence": evidence}}


def _answer_type(sym: str, a, regular: bool) -> dict | None:
    """知らない記号の答え（名前と種類）から勤務の種類を作る。答えが足りない・おかしければ None。
    新しい勤務は、ふだんの勤務（regular。決め方は infer）なら翌月も置き、そうでなければ頼まれた時だけ置く。"""
    if not isinstance(a, dict) or a.get("kind") not in ANSWER_KINDS or not str(a.get("name", "")).strip():
        return None
    st = {"symbol": sym, "name": str(a["name"]).strip(), "kind": a["kind"]}
    if a["kind"] == "work":
        if a.get("next_is_ake"):
            st["next_is_ake"] = True
        if not regular:
            st["on_request_only"] = True
    elif a["kind"] != "ake":
        st["on_request_only"] = True
    return st


def _own_shifts(table: dict[str, list[str]], unknown: list[str], answers: dict) -> set[str]:
    """新しい勤務と答えた記号のうち、誰かが2回以上入り、その人の勤務の日（明け・休みを除く）の半分以上を占める物。
    パートの人の短い勤務は、月の半分より少ない日にしか出なくても、その人のふだんの勤務なので翌月も置く
    （国の記載例のグループホーム。週2回の短い勤務が頼まれた時だけになって翌月に1回も入らず、その人たちは
    早番・日勤・遅番に入っていた。2026-11-05）。出張・会議など、人の勤務のうちたまにしか無い物は当たらない。
    1回だけの物も当てない（長く休んだ人が1回だけ出た会議を、翌月のふだんの勤務にしないため）。"""
    new = {s for s in unknown if isinstance(answers.get(s), dict) and answers[s].get("kind") == "work"}
    work = new | {s["symbol"] for s in _template() if s["kind"] == "work"}
    out: set[str] = set()
    for row in table.values():
        days = [c for c in row if c in work]
        out |= {s for s, k in Counter(c for c in days if c in new).items() if k >= 2 and k * 2 >= len(days)}
    return out


def _run_ends(table: dict[str, list[str]], sym: str) -> list[str | None]:
    """記号の連なりの最後の日の、次の日の記号（月末は None）。"""
    out = []
    for row in table.values():
        for d, c in enumerate(row):
            if c == sym and (d + 1 >= len(row) or row[d + 1] != sym):
                out.append(row[d + 1] if d + 1 < len(row) else None)
    return out


def _spellings(pasted: Pasted, same: dict[str, str]) -> dict[str, tuple[str, Counter]]:
    """アプリの記号 → (持ち出す書き方, 貼った表での書き方ごとの回数)。持ち出す書き方は、いちばん多かった書き方
    （同数なら、記号そのままの書き方を先に、次に先に出た書き方）。"""
    merged: dict[str, Counter] = {}
    for sym, cnt in pasted.spelled.items():
        merged.setdefault(same.get(sym, sym), Counter()).update(cnt)
    return {sym: (max(cnt, key=lambda w: (cnt[w], w == sym)), cnt) for sym, cnt in merged.items()}


def _paste_symbols(pasted: Pasted, same: dict[str, str]) -> dict[str, str]:
    """アプリの記号 → 貼った表での書き方。アプリの記号と同じ書き方は入れない。"""
    return {sym: best for sym, (best, _) in _spellings(pasted, same).items() if best != sym}


def _paste_merged(pasted: Pasted, same: dict[str, str]) -> dict[str, dict]:
    """1つの記号に書き方が2つ以上あった時（休と希、空欄と休 など）、持ち出しで1つにまとまる書き方。
    持ち出す時は1つの書き方にしか戻せないので、コピーの知らせで伝える（2026-11-04 Astra の指摘）。"""
    return {sym: {"to": best, "from": [w for w in cnt if w != best]}
            for sym, (best, cnt) in _spellings(pasted, same).items() if len(cnt) >= 2}


def infer(pasted: Pasted, year: int, month: int, answers: dict | None = None) -> Proposal:
    days = calendar.monthrange(year, month)[1]
    if pasted.days != days:
        if pasted.days == 28:
            raise ParseError("4週（28日）の表のようです。様式を暦月に切り替えてからコピーしてください")
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
    # 新しい勤務は、貼った月の半分以上の日に出たか、誰かが2回以上入ってその人の勤務の日の半分以上を占めれば、
    # ふだんの勤務にする
    unknown = [s for s in pasted.unknown_symbols if s not in same]
    own = _own_shifts(table, unknown, answers)
    answered: dict[str, dict] = {}
    for sym in unknown:
        st = _answer_type(sym, answers.get(sym), days_with(sym) * 2 >= n or sym in own)
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
    # 夜勤以外は、その人が貼った月に入った勤務だけ（1つも入っていない人は全部）。誰かのふだんの勤務だからと足した
    # 新しい勤務（パートの人の短い勤務など。月の半分より少ない日にしか出ない）は、入った人だけに入れ、全部の中にも
    # 入れない（貼った月に勤務の無かった人が、ほかの人の短い勤務に入らないように）
    plain = [s for s in ordinary if s not in night_syms]
    shared = [s for s in plain if s not in own or days_with(s) * 2 >= n]
    staff = []
    narrowed = []
    narrowed_info: dict[str, tuple[list[str], list[str]]] = {}   # 名前 → (入った勤務, 入っていない勤務)
    for name, row in table.items():
        worked = [s for s in plain if s in row]
        mine = worked or shared
        miss = [s for s in shared if s not in worked]
        if worked and miss:
            narrowed.append(name)
            narrowed_info[name] = (worked, miss)
        can = mine + [s for s in night_syms if s in ordinary and s in row]
        staff.append({"name": name, "unit": pasted.unit_of.get(name, "全体"), "can": can})
    staff_evidence = ""
    if narrowed:
        # 誰の・どの勤務の話かを名前と勤務で言う（「〇〇さんは、11月に入った勤務だけにしました」だけでは
        # 何のことか分からない、と受け止めのリーダー役。2026-11-04）
        n0 = narrowed[0]
        mine0, miss0 = narrowed_info[n0]
        nm = lambda s: getattr(by_sym.get(s), "name", s)   # noqa: E731
        others = narrowed[1:]
        listed = "・".join(others[:5]) + ("など" if len(others) > 5 else "")
        more = f"ほか{len(others)}人（{listed}さん）も、{month}月に入った勤務の種類だけにしています。" if others else ""
        staff_evidence = (f"{n0}さんは{month}月に{'・'.join(nm(s) for s in mine0)}だけだったので、来月もその勤務だけで組みます。"
                          + more + f"ほかの勤務も入れる時は「{n0}さんは{nm(miss0[0])}も」のように一言で")

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
    # ユニットごとに0人の日がある勤務は、施設全体の人数も守る。夜勤を2人でユニットを順に回す表では、ユニットごとの
    # 夜勤は0人の日があるので下限が付かず、翌月は施設全体で夜勤0人の夜ができていた（国の記載例の特養。2026-11-05）
    if each:
        for sym in ordinary:
            if min(cover[sym]) > 0:
                continue
            tot = [sum(1 for row in table.values() if row[d] == sym) for d in range(n)]
            lo, hi = min(tot), max(tot)
            if lo == hi and lo > 0:
                add(f"{names[sym]}の人数（施設全体）", "coverage", "fixed",
                    {"shifts": [sym], "scope": "all", "op": "==", "n": lo},
                    f"{month}月は毎日、施設全体で{names[sym]}が{lo}人")
            elif lo < hi:
                if lo > 0:
                    add(f"{names[sym]}の人数（施設全体の下限）", "coverage", "fixed",
                        {"shifts": [sym], "scope": "all", "op": ">=", "n": lo},
                        f"{month}月の{names[sym]}は日によって施設全体で{lo}〜{hi}人。いちばん少ない日の{lo}人を下限にした")
                add(f"{names[sym]}の人数（施設全体の上限）", "coverage", "negotiable",
                    {"shifts": [sym], "scope": "all", "op": "<=", "n": hi},
                    f"{month}月の{names[sym]}は日によって施設全体で{lo}〜{hi}人。いちばん多い日の{hi}人を上限にした")
    # 土日祝と平日の人数の違いはルールにしない（足すと表を作る計算が10倍ほど重くなった。2026-11-03）。
    # 違いは作った後の画面に赤で出し、一言（「土日の日勤は1人に」）で直す
    # 夜勤の回数。夜勤をした人の回数の幅を、夜勤に入れる人の月の回数にする（ひだまりの「夜勤の回数」と同じ形）
    nights = [sum(1 for x in row if x in night_syms) for row in table.values()]
    did = [k for k in nights if k > 0]
    if night_syms and did:
        add("夜勤の回数（夜勤に入れる人）", "count", "operational",
            {"shifts": list(night_syms), "min": min(did), "max": max(did), "capable_only": True},
            f"{month}月に夜勤をした人は{min(did)}〜{max(did)}回")
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
    merge_notes = [f"{nm}さんは2行（兼務）あったので、1人にまとめました（同じ日に両方の行に記号がある時は1行目の記号）"
                   for nm in pasted.merged]
    rest_notes = [f"{nm}さん 公休{v}日（ほかの人は{mode}日）。月の途中から入った人や長く休んだ人なら、"
                  f"作った後に一言で直してください（例「{nm}さん、公休は{mode}日」）" for nm, v in long_rest.items()]
    add("公休の数", "count", "operational", {"shifts": ["公"], "min": mode, "max": mode},
        f"公休は{len(rests)}人中{hit}人が{mode}日" + note,
        who=[nm for nm, v in rests.items() if v == mode] if outliers else None)
    for nm, v in outliers.items():
        add(f"公休の数（{nm}さん）", "count", "operational", {"shifts": ["公"], "min": v, "max": v},
            f"{nm}さんの公休は{month}月に{v}日（ほかの人は{mode}日）。{nmo}月も{v}日で作ります"
            f"（変える時は、作った後に「{nm}さん、公休は月{v + 1}日」のように一言で）", who=[nm])

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
                    long_rest=long_rest, rest_mode=mode, rest_notes=rest_notes, merge_notes=merge_notes,
                    paste_symbols=_paste_symbols(pasted, same), paste_merged=_paste_merged(pasted, same))


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
    # 取り込んだ施設は、次の勤務までの間が短い並び（遅番の翌日の早番など）を最初から避ける。デモの施設には入れない
    out["quality"] = {**out.get("quality", {}), "short_rest": SHORT_REST_WEIGHT, "short_rest_hours": SHORT_REST_HOURS}
    hol = {k: list(v) for k, v in HOLIDAYS.items()}
    for y, m in ((year, month), (ny, nm)):
        key = f"{y:04d}-{m:02d}"
        if key not in HOLIDAYS:
            hol[key] = []
            notes.append(f"{y}年{m}月の祝日は入っていません")
    out["holidays"] = hol
    return out


def timed_kind(st: dict) -> bool:
    """始まりの時刻と長さを聞く勤務（夜勤・明け・休みは聞かない）。"""
    return st.get("kind") == "work" and not st.get("next_is_ake")


def _clock_text(v) -> str:
    m = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{2})", _nfkc(str(v or "")).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise OnboardError(f"始まりの時刻「{v}」は、7:00 のように入れてください")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def apply_times(shift_types: list[dict], times: dict | None) -> None:
    """取り込みの時に確かめた勤務の時刻（記号 → {start, hours}）を勤務の種類に書く。空は時刻なし（数えない）。
    渡されなかった記号は、そのまま（見本の時刻）。"""
    for st in shift_types:
        if not timed_kind(st) or not isinstance(times, dict) or st["symbol"] not in times:
            continue
        t = times[st["symbol"]] or {}
        start, hours = str(t.get("start") or "").strip(), t.get("hours")
        if start == "" and hours in (None, ""):
            st.pop("start", None)
            st.pop("hours", None)
            continue
        if start == "" or hours in (None, ""):
            raise OnboardError(f"{st['name']}は、始まりの時刻と長さ（時間）の両方を入れるか、両方を空にしてください")
        try:
            h = float(_nfkc(str(hours)))
        except ValueError:
            raise OnboardError(f"{st['name']}の長さ（時間）は数字で入れてください")
        if not 0 < h <= 24:
            raise OnboardError(f"{st['name']}の長さ（時間）は、0より大きく24以下にしてください")
        st["start"] = _clock_text(start)
        st["hours"] = int(h) if h == int(h) else h


def create_facility(proposal: Proposal, root: Path, *, name: str, next_year: int, next_month: int,
                    answers: dict | None = None, times: dict | None = None) -> Created:
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
    apply_times(shift_types, times)

    notes: list[str] = []
    t0 = time.perf_counter()
    settings = _settings(proposal.year, proposal.month, next_year, next_month, notes)
    if proposal.paste_symbols:
        settings["paste_symbols"] = dict(proposal.paste_symbols)   # 持ち出す時に、施設の書き方へ戻す
    if proposal.paste_merged:
        settings["paste_merged"] = dict(proposal.paste_merged)     # 1つにまとまる書き方（コピーの知らせで伝える）
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
    short_rest = None
    if made:
        from .interval import count_short
        line = settings["quality"]["short_rest_hours"]
        short_rest = {"line": line, "month": proposal.month, "next_month": next_month,
                      "before": count_short(proposal.table, shift_types, line)[0],
                      "after": count_short(nxt.table, shift_types, line, nxt.prev_tail)[0],
                      "untimed": [st["name"] for st in shift_types
                                  if timed_kind(st) and not (st.get("start") and st.get("hours"))]}
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
                   seconds=round(r.seconds, 2), notes=notes, diffs=diffs, short_rest=short_rest)


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
                # 戻すための一言の例。増えた側を、貼った月の平均（四捨五入）に決める一言
                # （2026-11-04 本物の Haiku で「土日の日勤は1人」が2秒・17マスで貼った月の形に戻った。
                #   「日勤は平日2人、土日1人」は平日の2人以上がぶつかり32秒かかった）
                say = (f"土日の{names[sym]}は{int(ce + 0.5)}人" if ne > ce else
                       f"平日の{names[sym]}は{int(cw + 0.5)}人" if nw > cw else "")
                out.append({"kind": "weekday_shape", "say": say,
                            "why": f"平日と土日の{names[sym]}の分け方は、ルールにしていないので、{month}月と変わることがあります",
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
