"""shift-voice。話した言葉で直していく介護の勤務表（作り話の施設ひだまりの郷）。"""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="shiftvoice")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="ひだまりの郷の11月を手元のブラウザで開く")
    d.add_argument("--port", type=int, default=8765)
    d.add_argument("--data", default="data/runtime/hidamari", help="動かす用の置き場（初めはリポジトリのデータを写す）")
    d.add_argument("--reset", action="store_true", help="置き場を初めの状態に戻す")
    d.add_argument("--voice", action="store_true", help="声の入口を使う（uv run --extra voice。初回は約2.4GB を取得）")
    st = sub.add_parser("state", help="撮り直し用に、動かす用の置き場を名前を付けて保存する・戻す（サーバーを止めてから）")
    st.add_argument("action", choices=["save", "load", "list"])
    st.add_argument("name", nargs="?", help="例 start（11月1日の操作の前）・end（11月末）")
    st.add_argument("--data", default="data/runtime/hidamari")
    args = ap.parse_args()
    if args.cmd == "state":
        return _state(args)
    if args.cmd == "demo":
        from .server import demo_session, serve
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
        print("はじめて開く時は、11月の表を作るのに20秒ほどかかります")
        serve(session, port=args.port, has_key=has_key, voice=voice)


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
    src, dst = (data, base / args.name) if args.action == "save" else (base / args.name, data)
    if not src.exists():
        raise SystemExit(f"{src} がありません")
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    old = dst.with_name(dst.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dst.exists():
        dst.rename(old)
    tmp.rename(dst)
    shutil.rmtree(old, ignore_errors=True)
    print(("保存しました " if args.action == "save" else "戻しました ") + args.name)
