#!/usr/bin/env bash
# Prepare a repository-local YouTubeText environment on Apple Silicon macOS.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(dirname -- "$SCRIPT_DIR")"
PYTHON_BIN="${YOUTUBETEXT_PYTHON:-python3}"
VENV_DIR="$PROJECT_ROOT/.venv"

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  fail "YouTubeText currently requires macOS on Apple Silicon (arm64)."
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  fail "Python 3.11 through 3.14 was not found. Install it, then run this script again."
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 15) else 1)'; then
  PYTHON_VERSION="$($PYTHON_BIN --version 2>&1 || true)"
  fail "Python 3.11 through 3.14 is required (found: ${PYTHON_VERSION:-unknown})."
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  fail "FFmpeg was not found. Install it with 'brew install ffmpeg', then retry."
fi
if ! command -v swiftc >/dev/null 2>&1; then
  fail "swiftc was not found. Install Apple's Command Line Tools with 'xcode-select --install'."
fi

cd -- "$PROJECT_ROOT"
printf 'Preparing YouTubeText in %s\n' "$PROJECT_ROOT"

if command -v uv >/dev/null 2>&1; then
  printf 'Synchronizing the runtime environment with uv...\n'
  uv sync --no-dev --python "$PYTHON_BIN"
  RUNNER="uv run --no-dev youtubetext"
else
  printf '%s\n' "uv was not found; using Python's built-in venv and pip instead."
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  if ! "$VENV_DIR/bin/python" -c \
    'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 15) else 1)'
  then
    fail "The existing .venv does not use Python 3.11 through 3.14; recreate it."
  fi
  "$VENV_DIR/bin/python" -m pip install --editable "$PROJECT_ROOT"
  RUNNER="\"$VENV_DIR/bin/youtubetext\""
fi

printf 'Building the native Apple Vision OCR helper...\n'
"$PROJECT_ROOT/scripts/build_vision_ocr.sh" >/dev/null

printf '\nYouTubeText is ready. Run it from the repository root:\n'
printf '  cd %q\n' "$PROJECT_ROOT"
printf '  %s --help\n' "$RUNNER"
printf '  %s "https://www.youtube.com/watch?v=VIDEO_ID"\n' "$RUNNER"
