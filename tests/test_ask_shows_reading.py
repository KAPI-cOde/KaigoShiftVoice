"""「毎月ですか、今月だけですか」の問いに、Claude が何と読んだかを先に出す。前は答えた後に初めてルールの名前が出て、
読み違いに気づけないまま期間を選んでいた（チャットの認知的負荷の測り 2026-11-08、重さ8）。"""
from pathlib import Path

from test_even_out import EVEN, SCENE

HTML = Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html"


def test_問いの返事は読んだルールの名前を持ち_答えた後の名前と同じ(sess):
    sess.open()
    for text, ans in SCENE + [(EVEN, "毎月")]:
        r = sess.say(text)
        assert r.kind == "ask_period" and r.summary, (text, r.summary)
        done = sess.answer(r.uid, ans)
        assert done.kind == "applied" and done.message.startswith(r.summary), (r.summary, done.message)


def test_画面は問いの前に読んだルールの名前を出す():
    body = HTML.read_text(encoding="utf-8").split('if (r.kind === "ask_period") {', 1)[1].split("\n  }", 1)[0]
    head, _ = body.split('el("div", "q", "毎月ですか、今月だけですか")', 1)
    assert "r.summary" in head and "と読みました" in head
