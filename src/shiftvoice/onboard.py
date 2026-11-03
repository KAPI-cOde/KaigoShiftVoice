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
import hashlib
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
ROLE_WORDS = {"介護", "看護", "介護福祉士", "パート", "常勤", "非常勤", "リーダー"}
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
    """日の見出し（1・1日・11/1・11月1日・2026-11-01 など）なら日にちの数、違えば None。"""
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


def _unit_name(label: str) -> str:
    u = re.sub(r"ユニット$", "", label).strip()
    return u or label


def parse_paste(text: str) -> Pasted:
    text = _nfkc(text)
    delim = "\t" if "\t" in text else ","
    lines = re.split(r"\r\n|\r|\n", text)

    rows: list[tuple[int, list[str]]] = []   # (行番号, セル)
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        rows.append((i, [c.strip().strip('"').strip() for c in line.split(delim)]))

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
        return (not name or any(w in name for w in SUMMARY_WORDS) or name in shift_names or name.isdigit())

    def is_total_row(cells: list[str], name_idx: int) -> bool:
        """名前以外のセルが空でなく、全部が数字の行は集計の行（出勤者数・公休数 など）。"""
        vals = [c for j, c in enumerate(cells) if j != name_idx and c]
        return bool(vals) and all(re.fullmatch(r"\d+(?:\.\d+)?", v) for v in vals)

    # items は ("unit", 名前) か ("staff", 行番号, 名前, 日のセル)
    items: list[tuple] = []
    skipped = 0
    n_header: int | None = None
    if header and header[0][0][0] > 0:
        (cols, blanks), hcells = header
        n_header = len(cols)
        left = range(cols[0])
        nc = next((j for j in left if re.sub(r"\s", "", hcells[j]) in NAME_HEADS), None)
        if nc is None:
            def score(j: int) -> int:
                vals = [_clean_name(c[j]) for _, c in body if j < len(c) and not _is_weekday_row(c)]
                return sum(1 for v in vals if v and not v.isdigit() and v not in ROLE_WORDS)
            nc = max(left, key=lambda j: (score(j), -j))
        for lineno, cells in body:
            if _is_weekday_row(cells):
                continue
            name = _clean_name(cells[nc]) if nc < len(cells) else ""
            if is_label(name) or is_total_row(cells, nc):
                skipped += 1
                continue
            days = [cells[c] if c < len(cells) else "" for c in cols]
            if not any(days):
                items.append(("unit", name))
                continue
            if len(cells) <= cols[-1]:
                k = sum(1 for c in cols if c < len(cells))
                raise ParseError(f"{lineno}行目の「{name}」は日の数が{k}日分しかなく、"
                                 f"ほかの行の{n_header}日に足りません")
            if any(c < len(cells) and cells[c] for c in blanks):
                raise ParseError(f"{lineno}行目の「{name}」に、日の見出しが空の列の中身があります。"
                                 "その列を外してコピーしてください")
            items.append(("staff", lineno, name, days))
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
            items.append(("staff", lineno, name, rest) if any(rest) else ("unit", name))
        if n_header is None:
            # 全部空の列を取り除く（見出しが無い時の読み方）
            sc = [it[3] for it in items if it[0] == "staff"]
            width = max((len(c) for c in sc), default=0)
            empty = {i for i in range(width) if all(i >= len(c) or not c[i] for c in sc)}
            if empty:
                items = [it if it[0] == "unit" else (it[0], it[1], it[2], [c for i, c in enumerate(it[3]) if i not in empty])
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
    blank = 0
    unknown: list[str] = []
    for it in items:
        if it[0] == "unit":
            cur_unit = _unit_name(it[1])
            continue
        _, lineno, name, cells = it
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
        u = cur_unit or "全体"
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

    # 夜の翌日が明の割合（月末の夜は翌日が表の外なので数えない）
    night_pairs = [(row[d], row[d + 1]) for row in table.values() for d in range(n - 1) if row[d] == "夜"]
    night_ake = sum(1 for _, nx in night_pairs if nx == "明")
    ake_follows_night = bool(night_pairs) and night_ake / len(night_pairs) >= RATE_LINE

    # 勤務の種類。表に出た記号と公だけ
    shift_types = []
    for s in _template():
        if s["symbol"] not in used and s["symbol"] != "公":
            continue
        s = dict(s)
        if s["symbol"] == "夜":
            s["next_is_ake"] = ake_follows_night
        shift_types.append(s)
    by_sym = {s["symbol"]: ShiftType(**s) for s in shift_types}
    names = {s["symbol"]: s["name"] for s in shift_types}
    unknown = [s for s in pasted.unknown_symbols if s not in same]
    # 休みや有給と答えた記号は、連勤の勤務の日に数えない
    off = {s for s in unknown if isinstance(answers.get(s), dict) and answers[s].get("kind") in ("rest", "leave")}

    # 職員。夜以外のふつうの勤務は全員、夜はその月に夜をした人だけ
    ordinary = [s.symbol for s in by_sym.values() if s.kind == "work" and not s.on_request_only]
    staff = []
    for name, row in table.items():
        can = [s for s in ordinary if s != "夜"]
        if "夜" in ordinary and "夜" in row:
            can.append("夜")
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

    # 夜→明・明は夜の翌日だけ・明→公
    if ake_follows_night and "明" in by_sym:
        add("夜勤の翌日は明け", "sequence", "fixed", {"if": ["夜"], "next_in": ["明"]},
            f"夜勤の翌日は{len(night_pairs)}回中{night_ake}回が明け")
    ake_prev = [(row[d - 1], row[d]) for row in table.values() for d in range(1, n) if row[d] == "明"]
    if "夜" in by_sym and ake_prev:
        ok = sum(1 for pv, _ in ake_prev if pv == "夜")
        if ok / len(ake_prev) >= RATE_LINE:
            add("明けは夜勤の翌日だけ", "sequence", "fixed", {"if": ["明"], "prev_in": ["夜"]},
                f"明けの前の日は{len(ake_prev)}回中{ok}回が夜勤")
    ake_next = [(row[d], row[d + 1]) for row in table.values() for d in range(n - 1) if row[d] == "明"]
    if ake_next:
        ok = sum(1 for _, nx in ake_next if nx == "公")
        if ok / len(ake_next) >= RATE_LINE:
            add("明けの翌日は公休", "sequence", "fixed", {"if": ["明"], "next_in": ["公"]},
                f"明けの翌日は{len(ake_next)}回中{ok}回が公休")

    # 連勤の上限。数え方は ShiftType.counts_as_workday。知らない記号は勤務の日として数える（ゆるい側）
    def works(sym: str) -> bool:
        if sym in by_sym:
            return by_sym[sym].counts_as_workday
        return sym not in _template_symbols() and sym not in off

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

    return Proposal(year=year, month=month, table=table, shift_types=shift_types, units=units,
                    staff=staff, rules=rules, unknown_symbols=unknown, same_as=same)


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

    # 知らない記号の答え。足りなければ作らない
    missing = [s for s in proposal.unknown_symbols if s not in answers]
    if missing:
        raise OnboardError("次の記号が何か教えてください。" + "・".join(missing))
    shift_types = [dict(s) for s in proposal.shift_types]
    for sym in proposal.unknown_symbols:
        a = answers[sym]
        if not isinstance(a, dict) or a.get("kind") not in ANSWER_KINDS or not str(a.get("name", "")).strip():
            raise OnboardError(f"記号「{sym}」の答えは、名前と種類（{'・'.join(ANSWER_KINDS)}）が要ります")
        st = {"symbol": sym, "name": str(a["name"]).strip(), "kind": a["kind"]}
        if a["kind"] != "ake":
            st["on_request_only"] = True   # 頼まれた時だけ置く。作る時に勝手に使わない
        shift_types.append(st)

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
        for st in shift_types:
            if st["kind"] in ("work", "ake") and st["symbol"] in pasted_syms and st["symbol"] not in placed:
                notes.append(f"{st['name']}（{st['symbol']}）が{next_month}月の表に1回も入っていません。"
                             "何の勤務かの答えを確かめてください")
    else:
        notes.append("翌月の表はまだ作れていません")
    note = r.note if made else r.message
    return Created(root=root, facility_id=fac["id"], staff_count=len(proposal.staff),
                   rule_count=len(proposal.rules), next_table_made=made, stage_note=note,
                   seconds=round(r.seconds, 2), notes=notes)
