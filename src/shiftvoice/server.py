"""手元のサーバー（localhost だけ）。画面は static/index.html、裏側は session.Session。

API（すべて JSON）
- GET  /api/state                     表・札・職員・未反映の発言
- POST /api/say     {"text"}          一言
- POST /api/answer  {"uid", "text"}   「毎月ですか、今月だけですか」への返事（"rule"/"once" か話し言葉）
- POST /api/retry   {"uid"}           送り直す
鍵は環境変数 ANTHROPIC_API_KEY（.env からも読む）。画面にも記録にも出さない。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .reader import HaikuReader, ReadError
from .session import Session

STATIC = Path(__file__).resolve().parent / "static"


class NoKeyReader:
    """鍵が無い時。読み取りは呼べないと返す（表も帳面も変えない）。"""
    sent: list = []

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


def make_handler(session: Session, lock: threading.Lock, has_key: bool):
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
            """手元からだけ受ける。Host と Origin が 127.0.0.1 か localhost（DNS の付け替えと、ほかのサイトからの送信を止める）。"""
            port = self.server.server_address[1]
            ok = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in ok:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://{h}" for h in ok}

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
            elif self.path == "/api/state":
                with lock:
                    st = session.state()
                st["has_key"] = has_key
                self._json(200, st)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._local() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(403, {"error": "手元の画面からだけ送れます"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "JSON が読めません"})
            with lock:
                if self.path == "/api/say" and data.get("text", "").strip():
                    r = session.say(data["text"])
                elif self.path == "/api/answer":
                    r = session.answer(data.get("uid", ""), data.get("text", ""))
                elif self.path == "/api/retry":
                    r = session.retry(data.get("uid", ""))
                else:
                    return self._json(400, {"error": "知らない操作です"})
                st = session.state()
            st["has_key"] = has_key
            self._json(200, {"reply": r.to_dict(), "state": st})

    return H


def serve(session: Session, *, port: int = 8765, has_key: bool = False) -> None:
    session.open()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session, threading.Lock(), has_key))
    print(f"開きました http://127.0.0.1:{port}/ （止める時は Ctrl+C）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def demo_session(data_dir: Path, *, reset: bool = False, reader=None) -> tuple[Session, bool]:
    from .store import Store
    seed = Path(__file__).resolve().parents[2] / "data" / "hidamari"
    store = Store.from_seed(seed, data_dir, reset=reset)
    load_dotenv(Path.cwd() / ".env")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if reader is None:
        reader = HaikuReader() if has_key else NoKeyReader()
    return Session(store, 2026, 11, reader), has_key
