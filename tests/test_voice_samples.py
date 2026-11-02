"""V1・V2 作者が作り話の台本を読んだ声（リポジトリには入れない）で確かめる。
録音の置き場を環境変数 SHIFTVOICE_VOICE_SAMPLES に入れた時だけ動く（1.wav〜3.wav。16kHz・モノラル）。"""
import json
import os
import re
import time
import wave
from pathlib import Path

import pytest

from shiftvoice.names import Masker
from shiftvoice.reader import RecordedReader

from conftest import REPLIES

SAMPLES = os.environ.get("SHIFTVOICE_VOICE_SAMPLES")
pytestmark = pytest.mark.skipif(not SAMPLES, reason="声の録音が無い（SHIFTVOICE_VOICE_SAMPLES）")

EXPECT = {1: ("小島", "29日", "3回"), 2: ("菊池", "5回"), 3: ("青木", "11月1日")}


def _load(i):
    import numpy as np
    with wave.open(str(Path(SAMPLES) / f"{i}.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768


@pytest.fixture(scope="module")
def voice(store):
    pytest.importorskip("sherpa_onnx")
    from shiftvoice import voice as v
    if v.missing_files():
        pytest.skip("声のモデルが無い")
    return v.Voice([s.name for s in store.staff()])


def test_v1_names_match(voice, store):
    masker = Masker(store.staff(), store.facility())
    for i, words in EXPECT.items():
        text, _ = voice.transcribe(_load(i))
        for w in words:
            assert w in text, (i, text)
        assert masker.mask(text)[1] == [], (i, text)   # 名簿にない名前が出ていない


class LooseReader(RecordedReader):
    """句読点だけが違う一言を、用意した返事に当てる（声の文字は「、」「。」の付き方が台本と違う）。"""
    def read(self, masked_text, history=None):
        key = lambda t: re.sub(r"[、。,.\s　]", "", t)
        hit = next((k for k in self.replies if key(k) == key(masked_text)), masked_text)
        return super().read(hit, history)


def test_v2_voice_to_table_within_2s(voice, sess):
    sess.reader = LooseReader(json.loads(REPLIES.read_text(encoding="utf-8")))
    samples = _load(1)
    t0 = time.perf_counter()
    text, _ = voice.transcribe(samples)
    r = sess.say(text)
    secs = time.perf_counter() - t0
    print(f"声から表が直るまで {secs:.2f}秒（読み取りは用意した返事）")
    assert r.kind == "applied" and r.changes, r
    assert secs < 2.0
