---
name: screen-check
description: 画面（static/index.html）を変えた後に、録った返事のサーバーを写したデータで開き、見えないブラウザで操作して撮り、写真を自分で見て確かめる手順。画面の文言・並べ方・印刷・コピーを変えた時に使う
---

# 画面を撮って確かめる

報告の文だけで合格にせず、使う人と同じ操作をして撮った写真を自分で見るための手順です。

## 1. 開く
1. データを作業用のフォルダに写す（リポジトリの data/ を直接使わない）。`cp -R data/hidamari <作業用>/hidamari`
2. 録った返事でサーバーを開く。`env -u ANTHROPIC_API_KEY uv run python tools/screen_check.py <作業用>/hidamari 8790`（11月末の状態から見る時は `--act3` を付ける）
3. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/state` が 200 になるまで待つ

## 2. 操作して撮る
- `uv run --with playwright python <スクリプト>` で動かす。`launch_persistent_context` の置き場（user-data-dir）は作業用のフォルダの中に作り、`headless=True`、窓は 1440×950 を基本にする（ノートパソコンの幅を見る時は 1280・1366・1024）
- 一言は `#input` に入れて `#send` を押し、`#reply` に文字が出るまで待つ。ボタンは名前で押す（`get_by_role("button", name="…", exact=True)`）
- 計算の途中で次を押すと白い画面や押し損じになるので、待ちは長めにする（12月の表を作る時は、終わりの文が出るまで待つ）
- 印刷は `page.pdf()` で書き出して画像にして見る（`emulate_media(media="print")` の写真は、色が消えていく途中を写すことがある）
- 表のコピーは、クリップボードの読み書きを許して `navigator.clipboard.readText()` で中身も確かめる

## 3. 見る
- 撮った写真は必ず自分で開いて見る。頼まれた文が出ているか、文字が切れていないか、字幕や枠が重なっていないかを確かめる
- 文の有無は、写真に加えて `inner_text` でも確かめる

## 4. 片付ける
- サーバーを止める（`pkill -f "screen_check.py <作業用>"`）
- 見えないブラウザを閉じ、`pgrep -fl -- '--headless'` で残りが無いことを確かめる
