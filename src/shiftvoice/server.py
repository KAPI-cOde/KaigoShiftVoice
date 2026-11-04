"""手元のサーバー（localhost だけ）。画面は static/index.html、裏側は session.Session。

API（すべて JSON）
- GET  /api/state                     表・札・職員・未反映の発言
- GET  /api/table.tsv                 開いている月の表をタブ区切りで（Excel に貼り戻す・印刷の元）
- POST /api/say     {"text"}          一言
- POST /api/answer  {"uid", "text"}   「毎月ですか、今月だけですか」への返事（"rule"/"once" か話し言葉）
- POST /api/retry   {"uid"}           送り直す
- GET  /api/next/review · POST /api/next/answer|change|undo|build|adopt  翌月へ（ルールの見直し → 返さない案と返す案 → 採る）
- POST /api/voice   16kHz float32     声を文字にして一言と同じ流れへ（X-Answer-Uid があれば聞き返しへの返事）
- POST /api/undo    {}                ひとつ戻す
- POST /api/restore {"rule_id"}       片付けたルールを戻す（次に表を作る時から効く）
- GET  /api/cleanup · POST /api/cleanup {"archive", "keep"}  ルールの整理（片付けの候補を種類ごとにまとめて決める）
- POST /api/distribute {}             表を配った（ここから休みの電話は待機の人に頼む直し）
- POST /api/accept|refuse|cancel {"uid"}  ［了承をもらって直す］［断られた］［やめる］（accept は pick で休みを移す日の候補を選べる。残業などの穴埋めの［記録する］も accept）
- POST /api/move    {"uid", "pick"}   ［休みを移す日を変える］で候補を選ぶ（表は了承まで変えない）
鍵は環境変数 ANTHROPIC_API_KEY（.env からも読む）。画面にも記録にも出さない。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .nextmonth import next_ym
from .onboard import (OnboardError, ParseError, Proposal, Pasted, SymbolPlan, _short_id, _template, create_facility, infer,
                      parse_paste, plan_symbols, timed_kind)
from .reader import HaikuReader, ReadError, SonnetBuilder
from .session import Session

_quiet_running: set[str] = set()


def start_quiet(store: Store, year: int, month: int) -> None:
    """配った月の静かなルールを裏で数える（docs/rule-cleanup.md の1）。配った後の表は大きく変わらないので、
    ここで測った結果を翌月からの見直しに使う。もう測った月・測っている月は流さない。施設の設定 quiet.auto で止められる。"""
    from .quiet import _path, check_month
    if not store.facility().settings.get("quiet", {}).get("auto", True):
        return
    key = f"{store.root}|{year}-{month}"
    if key in _quiet_running or _path(store, year, month).exists():
        return
    _quiet_running.add(key)

    def run():
        try:
            try:
                check_month(store, year, month)
            except Exception as e:   # 測れなくても表の操作は止めない（翌月の見直しに静かな札が出ないだけ）
                print(f"静かなルールの確かめが止まりました（{year}年{month}月） {e}")
            if os.environ.get("ANTHROPIC_API_KEY"):
                try:   # 理由が一時的な事情かを分ける（aging.py。理由の文だけを、名前を伏せて送る）
                    from .aging import HaikuAging, classify_rules
                    classify_rules(store, HaikuAging())
                except Exception as e:
                    print(f"事情のルールの読み分けが止まりました {type(e).__name__}")
        finally:
            _quiet_running.discard(key)
    threading.Thread(target=run, daemon=True).start()
from .store import Store, read_json

SAMPLE_PASTE = Path(__file__).resolve().parents[2] / "data" / "samples" / "komorebi-2026-11.tsv"
SAMPLE_NAME = "こもれびの家（架空）"

STATIC = Path(__file__).resolve().parent / "static"


def sample_info() -> dict:
    """見本の貼り付け（架空の施設B）。"""
    return {"text": SAMPLE_PASTE.read_text(encoding="utf-8"), "name": SAMPLE_NAME, "year": 2026, "month": 11}


def _symbol_plan(data: dict, pasted: Pasted) -> SymbolPlan | None:
    """シフト記号表（任意の2つ目の欄）の文字があれば、記号と時刻を読んで仮の答えを作る。
    作成者がいま選んでいる答えがあれば、その答えで時刻と行を作る（選び直した記号の時刻を残さない）。"""
    text = str(data.get("symbols") or "").strip()
    answers = data.get("answers")
    return plan_symbols(pasted, text, answers if isinstance(answers, dict) else None) if text else None


def _parse_setup(data: dict) -> tuple[Pasted, Proposal, int, int]:
    try:
        year, month = int(data.get("year")), int(data.get("month"))
    except (TypeError, ValueError):
        raise ParseError("貼る表の年と月を数字で入れてください")
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        raise ParseError("貼る表の年と月を確かめてください")
    pasted = parse_paste(str(data.get("text") or ""))
    answers = data.get("answers")
    prop = infer(pasted, year, month, answers if isinstance(answers, dict) else None)
    plan = _symbol_plan(data, pasted)
    if plan:
        # 記号表から読んだおすすめ（画面が先に入れる）と、勤務の時刻の初めの値。ほかの見つけ方のおすすめより先にする
        prop.suggestions.update(plan.suggestions)
        prop.symbol_plan = plan
        for st in prop.shift_types:
            t = plan.times.get(st["symbol"])
            if t and timed_kind(st):
                st["start"], st["hours"] = t["start"], t["hours"]
    return pasted, prop, year, month


def _reading(pasted: Pasted, prop: Proposal) -> list[str]:
    """「勤務の記号の読み方」。別名で置き換えた物、答えで同じにした物、空欄。"""
    names = {s["symbol"]: s["name"] for s in _template()}
    out = [f"{raw}→{names.get(sym, sym)}" for raw, sym in pasted.aliases.items()]
    out += [f"{raw}→{names.get(sym, sym)}" for raw, sym in prop.same_as.items()]
    if pasted.blank_as_rest:
        who = [f"{nm}さん{pasted.blank_by[nm]}マス" for nm in prop.long_rest if pasted.blank_by.get(nm)]
        out.append(f"空欄（{pasted.blank_as_rest}マス）→公休" + (f"（多いのは{'・'.join(who)}）" if who else ""))
    return out


def preview_out(pasted: Pasted, prop: Proposal) -> dict:
    ny, nm = next_ym(prop.year, prop.month)
    return {
        "ok": True, "year": prop.year, "month": prop.month, "next_year": ny, "next_month": nm,
        "staff": list(prop.table), "days": pasted.days,
        "shift_types": [{"symbol": s["symbol"], "name": s["name"], "kind": s["kind"],
                         "on_request_only": bool(s.get("on_request_only")),
                         "timed": timed_kind(s), "start": s.get("start") or "", "hours": s.get("hours") or ""}
                        for s in prop.shift_types],
        "rules": [{"id": r["id"], "name": r["name"], "evidence": r["meta"]["evidence"]} for r in prop.rules],
        "unknown_symbols": list(pasted.unknown_symbols),
        "same_choices": [{"symbol": t["symbol"], "name": t["name"]} for t in _template()],
        "reading": _reading(pasted, prop), "units": list(prop.units),
        "blank_as_rest": pasted.blank_as_rest, "skipped": pasted.skipped,
        "problems": list(prop.problems), "suggestions": dict(prop.suggestions),
        "staff_evidence": prop.staff_evidence, "rest_notes": list(prop.rest_notes), "merge_notes": list(prop.merge_notes),
        "symbol_lines": list(prop.symbol_plan.lines) if prop.symbol_plan else [],
        "symbol_guessed": sorted(prop.symbol_plan.suggestions) if prop.symbol_plan else [],
        # 記号表の時刻を入れた勤務（画面の時刻の欄の説明で名前を挙げる）
        "symbol_times": [s["name"] for s in prop.shift_types if prop.symbol_plan and s["symbol"] in prop.symbol_plan.times],
        "symbol_time_from": dict(prop.symbol_plan.time_from) if prop.symbol_plan else {},   # 勤務 → 時刻を取った記号
    }


def st_name(root: Path) -> str:
    return (read_json(Path(root) / "facility.json") or {}).get("name", "")


def list_facilities(current: Path) -> list[dict]:
    """同じ親フォルダの下で facility.json を持つ置き場の一覧。"""
    current = Path(current)
    out = []
    for d in sorted(current.parent.iterdir()) if current.parent.exists() else []:
        if d.is_dir() and not d.name.endswith(".making") and (d / "facility.json").exists():
            out.append({"id": d.name, "name": st_name(d), "current": d == current})
    return out


def _latest_month(root: Path) -> tuple[int, int] | None:
    """置き場の months/ で、表がある一番新しい月。"""
    best = None
    for f in (Path(root) / "months").glob("????-??.json"):
        d = read_json(f) or {}
        if d.get("table") is None:
            continue
        try:
            ym = (int(f.name[:4]), int(f.name[5:7]))
        except ValueError:
            continue
        best = max(best, ym) if best else ym
    return best


class NoKeyReader:
    """鍵が無い時。読み取りは呼べないと返す（表も帳面も変えない）。"""
    sent: list = []
    calls_claude = False   # Claude を呼ばない（画面には「Claude へは送っていません」と出す）

    def read(self, masked_text, history=None):
        raise ReadError("ANTHROPIC_API_KEY がありません")


def load_dotenv(path: Path) -> None:
    """.env の KEY=VALUE を環境変数へ（すでにある値は上書きしない。中身は表示しない）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def codespaces_host(port: int) -> str | None:
    """GitHub Codespaces の中で動いている時だけ、その Codespace の転送先の名前（<Codespace の名前>-<番号>.<転送の領域>）を返す。
    Codespaces では、画面はこの名前で届く（Host と Origin）。ほかの時は None（今までどおり 127.0.0.1 と localhost だけ）。
    受けるのは、この Codespace のこの番号の名前1つだけ（Codespaces の転送は、既定で持ち主だけが開ける）。"""
    if os.environ.get("CODESPACES") != "true":
        return None
    name = os.environ.get("CODESPACE_NAME", "")
    domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    if not name or not domain:
        return None
    return f"{name}-{port}.{domain}"


WEEKDAY_JA = {"sun": "日", "mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土"}


def table_tsv(st: dict, paste_symbols: dict | None = None) -> str:
    """開いている月の表をタブ区切りにする（Excel の左上のマスに貼れる形）。
    1行目は日付、2行目は曜日、3行目から「名前＋各日の記号」。ユニットが2つ以上ならユニットの列を名前の前に置く。
    待機当番などの欄の行は最後に、その日の担当の名前（さんは外す）を入れる。
    paste_symbols（施設の設定。アプリの記号 → 取り込んだ表での書き方）があれば、勤務の記号をその書き方に戻す。
    無い記号はアプリの記号のまま。"""
    back = paste_symbols or {}
    def cell(v) -> str:
        t = str(v if v is not None else "").replace("\t", " ").replace("\r", " ").replace("\n", " ")
        # Excel に貼った時に式として動かないよう、＝・＋・－・＠で始まる2字以上の文字は先頭に ' を付ける
        # （自動のセキュリティレビューの指摘。記号1字の「-」などはそのまま）
        if len(t) > 1 and t.lstrip()[:1] in ("=", "+", "-", "@", "＝", "＋", "－", "＠"):
            t = "'" + t
        return t

    staff, days = st["staff"], st["days"]
    multi = len({s["unit"] for s in staff}) > 1
    pad = [""] * (2 if multi else 1)
    rows = [pad + [str(d) for d in range(1, days + 1)],
            pad + [WEEKDAY_JA.get(w, "") for w in st["weekdays"]]]
    for s in staff:
        line = [s["unit"]] if multi else []
        line.append(s["name"])
        row = st["table"].get(s["name"]) or []
        line += [back.get(row[d], row[d]) if d < len(row) else "" for d in range(days)]
        rows.append(line)
    for layer in (st.get("layers") or {}).values():
        line = ([""] if multi else []) + [layer.get("name") or "待機当番"]
        for d in range(1, days + 1):
            line.append("・".join(n.removesuffix("さん") for n in (layer.get("assign") or {}).get(str(d), [])))
        rows.append(line)
    return "\n".join("\t".join(cell(c) for c in r) for r in rows) + "\n"


class Box:
    """今開いている施設の入れ物。施設を切り替える時は lock の中で session と voice を差し替える。"""

    def __init__(self, session: Session, voice=None):
        self.session = session
        self.voice = voice
        self.orig_root = Path(session.store.root)     # 起動した時の置き場（声の入口はここの名簿に合わせてある）
        self.orig_voice = voice


def make_handler(session: Session, lock: threading.Lock, has_key: bool, voice=None):
    box = Box(session, voice)

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 発言や鍵を端末に流さない
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _local(self) -> bool:
            """手元からだけ受ける。Host と Origin が 127.0.0.1 か localhost（DNS の付け替えと、ほかのサイトからの送信を止める）。
            GitHub Codespaces の中で動く時だけ、その Codespace の転送先の名前1つも受ける（codespaces_host）。"""
            port = self.server.server_address[1]
            ok = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origins = {f"http://{h}" for h in ok}
            fwd = codespaces_host(port)
            if fwd:
                ok = ok | {fwd}
                origins = origins | {f"https://{fwd}"}
            if self.headers.get("Host") not in ok:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in origins

        def _voice(self, ctype: str):
            """声（16kHz・モノラルの float32 の並び）を文字にして、文字の一言と同じ流れに入れる。声は外へ送らない。"""
            if not self._local() or not ctype.startswith("application/octet-stream"):
                return self._json(403, {"error": "手元の画面からだけ送れます"})
            if box.voice is None:
                return self._json(200, {"error": "声の入口が使えません。文字で入れてください"})
            import numpy as np
            n = int(self.headers.get("Content-Length") or 0)
            samples = np.frombuffer(self.rfile.read(n), dtype="<f4")
            text, secs = box.voice.transcribe(samples)
            if not text:
                return self._json(200, {"error": "聞き取れませんでした。もう一度話すか、文字で入れてください",
                                        "voice_seconds": secs})
            if self.headers.get("X-Text-Only") == "1":
                # ［変える（話す）］は文字にするだけ。中身は画面で確かめてから送る
                return self._json(200, {"text": text, "voice_seconds": round(secs, 3)})
            with lock:
                uid = self.headers.get("X-Answer-Uid")
                r = box.session.answer(uid, text) if uid else box.session.say(text)
                st = box.session.state()
            st["has_key"] = has_key
            self._json(200, {"text": text, "voice_seconds": round(secs, 3), "reply": r.to_dict(), "state": st})

        def _state_out(self) -> dict:
            st = box.session.state()
            st["has_key"] = has_key
            st["has_voice"] = box.voice is not None
            return st

        def _swap(self, root: Path, year: int, month: int) -> dict:
            """lock の中で session を差し替える。声の入口は、起動した置き場の名簿に合わせてあるので、その置き場の時だけ残す。"""
            old = box.session
            new = Session(Store(root), year, month, old.reader, now=old.now, who=old.who, builder=old.builder)
            new.open()
            box.session = new
            box.voice = box.orig_voice if Path(root) == box.orig_root else None
            return self._state_out()

        def _setup(self, data: dict) -> None:
            """自分の施設で始める（貼った表から施設を作る）と、施設の切り替え。"""
            try:
                if self.path == "/api/setup/preview":
                    pasted, prop, year, month = _parse_setup(data)
                    return self._json(200, preview_out(pasted, prop))
                if self.path == "/api/setup/create":
                    pasted, prop, year, month = _parse_setup(data)
                    drop = {str(x) for x in (data.get("drop_rules") or [])}
                    prop.rules = [r for r in prop.rules if r["id"] not in drop]
                    if prop.problems:   # このままでは作れない（毎日いた勤務が来月に入らない など）
                        return self._json(400, {"error": "。".join(prop.problems), "problems": list(prop.problems)})
                    name = str(data.get("name") or "")
                    ny, nm = next_ym(year, month)
                    parent = Path(box.session.store.root).parent
                    if not name.strip():
                        raise OnboardError("施設の名前を入れてください")
                    root = parent / _short_id(name.strip())
                    existing = (root / "facility.json").exists()
                    notes: list[str] = []
                    short_rest = None
                    diffs = None   # もう作ってあった施設を開くだけの時は、違いは出さない
                    if not existing:
                        # 作りかけの置き場を残さない。別の名前の場所で作り、表までできた時だけ置き換える
                        import shutil
                        tmp = root.with_name(root.name + ".making")
                        shutil.rmtree(tmp, ignore_errors=True)
                        try:
                            c = create_facility(prop, tmp, name=name, next_year=ny, next_month=nm,
                                                answers=data.get("answers") or {},
                                                times=data.get("times") if isinstance(data.get("times"), dict) else None)
                        except BaseException:
                            shutil.rmtree(tmp, ignore_errors=True)
                            raise
                        notes = c.notes + ([c.stage_note] if c.stage_note else [])
                        diffs = c.diffs
                        short_rest = c.short_rest
                        if not c.next_table_made:
                            shutil.rmtree(tmp, ignore_errors=True)
                            return self._json(400, {"error": "翌月の表が作れませんでした。" + (c.stage_note or ""), "notes": notes})
                        tmp.rename(root)
                    else:
                        ny, nm = _latest_month(root) or (ny, nm)
                    with lock:
                        st = self._swap(root, ny, nm)
                    message = (f"『{st_name(root)}』はもう作ってあるので、前に作った施設を開きました。"
                               "今回貼った表は使っていません。新しく作る時は別の名前にしてください") if existing else ""
                    return self._json(200, {"ok": True, "existing": existing, "message": message, "facility": {"id": root.name, "name": st_name(root)},
                                            "year": ny, "month": nm, "notes": notes, "diffs": diffs, "short_rest": short_rest,
                                            "from_month": month, "state": st})
                if self.path == "/api/facilities/open":
                    with lock:
                        hit = [f for f in list_facilities(box.session.store.root) if f["id"] == data.get("id")]
                        if not hit:
                            return self._json(400, {"error": "その施設は見つかりません"})
                        root = Path(box.session.store.root).parent / hit[0]["id"]
                        ym = _latest_month(root)
                        if ym is None:
                            return self._json(400, {"error": "その施設には開ける月の表がありません"})
                        st = self._swap(root, *ym)
                    return self._json(200, {"ok": True, "facility": {"id": root.name, "name": st_name(root)}, "state": st})
                return self._json(400, {"error": "知らない操作です"})
            except (ParseError, OnboardError) as e:
                return self._json(400, {"error": str(e)})
            except RuntimeError as e:
                return self._json(400, {"error": str(e)})

        def do_GET(self):
            if not self._local():
                return self._json(403, {"error": "手元からだけ開けます"})
            if self.path in ("/", "/index.html"):
                body = (STATIC / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path in ("/api/next/review", "/api/next/review?split=1"):
                with lock:
                    self._json(200, self.server.flow().review(split=self.path.endswith("split=1")))
            elif self.path == "/api/cleanup":
                with lock:
                    self._json(200, box.session.cleanup())
            elif self.path.startswith("/api/why?"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                try:
                    day = int((q.get("day") or ["0"])[0])
                except ValueError:
                    day = 0
                with lock:
                    self._json(200, box.session.why((q.get("staff") or [""])[0], day))
            elif self.path == "/api/state":
                with lock:
                    st = box.session.state()
                st["has_key"] = has_key
                st["has_voice"] = box.voice is not None
                self._json(200, st)
            elif self.path == "/api/table.tsv":
                with lock:
                    settings = box.session.store.facility().settings
                    back = settings.get("paste_symbols") or {}
                    merged = settings.get("paste_merged") or {}
                    body = table_tsv(box.session.state(), back).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/tab-separated-values; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                if back:   # 施設の書き方に戻した記号（アプリの記号 → 戻した書き方）。日本語を含むので URL エンコードの JSON
                    self.send_header("X-Restored-Symbols", urllib.parse.quote(json.dumps(back, ensure_ascii=False)))
                if merged:   # 貼った表で書き方が2つ以上あり、1つにまとめて出す記号（休と希 など）
                    self.send_header("X-Merged-Symbols", urllib.parse.quote(json.dumps(merged, ensure_ascii=False)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/setup/sample":
                self._json(200, sample_info())
            elif self.path == "/api/facilities":
                with lock:
                    self._json(200, {"facilities": list_facilities(box.session.store.root)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            ctype = self.headers.get("Content-Type") or ""
            if self.path == "/api/voice":
                return self._voice(ctype)
            if not self._local() or not ctype.startswith("application/json"):
                return self._json(403, {"error": "手元の画面からだけ送れます"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "JSON が読めません"})
            if self.path.startswith("/api/setup/") or self.path.startswith("/api/facilities/"):
                return self._setup(data)
            if self.path.startswith("/api/next/"):
                with lock:
                    fl = self.server.flow()
                    try:
                        if self.path == "/api/next/answer":
                            out = fl.answer(data.get("rule_id", ""), data.get("answer", ""), data.get("new_until"),
                                            bool(data.get("split")))
                        elif self.path == "/api/next/change":
                            out = fl.change(data.get("rule_id", ""), data.get("text", ""), box.session.reader,
                                            split=bool(data.get("split")))
                        elif self.path == "/api/next/undo":
                            out = fl.undo(data.get("rule_id", ""), split=bool(data.get("split")))
                        elif self.path == "/api/next/build":
                            out = fl.build()
                        elif self.path == "/api/next/adopt":
                            out = fl.adopt(data.get("plan", ""))
                        else:
                            return self._json(400, {"error": "知らない操作です"})
                    except Exception as e:  # 帳面が受け付けない答え（二重の答えなど）
                        return self._json(200, {"error": str(e)})
                return self._json(200, out)
            with lock:
                if self.path == "/api/say" and data.get("text", "").strip():
                    r = box.session.say(data["text"])
                elif self.path == "/api/answer":
                    r = box.session.answer(data.get("uid", ""), data.get("text", ""))
                elif self.path == "/api/accept":
                    r = box.session.accept(data.get("uid", ""), data.get("pick"))
                elif self.path == "/api/move":
                    r = box.session.move(data.get("uid", ""), data.get("pick"))
                elif self.path == "/api/refuse":
                    r = box.session.refuse(data.get("uid", ""))
                elif self.path == "/api/cancel":
                    r = box.session.cancel(data.get("uid", ""))
                elif self.path == "/api/distribute":
                    box.session.distribute()
                    start_quiet(box.session.store, box.session.year, box.session.month)
                    from .session import Reply
                    r = Reply("ask", None, "表を配った後の扱いにしました")
                elif self.path == "/api/undo":
                    r = box.session.undo()
                elif self.path == "/api/cleanup":
                    return self._json(200, box.session.cleanup_apply(data.get("archive") or [], data.get("keep") or []))
                elif self.path == "/api/restore":
                    r = box.session.restore(data.get("rule_id", ""))
                elif self.path == "/api/wider":
                    r = box.session.wider(data.get("uid", ""))
                elif self.path == "/api/choose_name":
                    r = box.session.choose_name(data.get("uid", ""), data.get("wrong", ""), data.get("right", ""))
                elif self.path == "/api/retry":
                    r = box.session.retry(data.get("uid", ""))
                else:
                    return self._json(400, {"error": "知らない操作です"})
                st = box.session.state()
            st["has_key"] = has_key
            self._json(200, {"reply": r.to_dict(), "state": st})

    H.box = box
    return H


def serve(session: Session, *, port: int = 8765, has_key: bool = False, voice=None,
          open_browser: bool = False, lock_path: Path | None = None) -> None:
    session.open()
    handler = make_handler(session, threading.Lock(), has_key, voice)
    httpd = None
    for p in range(port, port + 10):   # 使われていたら次の番号へ（別のアプリと重なっても止まらない）
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"{port}〜{port + 9} 番がすべて使われています。--port で別の番号を指定してください")
    url = f"http://127.0.0.1:{port}/"
    lock = lock_path or _take_lock(session.store.root, url)
    if lock is None:
        httpd.server_close()
        return
    lock.write_text(f"{os.getpid()}\n{url}", encoding="utf-8")   # 実際に開いた番号を書く
    from .nextflow import NextFlow
    box = handler.box
    httpd.flow = lambda: NextFlow(box.session.store, box.session.year, box.session.month)
    print(f"開きました {url} （止める時は Ctrl+C）", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        release_lock(lock)


def lock_file(data: Path) -> Path:
    """置き場の印。置き場の外（同じ親フォルダ）に置く（--reset で置き場を消しても印は残る）。"""
    data = Path(data)
    return data.parent / f".{data.name}.open.lock"


def release_lock(lock: Path) -> None:
    """自分の印だけ消す（後から開いた別のサーバーの印を消さない）。"""
    try:
        if lock.read_text(encoding="utf-8").split("\n", 1)[0] == str(os.getpid()):
            lock.unlink()
    except (OSError, ValueError):
        pass


def _take_lock(root: Path, url: str) -> Path | None:
    """同じ置き場を2つのサーバーで開かない（後から保存した方が前の書き込みを消すため）。
    すでに開いていれば、その URL を出して None を返す。前のサーバーが落ちて印だけ残った時は引き継ぐ。"""
    lock = lock_file(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid, old_url = lock.read_text(encoding="utf-8").split("\n", 1)
            alive = True
            try:
                os.kill(int(pid), 0)
            except PermissionError:
                pass          # ほかの利用者の動いているプロセス
            if alive:
                print(f"この置き場はもう開いています。{old_url.strip()} を開いてください", flush=True)
                return None
        except (ValueError, ProcessLookupError):
            pass
    lock.write_text(f"{os.getpid()}\n{url}", encoding="utf-8")
    return lock


def demo_session(data_dir: Path, *, reset: bool = False, reader=None, builder=None) -> tuple[Session, bool]:
    from .store import Store
    seed = Path(__file__).resolve().parents[2] / "data" / "hidamari"
    store = Store.from_seed(seed, data_dir, reset=reset)
    load_dotenv(Path.cwd() / ".env")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if reader is None:
        reader = HaikuReader() if has_key else NoKeyReader()
    if builder is None:
        builder = SonnetBuilder() if has_key else None
    return Session(store, 2026, 11, reader, builder=builder), has_key
