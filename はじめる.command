#!/bin/zsh
# ダブルクリックで KaigoShiftVoice を開く（Mac）。止める時はこの窓を閉じるか Ctrl+C。
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "動かすのに uv（Python の部品をそろえるプログラム）が要ります。"
  echo "公式の方法（https://docs.astral.sh/uv/）で、このユーザーの中に入れます。"
  printf "入れますか [Y/n] "
  read -r ans
  case "$ans" in
    ""|y|Y|yes|はい) curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "入れられませんでした。README の「動かし方」を見てください"; read -r; exit 1; } ;;
    *) echo "やめました。README の「動かし方」を見てください"; read -r; exit 0 ;;
  esac
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "はじめての時は、部品をそろえるのに1〜2分かかります。"
uv run shiftvoice demo "$@"
echo "終わりました。この窓は閉じてかまいません。"
read -r
