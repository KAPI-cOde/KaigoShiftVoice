"""KaigoShiftVoice。話した言葉で直していく介護の勤務表（作り話の施設ひだまりの郷）。"""
from __future__ import annotations

import argparse
import functools
import builtins

print = functools.partial(builtins.print, flush=True)  # 案内はすぐ出す（溜め込まない）
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="shiftvoice")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="ひだまりの郷の11月を手元のブラウザで開く")
    d.add_argument("--port", type=int, default=8765)
    d.add_argument("--data", default="data/runtime/hidamari", help="動かす用の置き場（初めはリポジトリのデータを写す）")
    d.add_argument("--reset", action="store_true", help="置き場を初めの状態に戻す")
    d.add_argument("--voice", action="store_true", help="声の入口を使う（uv run --extra voice。初回は約2.4GB を取得）")
    d.add_argument("--no-open", action="store_true", help="ブラウザを自動で開かない")
    st = sub.add_parser("state", help="撮り直し用に、動かす用の置き場を名前を付けて保存する・戻す（サーバーを止めてから）")
    st.add_argument("action", choices=["save", "load", "list"])
    st.add_argument("name", nargs="?", help="例 start（11月1日の操作の前）・end（11月末）")
    st.add_argument("--data", default="data/runtime/hidamari")
    args = ap.parse_args()
    if args.cmd == "state":
        return _state(args)
    if args.cmd == "demo":
        from .server import _take_lock, demo_session, release_lock, serve
        lock = _take_lock(Path(args.data), f"http://127.0.0.1:{args.port}/")   # 消す・作る・立て直す前に取る
        if lock is None:
            return
        try:
            _demo(args, lock)
        finally:
            release_lock(lock)
        return
    raise SystemExit(f"知らない命令 {args.cmd}")


def _demo(args, lock) -> None:
    from .server import demo_session, serve
    _ask_key()
    _backup(Path(args.data))
    session, has_key = demo_session(Path(args.data), reset=args.reset)
    if not has_key:
        print("ANTHROPIC_API_KEY がありません。表は見られますが、一言の読み取りは動きません（README の鍵の入れ方）")
    voice = None
    if args.voice:
        from . import voice as v
        if v.missing_files():
            print(f"声のモデルを取得します（約2.4GB）。置き場 {v.model_dir()}")
            v.download()
        print("声のモデルを読み込んでいます（10秒ほど）")
        voice = v.Voice([s.name for s in session.store.staff()])
    print("はじめて開く時は、11月の表を作るのに10秒ほどかかります")
    serve(session, port=args.port, has_key=has_key, voice=voice, open_browser=not args.no_open, lock_path=lock)


def _state(args) -> None:
    """置き場をまるごと写す。途中で止まっても元の置き場が壊れないよう、写し終えてから置き換える。"""
    import re
    import shutil
    data = Path(args.data)
    base = data.parent / "states"
    if args.action == "list":
        for d in sorted(base.glob("*")) if base.exists() else []:
            print(d.name)
        return
    if not args.name or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.name):
        raise SystemExit("名前は英数字で付けてください（例 start・end）")
    from .server import lock_file
    lock = lock_file(data)
    if args.action == "load" and lock.exists():
        import os
        try:
            os.kill(int(lock.read_text(encoding="utf-8").split("\n", 1)[0]), 0)
            raise SystemExit("サーバーが開いています。止めて（Ctrl+C）から戻してください")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    src, dst = (data, base / args.name) if args.action == "save" else (base / args.name, data)
    if not src.exists():
        raise SystemExit(f"{src} がありません")
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns(".open.lock"))
    old = dst.with_name(dst.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dst.exists():
        dst.rename(old)
    tmp.rename(dst)
    shutil.rmtree(old, ignore_errors=True)
    print(("保存しました " if args.action == "save" else "戻しました ") + args.name)


def _ask_key() -> None:
    """鍵が無く、手元のターミナルから起動した時だけ聞く。入力は画面に出さず、このフォルダの .env（本人だけ読める）に書く。"""
    import getpass
    import os
    import sys
    from .server import load_dotenv
    env = Path.cwd() / ".env"
    load_dotenv(env)
    if os.environ.get("ANTHROPIC_API_KEY") or not sys.stdin.isatty():
        return
    print("一言の読み取りに、あなたの Claude の API の鍵を使います（Enter だけなら、表を見るだけで始めます）")
    try:
        k = getpass.getpass("ANTHROPIC_API_KEY（入力は表示されません）: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not k:
        return
    os.environ["ANTHROPIC_API_KEY"] = k
    ans = input("次からも使えるよう、このフォルダの .env に保存しますか（Git には入りません）[Y/n] ").strip().lower()
    if ans in ("", "y", "yes", "はい"):
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"ANTHROPIC_API_KEY={k}\n")
        print("保存しました（.env）")


def _backup(data: Path, keep: int = 5) -> None:
    """起動するたびに、動かす用の置き場の控えを取る（新しい5つを残す）。戻す時は state load と同じ形で写す。"""
    import datetime as dt
    import shutil
    if not data.exists():
        return
    base = data.parent / "backups"
    base.mkdir(parents=True, exist_ok=True)
    shutil.copytree(data, base / dt.datetime.now().strftime("%Y%m%d-%H%M%S"), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".open.lock"))
    for old in sorted(base.iterdir())[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
