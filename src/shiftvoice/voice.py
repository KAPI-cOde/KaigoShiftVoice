"""声の入口（15章）。手元の Haas-ja-v1.0（ONNX 版）を sherpa-onnx で動かす。声は外へ送らない。

- モデルはリポジトリの外（既定 ~/Library/Caches/shift-voice/models/haas-ja-v1.0-onnx。環境変数 SHIFTVOICE_MODELS で変えられる）
- 職員の名前と勤務の言葉を手がかり（hotwords、強さ1.5）として渡す。渡さないと 小島→尾島 などが出る
- 音声は16kHz・モノラルの float の並び。ブラウザで16kHz に直してから送る（ffmpeg を使わない）
- 長い発言は25秒ずつに切る。最初の読み込み（約11秒）は起動時に済ませる
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

REVISION = "2c2133a4279f"  # oboroge0/Haas-ja-v1.0-onnx の固定した版（短い名前。手元のフックが40桁の16進を鍵の形として止めるため）
FILES = ("encoder.onnx", "encoder.data", "decoder.onnx", "joiner.onnx", "tokens.txt")
SAMPLE_RATE = 16000
CHUNK_SECONDS = 25
SHIFT_WORDS = ("早番", "日勤", "遅番", "夜勤", "明け", "公休", "有給", "研修", "待機", "毎月", "今月だけ")


def model_dir() -> Path:
    base = os.environ.get("SHIFTVOICE_MODELS") or str(Path.home() / "Library" / "Caches" / "shift-voice" / "models")
    return Path(base) / "haas-ja-v1.0-onnx"


def missing_files(d: Path | None = None) -> list[str]:
    d = d or model_dir()
    return [f for f in FILES if not (d / f).exists()]


def download(d: Path | None = None) -> Path:
    """無い時だけ取得する（約2.4GB）。進み具合は huggingface_hub が出す。"""
    from huggingface_hub import snapshot_download
    d = d or model_dir()
    d.parent.mkdir(parents=True, exist_ok=True)
    snapshot_download("oboroge0/Haas-ja-v1.0-onnx", revision=REVISION, local_dir=str(d),
                      allow_patterns=list(FILES) + ["LICENSE", "NOTICE", "README.md"])
    return d


class Voice:
    def __init__(self, names: list[str], d: Path | None = None):
        import sherpa_onnx

        d = d or model_dir()
        miss = missing_files(d)
        if miss:
            raise FileNotFoundError(f"声のモデルがありません（{', '.join(miss)}）。{d}")
        hw = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        for w in list(dict.fromkeys(list(names) + list(SHIFT_WORDS))):
            hw.write(w + "\n")
        hw.close()
        self.hotwords = hw.name
        t0 = time.perf_counter()
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.onnx"), decoder=str(d / "decoder.onnx"), joiner=str(d / "joiner.onnx"),
            tokens=str(d / "tokens.txt"), num_threads=4, sample_rate=SAMPLE_RATE, feature_dim=128,
            decoding_method="modified_beam_search", max_active_paths=4,
            hotwords_file=self.hotwords, hotwords_score=1.5, model_type="nemo_transducer",
        )
        self.load_seconds = time.perf_counter() - t0

    def transcribe(self, samples) -> tuple[str, float]:
        """16kHz の float の並びを文字にする。返すのは（文字, 秒）。"""
        t0 = time.perf_counter()
        step = SAMPLE_RATE * CHUNK_SECONDS
        texts = []
        for i in range(0, max(len(samples), 1), step):
            st = self.rec.create_stream()
            st.accept_waveform(SAMPLE_RATE, samples[i:i + step])
            self.rec.decode_stream(st)
            texts.append(st.result.text.strip())
        return "".join(texts), time.perf_counter() - t0
