#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "TimeLapse Web requires uv. Install it from https://docs.astral.sh/uv/ and run this script again." >&2
  exit 1
fi

uv sync --frozen
exec uv run timelapse-web
