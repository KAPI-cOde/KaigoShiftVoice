#!/usr/bin/env bash
# 公開版を Hugging Face の Space へ送る。使い方（リポジトリの頭で）
#   deploy/hf-space/push.sh <持ち主>/<Space の名前>      例 deploy/hf-space/push.sh KAPI-cODe/kaigoshiftvoice
# 送る前に、別のターミナルで Hugging Face にログインしておく（hf auth login。トークンはそこで入れる）。
# このスクリプトはトークンも鍵も読まず、書かない。Claude の鍵は Space の設定の Secrets に ANTHROPIC_API_KEY で入れる。
# 送る物は、コミット済みの（git archive で取り出した）アプリの本体（src）・見本の施設（data/hidamari）・
# pyproject.toml・LICENSE と、このフォルダの Dockerfile と README.md（Space 用の頭書きつき）だけ。
# Git に入っていないファイル（.env など）は送らない。写した後の置き場を、そのまま全部点検してから送る。
set -euo pipefail

space="${1:-}"
if [[ ! "$space" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "Space の名前を <持ち主>/<名前> の形で渡してください" >&2
  exit 1
fi
repo="$(git rev-parse --show-toplevel)"
cd "$repo"
uv run python tools/check_public.py
paths=(src data/hidamari pyproject.toml LICENSE deploy/hf-space/Dockerfile deploy/hf-space/README.md)
if [[ -n "$(git status --porcelain -- "${paths[@]}")" ]]; then
  echo "送る物にコミットしていない変更があります。コミットしてから送ってください" >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone --depth 1 "https://huggingface.co/spaces/$space" "$work/space"
cd "$work/space"
git rm -rq --ignore-unmatch . >/dev/null
git -C "$repo" archive HEAD src data/hidamari pyproject.toml LICENSE | tar -x -C .
git -C "$repo" show HEAD:deploy/hf-space/Dockerfile > Dockerfile
git -C "$repo" show HEAD:deploy/hf-space/README.md > README.md
(cd "$repo" && uv run python tools/check_public.py --dir "$work/space")
git add -A
if git diff --cached --quiet; then
  echo "変わった所がないので、送りませんでした"
  exit 0
fi
git commit -qm "公開版を $(git -C "$repo" rev-parse --short HEAD) から送る"
git push origin HEAD
echo "送りました https://huggingface.co/spaces/$space"
