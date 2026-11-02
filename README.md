# shift-voice-2026

介護の勤務表を、話した言葉で直していくアプリです（Built with Claude: Japan 2026 の作品。作成中）。

- 勤務の並びの目安を扱う道具で、医学的な助言はしません
- デモの施設「ひだまりの郷」と職員は、すべて作り話です

## 動かし方

いるものは [uv](https://docs.astral.sh/uv/) と、自分の Claude の API の鍵です。

```
uv run shiftvoice demo
```

ブラウザで http://127.0.0.1:8765/ を開きます（このパソコンの中だけで開くサーバーです）。はじめて開く時は、11月の表を作るのに20秒ほどかかります。

### 文字だけで試す（声のモデルは要りません）

1. 画面の下の欄に、たとえば「遅番の次の日に早番はきついから、やめてあげて」と打って［伝える］を押します
2. 「毎月ですか、今月だけですか」と聞かれるので［毎月］を押します。ルールが1件増え、当てはまっていたマスが光って直ります
3. ほかの例。「岡田さん、15と16は法事で休み」「小島さん、29日に試験だから、それまで夜勤3回まで」「杉山さん、12月から夜勤もできるって」

最初の状態に戻す時は `uv run shiftvoice demo --reset` です。

### 声で試す（任意）

声は、このパソコンの中だけで文字にします（外へは送りません）。はじめての時は音声認識のモデル（約2.4GB）を取り込みます。

```
uv run --extra voice shiftvoice demo --voice
```

画面の［話す］を押して一言話し、もう一度押すと文字になり、文字で入れた時と同じように表が直ります。モデルの置き場は `~/Library/Caches/shift-voice/models/` で、環境変数 `SHIFTVOICE_MODELS` で変えられます。

### 鍵の入れ方

一言の読み取りに Claude（Haiku 5.5）を使います。鍵は、このフォルダの `.env` に入れます（`.env` は Git に入りません）。鍵が画面や記録に出ないよう、次のように入力を求める形で書いてください。

```
read -rs "k?ANTHROPIC_API_KEY: " && printf 'ANTHROPIC_API_KEY=%s\n' "$k" > .env && unset k
```

（macOS の zsh の書き方です。bash では `read -rsp "ANTHROPIC_API_KEY: " k` にします）

鍵がない時も表は見られますが、一言の読み取りは動きません。Claude へ送るのは、職員の名前を「職員01」のような番号に置き換えた一言だけです。

### 試験

```
uv run pytest -q
```

試験は Claude を呼ばず、用意した返事で回ります（本物の Claude での通しは別に測ります）。

## 作り方について

アイデアと仕様は期間の前に考え、このリポジトリのコードはハッカソンの期間中（2026-11-02 から）にすべて新しく書きました。

## 使っている外の部品

| 部品 | 使い道 | ライセンス |
|---|---|---|
| OR-Tools（CP-SAT） | 勤務表を作る計算 | Apache-2.0 |
| Anthropic Python SDK | 一言の読み取り（Claude Haiku 5.5）と当番の組み立て（Claude Sonnet 5.5）を呼ぶ | MIT |
| Haas-ja-v1.0（Third Intelligence） | 日本語の音声認識の元のモデル（声で試す時だけ） | Apache-2.0 |
| oboroge0/Haas-ja-v1.0-onnx | 上のモデルを sherpa-onnx で動かす変換版（版 2c2133a を固定） | Apache-2.0（配布の記載） |
| sherpa-onnx・sherpa-onnx-core | 音声認識を動かす | Apache-2.0 |
| ONNX Runtime | モデルの推論（sherpa-onnx-core に同梱） | MIT |
| huggingface_hub | モデルの取り込み | Apache-2.0 |
| NumPy | 音声の配列 | BSD-3-Clause |

## ライセンス

MIT（作者 KAPI-cODe）
