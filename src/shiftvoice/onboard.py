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
from dataclasses import dataclass, field
from pathlib import Path

from .build import build_original
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
UNIT_HEADS = {"ユニット", "棟", "フロア", "ユニット名"}
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
    suggestions: dict[str, str] = field(default_factory=dict)   # 知らない記号 → おすすめの選び方（same:夜 / night）


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
                  aliases=aliases, unit_of=unit_of, unit_list=unit_list)


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
    answers = answers or {}
    # 知らない記号の答えのうち「既存の勤務と同じ」は、先に表の中で置き換えてから見つける
    same: dict[str, str] = {}
    for sym in pasted.unknown_symbols:
        a = answers.get(sym)
        if isinstance(a, dict) and a.get("same_as"):
            if a["same_as"] not in _template_symbols():
                raise OnboardError(f"記号「{sym}」を同じにする先「{a['same_as']}」は、知らない勤務です")
            same[sym] = a["same_as"]
    table = {k: [same.get(c, c) for c in row] for k, row in pasted.table.items()}
    n = pasted.days
    used = {s for row in table.values() for s in row}

    def days_with(sym: str) -> int:
        return sum(1 for d in range(n) if any(row[d] == sym for row in table.values()))

    # 夜の翌日が明の割合（月末の夜は翌日が表の外なので数えない）
    def after(sym: str) -> tuple[int, int]:
        pairs = [row[d + 1] for row in table.values() for d in range(n - 1) if row[d] == sym]
        return len(pairs), sum(1 for nx in pairs if nx == "明")

    night_n, night_ake = after("夜")
    ake_follows_night = night_n > 0 and night_ake / night_n >= RATE_LINE

    # 勤務の種類。表に出た記号と公だけ。答えのある知らない記号も入れる
    shift_types = []
    for s in _template():
        if s["symbol"] not in used and s["symbol"] != "公":
            continue
        s = dict(s)
        if s["symbol"] == "夜":
            s["next_is_ake"] = ake_follows_night
        shift_types.append(s)
    unknown = [s for s in pasted.unknown_symbols if s not in same]
    answered: dict[str, dict] = {}
    for sym in unknown:
        st = _answer_type(sym, answers.get(sym), days_with(sym) * 2 >= n)
        if st:
            answered[sym] = st
            shift_types.append(st)
    by_sym = {s["symbol"]: ShiftType(**s) for s in shift_types}
    names = {s["symbol"]: s["name"] for s in shift_types}

    # 夜勤の記号。夜と、答えで「夜勤（次の日は明け）」にした記号
    night_syms = [s.symbol for s in by_sym.values()
                  if s.kind == "work" and (s.symbol == "夜" or (s.next_is_ake and s.symbol in answered))]

    # 職員。夜勤以外のふつうの勤務は全員、夜勤はその月にした人だけ
    ordinary = [s.symbol for s in by_sym.values() if s.kind == "work" and not s.on_request_only]
    staff = []
    for name, row in table.items():
        can = [s for s in ordinary if s not in night_syms]
        can += [s for s in night_syms if s in ordinary and s in row]
        staff.append({"name": name, "unit": pasted.unit_of.get(name, "全体"), "can": can})

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
    for sym in ordinary:
        if each:
            counts = [sum(1 for nm, row in table.items() if row[d] == sym and pasted.unit_of.get(nm) == u)
                      for u in units for d in range(n)]
        else:
            counts = [sum(1 for row in table.values() if row[d] == sym) for d in range(n)]
        lo, hi = min(counts), max(counts)
        if lo == hi:
            add(f"{names[sym]}の人数", "coverage", "fixed",
                {"shifts": [sym], "scope": scope, "op": "==", "n": lo},
                f"{month}月は毎日、{where}{names[sym]}が{lo}人")
        elif lo > 0:
            add(f"{names[sym]}の人数", "coverage", "negotiable",
                {"shifts": [sym], "scope": scope, "op": ">=", "n": lo},
                f"{month}月の{names[sym]}は日によって{where}{lo}〜{hi}人。いちばん少ない日の{lo}人を下限にした")

    # 公休の数。いちばん多い数の人たちに1本、違う人は人ごとに1本（パートの人が翌月フルで働く表を防ぐ）
    rests = {nm: row.count("公") for nm, row in table.items()}
    mode, hit = sorted(Counter(rests.values()).items(), key=lambda kv: (-kv[1], kv[0]))[0]
    ny, nmo = next_ym(year, month)
    note = ""
    if calendar.monthrange(ny, nmo)[1] != days:
        note = f"。{nmo}月は{calendar.monthrange(ny, nmo)[1]}日あります。公休の数を確かめてください"
    outliers = {nm: v for nm, v in rests.items() if v != mode}
    add("公休の数", "count", "fixed", {"shifts": ["公"], "min": mode, "max": mode},
        f"公休は{len(rests)}人中{hit}人が{mode}日" + note,
        who=[nm for nm, v in rests.items() if v == mode] if outliers else None)
    for nm, v in outliers.items():
        add(f"公休の数（{nm}さん）", "count", "fixed", {"shifts": ["公"], "min": v, "max": v},
            f"{nm}さんの公休は{v}日" + note, who=[nm])

    # 夜勤→明・明は夜勤の翌日だけ・明→公
    if "明" in by_sym:
        for sym in night_syms:
            tot, ok = after(sym)
            if tot and ok / tot >= RATE_LINE:
                add("夜勤の翌日は明け" if sym == "夜" else f"{names[sym]}の翌日は明け", "sequence", "fixed",
                    {"if": [sym], "next_in": ["明"]}, f"{names[sym]}の翌日は{tot}回中{ok}回が明け")
    ake_prev = [(row[d - 1], row[d]) for row in table.values() for d in range(1, n) if row[d] == "明"]
    if night_syms and ake_prev:
        ok = sum(1 for pv, _ in ake_prev if pv in night_syms)
        if ok / len(ake_prev) >= RATE_LINE:
            add("明けは夜勤の翌日だけ", "sequence", "fixed", {"if": ["明"], "prev_in": list(night_syms)},
                f"明けの前の日は{len(ake_prev)}回中{ok}回が夜勤")
    ake_next = [(row[d], row[d + 1]) for row in table.values() for d in range(n - 1) if row[d] == "明"]
    if ake_next:
        ok = sum(1 for _, nx in ake_next if nx == "公")
        if ok / len(ake_next) >= RATE_LINE:
            add("明けの翌日は公休", "sequence", "fixed", {"if": ["明"], "next_in": ["公"]},
                f"明けの翌日は{len(ake_next)}回中{ok}回が公休")

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
    suggestions = {}
    for sym in unknown:
        tot, ok = after(sym)
        if tot >= 3 and ok / tot >= RATE_LINE:
            suggestions[sym] = "same:夜" if "夜" in used else "night"

    return Proposal(year=year, month=month, table=table, shift_types=shift_types, units=units,
                    staff=staff, rules=rules, unknown_symbols=unknown, same_as=same,
                    problems=problems, suggestions=suggestions)


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
        for st in shift_types:
            if (st["kind"] in ("work", "ake") and not st.get("on_request_only")
                    and st["symbol"] in pasted_syms and st["symbol"] not in placed):
                days_seen = sum(1 for d in range(n_days)
                                if any(row[d] == st["symbol"] for row in proposal.table.values()))
                if n_days and days_seen * 2 >= n_days:
                    missing_daily.append(f"{st['name']}（{st['symbol']}）")
                else:
                    notes.append(f"{st['name']}（{st['symbol']}）が{next_month}月の表に1回も入っていません。"
                                 "何の勤務かの答えを確かめてください")
        if missing_daily:
            # 出口の確かめ。貼った月に毎日のようにいた勤務が翌月に1回も無い表は、どの答え方から来たかに関わらず
            # 「できた」と言わない（入口の確かめをすり抜けた黙って違う表を、ここで必ず止める）
            made = False
            r.note = None
            r.message = ("・".join(missing_daily) + f"は{proposal.month}月にほぼ毎日いましたが、"
                         f"{next_month}月の表に1回も入りませんでした。記号の答え（何の勤務か）を確かめてください")
    if not made:
        notes.append("翌月の表はまだ作れていません")
    note = r.note if made else r.message
    return Created(root=root, facility_id=fac["id"], staff_count=len(proposal.staff),
                   rule_count=len(proposal.rules), next_table_made=made, stage_note=note,
                   seconds=round(r.seconds, 2), notes=notes)
