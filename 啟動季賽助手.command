#!/bin/zsh

cd -- "${0:A:h}" || exit 1

if [[ ! -x .venv/bin/python ]]; then
  print -r -- '尚未建立執行環境。請先在此專案目錄執行：uv sync --locked'
  read -r '?按 Enter 結束。'
  exit 1
fi

export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m fba.apps.inseason.launcher "$@"
