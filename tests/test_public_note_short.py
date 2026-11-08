"""公開版の画面の案内は1行だけにし、長い注意と声の説明は出さない。手元版の例の一言はそのまま。"""
from pathlib import Path

import shiftvoice

HTML = (Path(shiftvoice.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_公開版の案内は1行で_長い注意と声の説明を出さない():
    body = HTML[HTML.index("function setupPublic(pub)"):]
    body = body[:body.index("\n}\n")]
    assert "日まで）。声はブラウザの音声認識で文字にします。実在の人の名前は入れないでください。" in body
    assert "見つけきれないこともあります" not in HTML
    assert "公開版の声は、お使いのブラウザの音声認識で文字にします" not in HTML


def test_例の一言は公開版だけ1つにし_手元版は今のまま():
    assert '<div class="hint" id="hint">例「岡田さん、15と16は法事で休み」' in HTML
    hint = HTML[HTML.index("function renderHint()"):]
    hint = hint[:hint.index("\n}\n")]
    assert 'S.public) ? "例「遅番の次の日に早番はきついから、やめてあげて」"' in hint
