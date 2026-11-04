# KaigoShiftVoice を Claude Code で触る時の決まり

話して直せる介護の勤務表のアプリです。このファイルは、Claude Code でこのリポジトリを開いた時に読む決まりです。開発の進め方の記録は docs/development.md にあります。

## 言葉
- チャット・コミットメッセージ・画面の文字・文書は日本語で書く
- 使う人が見る言葉（画面・ボタン・字幕）では「ルール」と言い、数え方は「件」にする
- 医学的な判断や助言はしない。画面の下と README にそう書いてある

## 守ること
- 施設と職員はすべて作り話にする（ひだまりの郷、こもれびの家など）。人の名前は tools/allowed_names.json にある作り話の名前だけを使い、実在の施設・職員の名前をコード・試験・文書・コミットに書かない
- API の鍵は、コードにもリポジトリにも画面にも出さない。.env か環境変数に置く（.env は git に入らない）
- Claude へ送るのは、名簿の名前を「職員01」のような番号に置き換えた一言だけにする
- ファイルを書くたびに .claude/settings.json のフックが tools/check_public.py（鍵の形と、許可していない名前）を回す。止まったら、表示された直し方のとおりに直す。コミットの前にも `uv run python tools/check_public.py` を流す
- コミットは機能ごとに小さくする。force push と履歴の書き換えはしない
- 試験を消したり弱めたりしない。直した時は、直す前のコードで落ちる試験を足す

## 作り
- src/shiftvoice/ にアプリの本体がある。session.py（一言から表を直す流れ）、reader.py（Claude Haiku で一言を読む・Claude Sonnet で当番を組み立てる）、solver.py（OR-Tools で表を作る）、onboard.py（Excel の勤務表と国の標準様式の取り込み）、standby.py（待機当番と公休移動）、giveback.py と nextflow.py（翌月に返す案）、server.py（手元だけのサーバー）、static/index.html（画面）
- 試験は `env -u ANTHROPIC_API_KEY uv run pytest -q`。本物の Claude で録った返事（tests/fixtures/replies.json・builds.json）で回るので鍵は要らない。本物の Claude で台本の3幕を通すのは `SHIFTVOICE_LIVE=1`（鍵が要る）
- 画面を確かめる時は .claude/skills/screen-check の手順で、録った返事のサーバーを写したデータで開いて撮る

## 進め方
- 設計の判断と検収はメインの Claude が受け持つ。量の多い作業（画面・調べもの）はサブエージェントに任せ、メインが差分を読み、試験を流し、撮った画面を見てから取り込む。報告の文だけで合格にしない
- 区切りごとに、書いた側とは別のサブエージェントに壊す側の目で差分を読ませる（.claude/agents/adversarial-reviewer.md）。指摘には起きる条件・起きやすさ（0〜10。このアプリの使い方で、その条件が実際に起きる起きやすさ）・影響（1〜10）を付けさせ、掛け算が20以上か影響8以上の物だけを直す。見送った物は一覧に残す
- 使う人の目（.claude/agents/facility-reader.md）、個人情報の目（.claude/agents/privacy-reviewer.md）、まっさらな取得で README どおりに動かす目（.claude/agents/fresh-clone-tester.md）、審査員の目（.claude/agents/demo-judge.md）でも確かめる
- 画面を変えたら、見えないブラウザで撮って自分で見る。撮った後はサーバーと見えないブラウザを必ず止める
