#!/bin/zsh

cd -- "${0:A:h}" || exit 1

fba_uv="$(command -v uv)"
if [[ -z "$fba_uv" && -x "$HOME/.local/bin/uv" ]]; then
  fba_uv="$HOME/.local/bin/uv"
fi
if [[ -z "$fba_uv" ]]; then
  print -r -- '請先安裝 uv，再重新開啟命令檔：'
  print -r -- 'https://docs.astral.sh/uv/getting-started/installation/'
  [[ -t 0 ]] && read -r '?按 Enter 結束。'
  exit 127
fi

exec "$fba_uv" run --locked --no-dev fba app "$@"
