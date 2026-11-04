#!/bin/sh
# ファイルを書いた後に、公開してよいかを点検する（tools/check_public.py。鍵の形の文字と、許可していない人の名前）。
# 止まった時は終了コード2で、直し方を Claude に見せる（ファイルはもう書かれているので、直して書き直す）。
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY="uv run --quiet python"; fi
out=$($PY tools/check_public.py 2>&1)
if [ $? -ne 0 ]; then
  {
    echo "公開の点検で止めました（tools/check_public.py）。"
    echo "$out"
    echo "直し方。人の名前は、tools/allowed_names.json にある作り話の名前に替えてください（実在の施設や職員の名前は書かない）。"
    echo "鍵のような文字は、ファイルに書かず .env か環境変数に置いてください（.env は git に入りません）。"
  } >&2
  exit 2
fi
exit 0
