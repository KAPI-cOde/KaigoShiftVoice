"""画面の確かめ用（開発だけ）。録った返事でサーバーを開く。アプリの見本モードではない。

uv run python tools/screen_check.py <置き場のフォルダ> [port]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FIXED_NOW, REPLIES  # noqa: E402
from shiftvoice.reader import RecordedBuilder, RecordedReader  # noqa: E402
from shiftvoice.server import demo_session, serve  # noqa: E402

data = Path(sys.argv[1])
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8766
session, _ = demo_session(data, reader=RecordedReader.from_file(REPLIES),
                          builder=RecordedBuilder.from_file(REPLIES.parent / "builds.json"))
session.base_date = "2026-11-01"
session.now = lambda: FIXED_NOW
serve(session, port=port, has_key=True)
