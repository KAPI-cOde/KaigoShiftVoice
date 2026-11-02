"""画面の確かめ用（開発だけ）。用意した返事でサーバーを開く。アプリの見本モードではない。

uv run python tools/screen_check.py <置き場のフォルダ> [port] [--act3]
--act3 は、1幕・2幕を済ませ、小島さんの残り2回の記録を足した11月末の状態から開く（3幕の画面の確かめ用）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FIXED_NOW, REPLIES  # noqa: E402
from shiftvoice.model import Acceptance  # noqa: E402
from shiftvoice.reader import RecordedBuilder, RecordedReader  # noqa: E402
from shiftvoice.server import demo_session, serve  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
data = Path(args[0])
port = int(args[1]) if len(args) > 1 else 8766
session, _ = demo_session(data, reader=RecordedReader.from_file(REPLIES),
                          builder=RecordedBuilder.from_file(REPLIES.parent / "builds.json"))
session.now = lambda: FIXED_NOW
session.base_date = "2026-11-01"
if "--act3" in sys.argv and not session.store.acceptances() or (
        "--act3" in sys.argv and not any(a.staff == "小島" for a in session.store.acceptances())):
    from test_act2 import CALL, STANDBY
    from test_t1_act1 import ACT1
    session.open()
    for text, ans in ACT1:
        r = session.say(text)
        if r.kind == "ask_period":
            session.answer(r.uid, ans)
    session.say(STANDBY)
    session.distribute()
    session.accept(session.say(CALL).uid)
    st = session.store
    st.save_acceptances(st.acceptances() + [
        Acceptance(id="k2", staff="小島", date="2026-11-14", layer="standby", op_id="op-k2", note="確かめ用"),
        Acceptance(id="k3", staff="小島", date="2026-11-21", layer="standby", op_id="op-k3", note="確かめ用")])
    session.base_date = "2026-11-30"
serve(session, port=port, has_key=True)
