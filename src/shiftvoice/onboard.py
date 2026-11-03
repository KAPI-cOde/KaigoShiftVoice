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
}
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

def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace("　", " ").strip()


def _day_number(label: str) -> int | None:
    """日の見出し（1・1日・11/1・11月1日・2026-11-01 など）なら日にちの数、違えば None。"""
    m = _DAY_MD.match(label) or _DAY.match(label)
    if not m:
        return None
    d = int(m.group(2))
    return d if 1 <= d <= 31 else None


def _is_header(cells: list[str]) -> list[int] | None:
    ne = [c for c in cells if c]
    if len(ne) < 6:
        return None
    for rest in (ne[1:], ne):  # 先頭が「氏名」などの名前の列の時と、名前の列が無い時
        nums = [_day_number(c) for c in rest]
        if all(n is not None for n in nums) and nums == list(range(1, len(nums) + 1)):
            return nums
    return None


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
    return name


def parse_paste(text: str) -> Pasted:
    text = unicodedata.normalize("NFKC", text)
    delim = "\t" if "\t" in text else ","
    lines = re.split(r"\r\n|\r|\n", text)

    rows: list[tuple[int, list[str]]] = []   # (行番号, セル)
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        rows.append((i, [c.strip().strip('"').strip() for c in line.split(delim)]))

    # 見出しの行を探す。見出しより前の行（表題など）は読まない
    n_header: int | None = None
    start = 0
    for idx, (_, cells) in enumerate(rows):
        nums = _is_header(cells)
        if nums:
            n_header, start = len(nums), idx + 1
            break
    body = rows[start:]

    shift_names = set(ALIASES) | set(SHIFT_WORDS) | _template_words()
    staff_rows: list[tuple[int, str, list[str]]] = []
    skipped = 0
    for lineno, cells in body:
        if _is_weekday_row(cells):
            continue
        k = next((j for j, c in enumerate(cells) if c), None)
        name = _clean_name(cells[k]) if k is not None else ""
        if (not name or any(w in name for w in SUMMARY_WORDS) or name in shift_names
                or name.isdigit()):
            skipped += 1
            continue
        staff_rows.append((lineno, name, cells[k + 1:]))

    if not staff_rows:
        raise ParseError("職員の行が1つも見つかりませんでした。名前の列と日ごとの勤務をまとめてコピーしてください")

    n = n_header
    if n is None:
        n = Counter(len(c) for _, _, c in staff_rows).most_common(1)[0][0]
    if not 28 <= n <= 31:
        raise ParseError(f"日の数が{n}日分しかありません。1か月分（28〜31日）をまとめてコピーしてください"
                         if n < 28 else f"日の数が{n}日分あります。1か月分（28〜31日）だけをコピーしてください")

    seen: dict[str, int] = {}
    table: dict[str, list[str]] = {}
    blank = 0
    unknown: list[str] = []
    for lineno, name, cells in staff_rows:
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
            if sym not in _template_symbols() and sym not in unknown:
                unknown.append(sym)
            row.append(sym)
        table[name] = row
    return Pasted(table=table, days=n, skipped=skipped, blank_as_rest=blank,
                  unknown_symbols=unknown, had_header=n_header is not None)


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


def infer(pasted: Pasted, year: int, month: int) -> Proposal:
    days = calendar.monthrange(year, month)[1]
    if pasted.days != days:
        raise ParseError(f"{month}月は{days}日までですが、貼った表は{pasted.days}日分あります。月を確かめてください")
    table = pasted.table
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
    unknown = [s for s in pasted.unknown_symbols]

    # 職員。夜以外のふつうの勤務は全員、夜はその月に夜をした人だけ
    ordinary = [s.symbol for s in by_sym.values() if s.kind == "work" and not s.on_request_only]
    staff = []
    for name, row in table.items():
        can = [s for s in ordinary if s != "夜"]
        if "夜" in ordinary and "夜" in row:
            can.append("夜")
        staff.append({"name": name, "unit": "全体", "can": can})

    rules: list[dict] = []

    def add(name, kind, flex, params, evidence):
        rules.append(_rule(f"r{len(rules) + 1:03d}", name, kind, flex, params, evidence))

    # 人数
    for sym in ordinary:
        counts = [sum(1 for row in table.values() if row[d] == sym) for d in range(n)]
        lo, hi = min(counts), max(counts)
        if lo == hi:
            add(f"{names[sym]}の人数", "coverage", "fixed",
                {"shifts": [sym], "scope": "all", "op": "==", "n": lo},
                f"{month}月は毎日、{names[sym]}が{lo}人")
        elif lo > 0:
            add(f"{names[sym]}の人数", "coverage", "negotiable",
                {"shifts": [sym], "scope": "all", "op": ">=", "n": lo},
                f"{month}月の{names[sym]}は日によって{lo}〜{hi}人。いちばん少ない日の{lo}人を下限にした")

    # 公休の数
    rests = [row.count("公") for row in table.values()]
    mode, hit = sorted(Counter(rests).items(), key=lambda kv: (-kv[1], kv[0]))[0]
    add("公休の数", "count", "fixed", {"shifts": ["公"], "min": mode, "max": mode},
        f"公休は{len(rests)}人中{hit}人が{mode}日")

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
        return by_sym[sym].counts_as_workday if sym in by_sym else sym not in _template_symbols()

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

    return Proposal(year=year, month=month, table=table, shift_types=shift_types, units=["全体"],
                    staff=staff, rules=rules, unknown_symbols=unknown)


# ---------------------------------------------------------------- 作る

def _short_id(name: str) -> str:
    return "f" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:7]


def _settings(year: int, month: int, ny: int, nm: int, notes: list[str]) -> dict:
    src = read_json(SEED / "facility.json")["settings"]
    out: dict = {"solve": {k: src["solve"][k] for k in SOLVE_KEYS if k in src.get("solve", {})}}
    for k in SETTING_KEYS:
        if k in src:
            out[k] = src[k]
    hol = {}
    for y, m in ((year, month), (ny, nm)):
        key = f"{y:04d}-{m:02d}"
        hol[key] = list(HOLIDAYS.get(key, []))
        if key not in HOLIDAYS:
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
    else:
        notes.append("翌月の表はまだ作れていません")
    note = r.note if made else r.message
    return Created(root=root, facility_id=fac["id"], staff_count=len(proposal.staff),
                   rule_count=len(proposal.rules), next_table_made=made, stage_note=note,
                   seconds=round(r.seconds, 2), notes=notes)
