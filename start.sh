#!/usr/bin/env bash
# One-command start for macOS / Linux.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Setting up Agent Tillu (first run only)…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -e ".[browser]"
  ./.venv/bin/python -m playwright install chromium || echo "(Browser tools skipped: Chromium could not be installed.)"
fi
if command -v ollama >/dev/null 2>&1; then
  MODEL="${TILLU_MODEL:-qwen3:8b}"
  ollama list 2>/dev/null | grep -q "^${MODEL%%:*}" || { echo "Pulling $MODEL (one time)…"; ollama pull "$MODEL" || true; }
  ollama list 2>/dev/null | grep -q "^nomic-embed-text" || ollama pull nomic-embed-text || true
else
  echo "Ollama not found. Install it from https://ollama.com, or use LM Studio and set the URL in Settings."
  echo "Starting with the built-in 'stub' model so you can look around."
  export TILLU_MODEL="${TILLU_MODEL:-stub}"
fi
exec ./.venv/bin/python -m tillu "$@"
