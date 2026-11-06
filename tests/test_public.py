"""公開版（SHIFTVOICE_PUBLIC=1。src/shiftvoice/public.py）。人ごとに置き場が分かれ、残す口だけが開き、
Claude を呼ぶ全部の口が1か所の見張りを通ることを、鍵なしで確かめる（読み手は呼んだ回数を数える偽物）。"""
import datetime as dt
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import REPLIES
from shiftvoice.public import JST, Gate, PublicConfig, Workspaces, make_app, name_like, worst_cost
from shiftvoice.reader import RecordedBuilder, RecordedReader
from shiftvoice.server import make_handler

BUILDS = Path(__file__).resolve().parent / "fixtures" / "builds.json"
SAY = "岡田さん、15と16は法事で休み"          # 録った返事がある一言（職員07さん、15と16は法事で休み）
BUILD_SAY = "毎月、土日は休みの人から待機1人。急な休みの電話はその人にだけ。引き受けた回数は、翌月も覚えておいて"


class Counting:
    """録った返事で答え、呼ばれた回数を数える読み手（本物の Claude の代わり）。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.sent = inner.sent

    def read(self, masked, history=None):
        self.calls += 1
        return self.inner.read(masked, history)

    def build(self, masked):
        self.calls += 1
        return self.inner.build(masked)


@pytest.fixture
def make_server(built_dir, tmp_path):
    servers = []

    def make(now=None, **over):
        cfg = PublicConfig(root=tmp_path / "pub", seed=built_dir, ledger_dir=tmp_path / "ledger", **over)
        readers, builders = [], []

        def rf():
            readers.append(Counting(RecordedReader.from_file(REPLIES)))
            return readers[-1]

        def bf():
            builders.append(Counting(RecordedBuilder.from_file(BUILDS)))
            return builders[-1]

        app = make_app(cfg, reader_factory=rf, builder_factory=bf, has_key=True, gate=Gate(cfg, now=now))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, threading.Lock(), True, None, public=app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        calls = lambda: sum(r.calls for r in readers) + sum(b.calls for b in builders)  # noqa: E731
        return base, app, calls

    yield make
    for h in servers:
        h.shutdown()
        h.server_close()


def req(base, path, method="GET", body=None, cookie=None, headers=None, timeout=10):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = f"sv_id={cookie}"
    if body is not None:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, method=method, headers=h,
                               data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as res:
            raw, code, hd = res.read(), res.status, res.headers
    except urllib.error.HTTPError as e:
        raw, code, hd = e.read(), e.code, e.headers
    ck = None
    for v in hd.get_all("Set-Cookie") or []:
        if v.startswith("sv_id="):
            ck = v.split(";", 1)[0].split("=", 1)[1]
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw.decode("utf-8", "replace")
    return code, data, ck


def new_user(base):
    code, st, ck = req(base, "/api/state")
    assert code == 200 and ck and "public" in st
    return ck, st


def test_2人の置き場は分かれ_片方の直しがもう片方に出ない(make_server):
    base, app, calls = make_server()
    a, st_a = new_user(base)
    b, st_b = new_user(base)
    assert a != b
    code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
    assert code == 200 and res["reply"]["kind"] != "error", res
    assert calls() == 1
    _, a2, _ = req(base, "/api/state", cookie=a)
    _, b2, _ = req(base, "/api/state", cookie=b)
    assert a2["table"] != st_a["table"]          # A の表は直った
    assert b2["table"] == st_b["table"]          # B の表は変わらない
    assert [p["text"] for p in b2.get("pending", [])] == []
    roots = {Path(ws.root) for ws in app.spaces.items.values()}
    assert len({r.parent for r in roots}) == 2   # 親フォルダまで別（施設の一覧にほかの人の置き場が出ない）
    for tok, ws in app.spaces.items.items():
        assert tok not in str(ws.root)           # フォルダの名前にクッキーの ID を使わない


def test_発行していない_ID_や_パスの形の_ID_では_ほかの人の置き場に届かない(make_server):
    base, app, calls = make_server()
    a, _ = new_user(base)
    req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
    before = set(app.spaces.items)
    for fake in ["../../" + a[:30], "A" * 43, a[:-1] + ("B" if a[-1] != "B" else "C"), a + "x"]:
        code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=fake)
        assert code == 401
        code, st, ck = req(base, "/api/state", cookie=fake)
        assert ck and ck != a and not st.get("pending")   # 新しい人として、まっさらな置き場が出る
    assert before <= set(app.spaces.items)
    assert calls() == 1
    users = Path(app.cfg.root) / "users"
    assert all(p.parent == users for p in users.iterdir())   # 置き場の外にフォルダを作らない


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/facilities"), ("GET", "/api/setup/sample"),
    ("POST", "/api/setup/preview"), ("POST", "/api/setup/create"), ("POST", "/api/facilities/open"),
    ("POST", "/api/voice"), ("POST", "/api/unknown"),
])
def test_取り込み_施設の一覧と切り替え_声の口は_403(make_server, method, path):
    base, app, calls = make_server()
    a, _ = new_user(base)
    code, res, _ = req(base, path, method, {} if method == "POST" else None, cookie=a)
    assert code == 403, (path, res)


def test_人ごとの回数の上限を超えると_Claude_を呼ばない(make_server):
    base, app, calls = make_server(user_reads_per_hour=2)
    a, _ = new_user(base)
    for _ in range(2):
        req(base, "/api/say", "POST", {"text": "職員99さん、だめな一言"}, cookie=a)   # 名簿に無い→送らない
    for _ in range(2):
        req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
    assert calls() == 2
    code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
    assert calls() == 2
    assert res["reply"]["kind"] == "error" and "試せる回数を超えました" in res["reply"]["message"]
    # 送り直しも同じ見張りを通る
    code, res, _ = req(base, "/api/retry", "POST", {"text": "", "uid": res["reply"]["uid"]}, cookie=a)
    assert calls() == 2
    # ほかの人はまだ試せる
    b, _ = new_user(base)
    req(base, "/api/say", "POST", {"text": SAY}, cookie=b)
    assert calls() == 3


def test_全体の毎分の上限と_費用の上限でも呼ばない(make_server):
    base, app, calls = make_server(global_calls_per_minute=1)
    a, _ = new_user(base)
    b, _ = new_user(base)
    req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
    code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=b)
    assert calls() == 1 and "混み合っています" in res["reply"]["message"]

    one = worst_cost("read", "職員07さん、15と16は法事で休み")
    base2, app2, calls2 = make_server(boot_budget_usd=one * 1.5)
    c, _ = new_user(base2)
    req(base2, "/api/say", "POST", {"text": SAY}, cookie=c)
    code, res, _ = req(base2, "/api/undo", "POST", {}, cookie=c)
    code, res, _ = req(base2, "/api/say", "POST", {"text": SAY}, cookie=c)
    assert calls2() == 1 and "使い切りました" in res["reply"]["message"]
    # 帳簿には、出力と送り直しまで含めた最大の費用を、呼ぶ前に書いてある（帳簿は2つのサーバーで同じ置き場。
    # 前のサーバーの1回と、このサーバーの1回）
    spent = json.loads((app2.cfg.ledger_dir / "spend.json").read_text())["spent_usd"]
    assert spent == pytest.approx(2 * one, rel=1e-3)


def test_帳簿が読めない時と_通算の上限を超えた時は呼ばない(make_server, tmp_path):
    led = tmp_path / "ledger"
    led.mkdir()
    (led / "spend.json").write_text("壊れた帳簿", encoding="utf-8")
    base, app, calls = make_server()
    a, _ = new_user(base)
    code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
    assert calls() == 0 and "帳簿が読めない" in res["reply"]["message"]

    (led / "spend.json").write_text(json.dumps({"spent_usd": 79.9999}), encoding="utf-8")
    base2, app2, calls2 = make_server()   # 再起動しても、残った帳簿の通算から数える
    b, _ = new_user(base2)
    code, res, _ = req(base2, "/api/say", "POST", {"text": SAY}, cookie=b)
    assert calls2() == 0 and "使い切りました" in res["reply"]["message"]


@pytest.mark.parametrize("text", [
    "山川太郎は通院で休み", "工藤は15日休み", "岡田さん、はなこさんと15日交代", "岡田さん、竹内と交代",
    "岡田さん、090-1234-5678に電話して",
])
def test_名簿に無い名前らしい語や電話番号を含む一言は送らない(make_server, text):
    base, app, calls = make_server()
    a, _ = new_user(base)
    code, res, _ = req(base, "/api/say", "POST", {"text": text}, cookie=a)
    assert calls() == 0
    assert res["reply"]["kind"] in ("error", "ask"), res
    assert res["reply"].get("sent") is None
    if res["reply"]["kind"] == "error":   # 送り直しても同じなので［送り直す］を出さない
        assert (res["reply"].get("extra") or {}).get("no_retry") is True


def test_名前らしい語の見分けは_録った一言をどれも止めない():
    keys = list(json.loads(REPLIES.read_text(encoding="utf-8"))) + list(json.loads(BUILDS.read_text(encoding="utf-8")))
    assert [k for k in keys if name_like(k)] == []
    assert name_like("職員03さん、腰痛で夜勤なし") is None
    assert name_like("みなさん、たくさん休んで") is None


def test_翌月のルールの変え方と組み立ても同じ見張りを通る(make_server):
    base, app, calls = make_server(user_reads_per_hour=0, user_builds_per_hour=0)
    a, _ = new_user(base)
    code, rv, _ = req(base, "/api/next/review", cookie=a)
    rid = next((c["rule_id"] for c in rv.get("cards", []) if c.get("rule_id")), "x")
    code, res, _ = req(base, "/api/next/change", "POST", {"rule_id": rid, "text": SAY}, cookie=a)
    assert calls() == 0 and "試せる回数を超えました" in res.get("error", ""), res

    base2, app2, calls2 = make_server(user_builds_per_hour=0)
    b, _ = new_user(base2)
    code, res, _ = req(base2, "/api/say", "POST", {"text": BUILD_SAY}, cookie=b)
    assert calls2() == 1   # 読み取りは呼んだが、組み立て（Sonnet）は呼ばない
    assert "試せる回数を超えました" in res["reply"]["message"]


def test_終わりの日を過ぎたら_全部の口で呼ばない(make_server):
    after = lambda: dt.datetime(2026, 11, 14, 0, 0, 1, tzinfo=JST)  # noqa: E731
    base, app, calls = make_server(now=after)
    a, st = new_user(base)
    assert st["public"]["closed"] is True and "11月13日" in st["public"]["closed_message"]
    code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
    assert calls() == 0 and "終わりました" in res["reply"]["message"]
    code, res, _ = req(base, "/api/retry", "POST", {"uid": res["reply"]["uid"]}, cookie=a)
    assert calls() == 0
    last = lambda: dt.datetime(2026, 11, 13, 23, 59, tzinfo=JST)  # noqa: E731
    assert not Gate(app.cfg, now=last).closed()


def test_長い一言と大きすぎる本文は受けない(make_server):
    base, app, calls = make_server()
    a, _ = new_user(base)
    code, res, _ = req(base, "/api/say", "POST", {"text": "岡田さん、" + "あ" * 300}, cookie=a)
    assert calls() == 0 and "200字まで" in res["error"]
    code, res, _ = req(base, "/api/say", "POST", {"text": "x" * 20000}, cookie=a)
    assert code == 413 and calls() == 0


def test_ほかのサイトからの送信は断る(make_server):
    base, app, calls = make_server()
    a, _ = new_user(base)
    code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=a, headers={"Origin": "https://evil.example"})
    assert code == 403 and calls() == 0


def test_人数が上限なら_使っている人を追い出さずに新しい人を断る(make_server):
    base, app, calls = make_server(max_users=1)
    a, _ = new_user(base)
    code, res, ck = req(base, "/api/state")
    assert code == 503 and ck is None
    code, st, _ = req(base, "/api/state", cookie=a)
    assert code == 200


def test_処理中の置き場は片付けで消さない(tmp_path, built_dir):
    cfg = PublicConfig(root=tmp_path / "pub", seed=built_dir, idle_seconds=-1)
    sp = Workspaces(cfg, lambda store, user: type("S", (), {"open": lambda self: None})())
    ws = sp.create()               # 作った直後は busy=1（処理中）
    sp._sweep(10 ** 9)
    assert ws.token in sp.items and ws.root.exists()
    sp.release(ws)
    sp._sweep(10 ** 9)
    assert ws.token not in sp.items and not ws.root.exists()
    assert sp.acquire(ws.token) is None   # 消した ID は使い直さない


def test_鍵は返事にも端末にも出ない(make_server, monkeypatch, capsys):
    fake = "sk-" + "ant-" + "TESTKEY" + "0123456789abcdefghij"
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake)
    base, app, calls = make_server()
    a, st = new_user(base)
    bodies = [json.dumps(st, ensure_ascii=False)]
    for path, method, body in [("/api/say", "POST", {"text": SAY}), ("/api/state", "GET", None),
                               ("/api/next/review", "GET", None), ("/api/table.tsv", "GET", None),
                               ("/api/facilities", "GET", None), ("/", "GET", None)]:
        code, res, _ = req(base, path, method, body, cookie=a)
        bodies.append(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False))
    out = capsys.readouterr()
    assert all(fake not in b for b in bodies)
    assert fake not in out.out and fake not in out.err


def test_画面を読み込むだけでは置き場を作らない(make_server):
    base, app, calls = make_server()
    code, body, ck = req(base, "/")
    assert code == 200 and ck is None and "公開版" in body and not app.spaces.items


def test_名前で止めた一言は_開き直しても送り直しを出さない(make_server):
    base, app, calls = make_server()
    a, _ = new_user(base)
    req(base, "/api/say", "POST", {"text": "山川太郎は20日休み"}, cookie=a)
    _, st, _ = req(base, "/api/state", cookie=a)
    assert not [p for p in st.get("pending", []) if p.get("status") == "failed"]



# ---- 実装への敵対レビュー（2026-11-06 Codex Astra）の6件と、線の下の2件

def test_置き場が満ちても_しばらく使われていない置き場を片付けて新しい人を受ける(make_server):
    base, app, calls = make_server(max_users=2, evict_idle_seconds=0)
    a, _ = new_user(base)
    b, _ = new_user(base)
    code, st, c = req(base, "/api/state")      # クッキーを捨てて取り直す人が枠を埋めても締め出されない
    assert code == 200 and c
    assert len(app.spaces.items) == 2


def test_新しい置き場を作る速さには上限がある(make_server):
    base, app, calls = make_server(new_users_per_minute=2)
    new_user(base)
    new_user(base)
    code, res, ck = req(base, "/api/state")
    assert code == 503 and ck is None


def test_同じ人の重なった送信は_全体の枠を取らずにすぐ断る(make_server):
    base, app, calls = make_server()
    a, _ = new_user(base)
    ws = app.spaces.items[a]
    got = threading.Event()
    done = threading.Event()

    def hold():   # 同じ人の重い処理が動いている間
        with ws.lock:
            got.set()
            done.wait(15)
    t = threading.Thread(target=hold)
    t.start()
    got.wait(5)
    try:
        code, res, _ = req(base, "/api/say", "POST", {"text": SAY}, cookie=a)
        assert code == 429 and "前の操作" in res["error"]
        assert app.slots._value == app.cfg.workers   # 全体の枠は1つも使っていない
    finally:
        done.set()
        t.join()
    assert calls() == 0


@pytest.mark.parametrize("text", [
    "岡田さん、John Smithは糖尿病で15日休み", "岡田さん、はるかはうつ病で15日休み", "岡田さん、連絡先は090 1234 5678です",
    "岡田さん、はるかは15日休み", "岡田さん、がんの治療で15日休み", "岡田さん、手術で15と16は休み",
])
def test_敬称の無い名前_空白の電話番号_病名も送らない(make_server, text):
    base, app, calls = make_server()
    a, _ = new_user(base)
    code, res, _ = req(base, "/api/say", "POST", {"text": text}, cookie=a)
    assert calls() == 0, text
    assert res["reply"].get("sent") is None


def test_公開版の画面は_埋め込みの中なら状態を取りに行かずに直接のページへ案内する(make_server):
    base, app, calls = make_server()
    code, body, _ = req(base, "/")
    assert 'data-public="1"' in body
    html = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html").read_text(encoding="utf-8")
    i_frame = html.index("function embeddedPublic()")
    i_state = html.index('if (embeddedPublic()) {')
    assert i_frame < i_state < html.index('fetch("/api/state").then(function (r) { return r.json(); }).then(function (st) {\n  S = st;\n  if (st.public)')
    assert not app.spaces.items


def test_基準の日は11月末で_画面の引き受けと12月の返す案が同じ数(make_server):
    base, app, calls = make_server(now=lambda: dt.datetime(2026, 11, 6, 9, 0, tzinfo=JST))
    a, st = new_user(base)
    assert app.spaces.items[a].session.base_date == "2026-11-30"
    shown = {k: v for k, v in st["accepted"].items() if v}
    req(base, "/api/next/review", cookie=a)
    code, out, _ = req(base, "/api/next/build", "POST", {}, cookie=a, timeout=180)   # 2つの案を作る計算
    counts = {k: v for k, v in out["counts"].items() if v}
    assert counts == {"田村": 2, "菊池": 1}
    assert shown == counts
    assert "田村さん" in json.dumps(out, ensure_ascii=False)


def test_読み取りを送った後に組み立てで止めた時は_送った文を残す(make_server):
    base, app, calls = make_server(user_builds_per_hour=0)
    b, _ = new_user(base)
    code, res, _ = req(base, "/api/say", "POST", {"text": BUILD_SAY}, cookie=b)
    assert calls() == 1
    assert res["reply"].get("sent")      # Haiku へは送ったので「送っていません」と出さない


def test_時間のたった置き場は_使いに来ても戻らない(tmp_path, built_dir):
    cfg = PublicConfig(root=tmp_path / "pub", seed=built_dir, idle_seconds=0)
    sp = Workspaces(cfg, lambda store, user: type("S", (), {"open": lambda self: None})())
    ws = sp.create()
    sp.release(ws)
    import time as _t
    _t.sleep(0.01)
    assert sp.acquire(ws.token) is None and not ws.root.exists()


def test_表を作る前の希望休は公開版でも入れられ_見張りを通り_希の表の貼り付けは断る(make_server):
    base, app, calls = make_server()
    code, res, cookie = req(base, "/api/state")
    assert code == 200
    code, res, _ = req(base, "/api/next/wish_paste", "POST", {"text": "x"}, cookie=cookie)
    assert code == 403
    code, res, _ = req(base, "/api/next/wish", "POST", {"text": "山川太郎は15日休み"}, cookie=cookie)
    assert code != 403 and calls() == 0     # 名前らしい語は送らない


def test_公開版でも話して勤務を足せて_見張りを通り_ひとつ戻せて_ほかの人に出ない(make_server):
    base, app, calls = make_server()
    a, _ = new_user(base)
    b, _ = new_user(base)
    code, res, _ = req(base, "/api/say", "POST", {"text": "8時から17時の日8を足して"}, cookie=a)
    assert code == 200 and res["reply"]["kind"] == "applied", res
    assert calls() == 0                                  # 手元で読むので Claude を呼ばない
    _, st, _ = req(base, "/api/state", cookie=a)
    assert ["日8", "日8"] in st["shift_names"]
    _, other, _ = req(base, "/api/state", cookie=b)
    assert ["日8", "日8"] not in other["shift_names"]    # 足した人の置き場だけ
    code, res, _ = req(base, "/api/say", "POST", {"text": "10日の小島さん、日8に"}, cookie=a)
    assert code == 200 and res["reply"]["kind"] == "applied", res
    assert calls() == 1                                  # 続きの一言は見張りを通って送る
    assert res["state"]["table"]["小島"][9] == "日8"
    for _ in range(2):
        code, res, _ = req(base, "/api/undo", "POST", {}, cookie=a)
        assert code == 200, res
    _, st, _ = req(base, "/api/state", cookie=a)
    assert ["日8", "日8"] not in st["shift_names"] and st["table"]["小島"][9] != "日8"
