#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${GEMINI_VIDEO_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/gemini-video-auditor/env}"

if [[ -n "${GEMINI_VIDEO_PYTHON:-}" ]]; then
  PYTHON="$GEMINI_VIDEO_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" "$ROOT/scripts/analyze_video.py" --env-file "$ENV_FILE" "$@"
