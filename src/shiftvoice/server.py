"""手元のサーバー（localhost だけ）。画面は static/index.html、裏側は session.Session。

API（すべて JSON）
- GET  /api/state                     表・札・職員・未反映の発言
- GET  /api/rule?id=                  ルールを押した時の中身（数で言う1行・今の表の様子・上限や下限に届いているマス）
- GET  /api/table.tsv                開いている月の表をタブ区切りで（Excel に貼り戻す・印刷の元）
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
    # 記号表があれば、別名の読み方と時刻が合わない記号（明けの側の「夜」）も知らない記号として聞く
    pasted = parse_paste(str(data.get("text") or ""), str(data.get("symbols") or ""))
    answers = data.get("answers")
    prop = infer(pasted, year, month, answers if isinstance(answers, dict) else None)
    plan = _symbol_plan(data, pasted)
    if plan:
        # 記号表から読んだおすすめ（画面が先に入れる）と、勤務の時刻の初めの値。ほかの見つけ方のおすすめより先にする
        prop.suggestions.update(plan.suggestions)
        for sym in plan.suggestions:
            prop.suggestion_why.pop(sym, None)   # 理由の一言も、おすすめと同じ見つけ方の物にする
        prop.suggestion_why.update(plan.why)
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
        "suggestion_why": dict(prop.suggestion_why),   # おすすめの理由の一言（画面はおすすめのままの時だけ出す）
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
    Codespaces の転送は、多くの場合 Host と画面の Origin を localhost に書き換えて届けるので、今までの確かめのまま通る。
    転送のしかたによって、この名前のまま届く時のために、この名前1つも受ける。ほかの時は None（今までどおり 127.0.0.1 と
    localhost だけ）。受けるのは、この Codespace のこの番号の名前1つだけ（Codespaces の転送は、既定で持ち主だけが開ける）。"""
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
    """今開いている施設の入れ物。施設を切り替える時は lock の中で session を差し替える。
    声の入口（voice）は施設に関わらず1つを使い回し、文字にするたびに、開いている施設の名簿の名前を渡す。"""

    def __init__(self, session: Session, voice=None):
        self.session = session
        self.voice = voice


def make_handler(session: Session | None, lock: threading.Lock, has_key: bool, voice=None, *, public=None):
    """public（public.PublicApp）を渡した時だけ公開版の動き（人ごとの置き場・残す口だけ・見張り）。"""
    box = Box(session, voice)

    class H(BaseHTTPRequestHandler):
        lock = None   # 下で入れる（公開版では、リクエストごとにその人の置き場とロックに差し替える）

        def flow(self):
            return self.server.flow()
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
            GitHub Codespaces の中で動く時だけ、その Codespace の転送先の名前のまま届いた時のために、その名前1つも受ける
            （codespaces_host。多くの場合、転送は localhost に書き換えて届ける）。"""
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
            if self.box.voice is None:
                return self._json(200, {"error": "声の入口が使えません。文字で入れてください"})
            import numpy as np
            n = int(self.headers.get("Content-Length") or 0)
            samples = np.frombuffer(self.rfile.read(n), dtype="<f4")
            # 名前の手がかりは、いま開いている施設の名簿（［自分の施設で始める］や切り替えの後も、その施設の名前で聞く）
            names = [s.name for s in self.box.session.store.staff()]
            text, secs = self.box.voice.transcribe(samples, names)
            if not text:
                return self._json(200, {"error": "聞き取れませんでした。もう一度話すか、文字で入れてください",
                                        "voice_seconds": secs})
            if self.headers.get("X-Text-Only") == "1":
                # ［変える（話す）］は文字にするだけ。中身は画面で確かめてから送る
                return self._json(200, {"text": text, "voice_seconds": round(secs, 3)})
            with self.lock:
                uid = self.headers.get("X-Answer-Uid")
                r = self.box.session.answer(uid, text) if uid else self.box.session.say(text)
                st = self.box.session.state()
            st["has_key"] = has_key
            self._json(200, {"text": text, "voice_seconds": round(secs, 3), "reply": r.to_dict(), "state": st})

        def _state_out(self) -> dict:
            st = self.box.session.state()
            st["has_key"] = has_key
            st["has_voice"] = self.box.voice is not None
            return st

        def _swap(self, root: Path, year: int, month: int) -> dict:
            """lock の中で session を差し替える。声の入口はそのまま使う（名前は文字にするたびに、新しい施設の名簿から渡す）。"""
            old = self.box.session
            new = Session(Store(root), year, month, old.reader, now=old.now, who=old.who, builder=old.builder)
            new.open()
            self.box.session = new
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
                    parent = Path(self.box.session.store.root).parent
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
                    with self.lock:
                        st = self._swap(root, ny, nm)
                    message = (f"『{st_name(root)}』はもう作ってあるので、前に作った施設を開きました。"
                               "今回貼った表は使っていません。新しく作る時は別の名前にしてください") if existing else ""
                    return self._json(200, {"ok": True, "existing": existing, "message": message, "facility": {"id": root.name, "name": st_name(root)},
                                            "year": ny, "month": nm, "notes": notes, "diffs": diffs, "short_rest": short_rest,
                                            "from_month": month, "state": st})
                if self.path == "/api/facilities/open":
                    with self.lock:
                        hit = [f for f in list_facilities(self.box.session.store.root) if f["id"] == data.get("id")]
                        if not hit:
                            return self._json(400, {"error": "その施設は見つかりません"})
                        root = Path(self.box.session.store.root).parent / hit[0]["id"]
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

        def _public(self, method: str):
            """公開版の入口。Host と Origin・残す口・本文の大きさ・その人の置き場を確かめてから、手元と同じ道筋へ渡す。"""
            from .public import COOKIE, Full, allowed
            host = self.headers.get("Host") or ""
            origin = self.headers.get("Origin")
            ok_origins = {f"https://{host}", f"http://{host}"}
            if public.cfg.host:   # 転送で Host が書き換わって届いた時のために、設定した名前の Origin も受ける
                ok_origins.add(f"https://{public.cfg.host}")
            if origin is not None and origin not in ok_origins:
                return self._json(403, {"error": "ほかのサイトからは送れません"})
            path = self.path
            if not allowed(method, path):
                code = 404 if method == "GET" and not path.startswith("/api/") else 403
                return self._json(code, {"error": "公開版では使えません。手元で動かす手順は README にあります"})
            data = {}
            if method == "POST":
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._json(403, {"error": "JSON で送ってください"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > public.cfg.max_body:
                    return self._json(413, {"error": "送る文が長すぎます"})
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._json(400, {"error": "JSON が読めません"})
                if not isinstance(data, dict):
                    return self._json(400, {"error": "JSON が読めません"})
                for k in ("text", "wrong", "right", "answer"):
                    v = data.get(k)
                    if v is not None and (not isinstance(v, str) or len(v) > public.cfg.max_text):
                        return self._json(200, {"error": f"一言は{public.cfg.max_text}字までにしてください（表もルールも変えていません）"})
            from http.cookies import SimpleCookie
            token = None
            try:
                c = SimpleCookie(self.headers.get("Cookie") or "")
                token = c[COOKIE].value if COOKIE in c else None
            except Exception:  # noqa: BLE001 壊れたクッキーは無い扱い
                token = None
            ws = public.spaces.acquire(token)
            new_cookie = None
            if ws is None:
                if method == "GET" and path in ("/", "/index.html"):
                    return self._get()   # 画面だけ。置き場は画面が最初に呼ぶ /api/state で作る（読み込むだけの訪問で枠を使わない）
                if method == "GET" and path == "/api/state":
                    try:
                        ws = public.spaces.create()
                    except Full:
                        return self._json(503, {"error": "いま混み合っています。しばらくしてから開いてください"})
                    new_cookie = ws.token
                else:
                    return self._json(401, {"error": "試す場所が見つかりません（時間がたつと消えます）。ページを開き直してください"})
            try:
                if new_cookie:
                    self._cookie = new_cookie
                from .nextflow import NextFlow
                self.box = Box(ws.session)
                self.lock = ws.lock
                self.flow = lambda: NextFlow(ws.session.store, ws.session.year, ws.session.month)
                if method == "GET":
                    return self._get()
                # 同じ人の前の操作が動いている間は、全体の枠を取らずにすぐ断る（待つ間に全体の枠を占めない）
                if not ws.lock.acquire(blocking=False):
                    return self._json(429, {"error": "前の操作が終わるまでお待ちください（表もルールも変えていません）"})
                try:
                    if not public.slots.acquire(timeout=30):
                        return self._json(503, {"error": "いま混み合っています。1分ほどしてから試してください"})
                except BaseException:
                    ws.lock.release()
                    raise
                try:
                    from .reader import LimitError
                    try:
                        public.gate.count(ws.token, "post", public.cfg.user_posts_per_hour)
                        if path == "/api/next/build":
                            public.gate.count(ws.token, "table", public.cfg.user_tables_per_hour)
                    except LimitError as e:
                        return self._json(429, {"error": str(e)})
                    return self._post(data)
                finally:
                    public.slots.release()
                    ws.lock.release()
            finally:
                public.spaces.release(ws)

        def end_headers(self):
            ck = getattr(self, "_cookie", None)
            if ck:
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                self.send_header("Set-Cookie", f"sv_id={ck}; Path=/; HttpOnly; SameSite=Lax; Max-Age=7200{secure}")
                self._cookie = None
            if public is not None:
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def do_GET(self):
            if public is not None:
                return self._public("GET")
            if not self._local():
                return self._json(403, {"error": "手元からだけ開けます"})
            self._get()

        def _get(self):
            if self.path in ("/", "/index.html"):
                body = (STATIC / "index.html").read_bytes()
                if public is not None:   # 公開版の印（埋め込みの中なら、状態を取りに行く前に直接のページへ案内する）
                    body = body.replace(b"<body>", b'<body data-public="1">', 1)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path in ("/api/next/review", "/api/next/review?split=1"):
                with self.lock:
                    self._json(200, self.flow().review(split=self.path.endswith("split=1")))
            elif self.path == "/api/cleanup":
                with self.lock:
                    self._json(200, self.box.session.cleanup())
            elif self.path.startswith("/api/why?"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                try:
                    day = int((q.get("day") or ["0"])[0])
                except ValueError:
                    day = 0
                with self.lock:
                    self._json(200, self.box.session.why((q.get("staff") or [""])[0], day))
            elif self.path.startswith("/api/rule?"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                with self.lock:
                    self._json(200, self.box.session.rule_detail((q.get("id") or [""])[0]))
            elif self.path == "/api/state":
                with self.lock:
                    st = self.box.session.state()
                st["has_key"] = has_key
                st["has_voice"] = self.box.voice is not None
                if public is not None:
                    st["public"] = public.info()
                self._json(200, st)
            elif self.path == "/api/table.tsv":
                with self.lock:
                    settings = self.box.session.store.facility().settings
                    back = settings.get("paste_symbols") or {}
                    merged = settings.get("paste_merged") or {}
                    body = table_tsv(self.box.session.state(), back).encode("utf-8")
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
                with self.lock:
                    self._json(200, {"facilities": list_facilities(self.box.session.store.root)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if public is not None:
                return self._public("POST")
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
            self._post(data)

        def _post(self, data: dict):
            if self.path.startswith("/api/setup/") or self.path.startswith("/api/facilities/"):
                return self._setup(data)
            if self.path.startswith("/api/next/"):
                with self.lock:
                    fl = self.flow()
                    try:
                        if self.path == "/api/next/answer":
                            out = fl.answer(data.get("rule_id", ""), data.get("answer", ""), data.get("new_until"),
                                            bool(data.get("split")))
                        elif self.path == "/api/next/change":
                            out = fl.change(data.get("rule_id", ""), data.get("text", ""), self.box.session.reader,
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
            with self.lock:
                if self.path == "/api/say" and data.get("text", "").strip():
                    r = self.box.session.say(data["text"])
                elif self.path == "/api/answer":
                    r = self.box.session.answer(data.get("uid", ""), data.get("text", ""))
                elif self.path == "/api/accept":
                    r = self.box.session.accept(data.get("uid", ""), data.get("pick"))
                elif self.path == "/api/move":
                    r = self.box.session.move(data.get("uid", ""), data.get("pick"))
                elif self.path == "/api/refuse":
                    r = self.box.session.refuse(data.get("uid", ""))
                elif self.path == "/api/cancel":
                    r = self.box.session.cancel(data.get("uid", ""))
                elif self.path == "/api/distribute":
                    self.box.session.distribute()
                    if public is None:   # 公開版は裏で Claude を呼ぶ読み分けを流さない（public.py）
                        start_quiet(self.box.session.store, self.box.session.year, self.box.session.month)
                    from .session import Reply
                    r = Reply("ask", None, "表を配った後の扱いにしました")
                elif self.path == "/api/undo":
                    r = self.box.session.undo()
                elif self.path == "/api/cleanup":
                    return self._json(200, self.box.session.cleanup_apply(data.get("archive") or [], data.get("keep") or []))
                elif self.path == "/api/restore":
                    r = self.box.session.restore(data.get("rule_id", ""))
                elif self.path == "/api/wider":
                    r = self.box.session.wider(data.get("uid", ""))
                elif self.path == "/api/choose_name":
                    r = self.box.session.choose_name(data.get("uid", ""), data.get("wrong", ""), data.get("right", ""))
                elif self.path == "/api/retry":
                    r = self.box.session.retry(data.get("uid", ""))
                else:
                    return self._json(400, {"error": "知らない操作です"})
                st = self.box.session.state()
            st["has_key"] = has_key
            self._json(200, {"reply": r.to_dict(), "state": st})

    H.box = box
    H.lock = lock
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


def pid_alive(pid: int) -> bool:
    """その番号のプロセスが生きているか。Windows の os.kill(pid, 0) は Ctrl+C を送ってしまうので使わない。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5        # ERROR_ACCESS_DENIED は生きている扱い
        try:
            code = ctypes.c_uint32()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                   # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True           # ほかの利用者の動いているプロセス
    except OSError:
        return False
    return True


def _take_lock(root: Path, url: str) -> Path | None:
    """同じ置き場を2つのサーバーで開かない（後から保存した方が前の書き込みを消すため）。
    すでに開いていれば、その URL を出して None を返す。前のサーバーが落ちて印だけ残った時は引き継ぐ。"""
    lock = lock_file(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid, old_url = lock.read_text(encoding="utf-8").split("\n", 1)
            alive = pid_alive(int(pid))
        except (OSError, ValueError):
            alive = False     # 読めない・壊れている印は引き継ぐ
        if alive:
            print(f"この置き場はもう開いています。{old_url.strip()} を開いてください", flush=True)
            return None
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
