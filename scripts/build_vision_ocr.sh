#!/usr/bin/env bash
# Build the Apple Silicon helper that bridges Python to macOS Vision OCR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE="$PROJECT_ROOT/native/macos/VisionOCR.swift"
OUTPUT="${YOUTUBETEXT_VISION_OCR_OUTPUT:-$PROJECT_ROOT/bin/youtubetext-vision-ocr}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Apple Vision OCR can only be built on macOS." >&2
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "YouTubeText currently supports Apple Silicon Macs only." >&2
  exit 1
fi

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc was not found. Install the Xcode Command Line Tools first." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"
swiftc \
  -O \
  -parse-as-library \
  -target arm64-apple-macosx13.0 \
  -framework Foundation \
  -framework ImageIO \
  -framework Vision \
  "$SOURCE" \
  -o "$OUTPUT"
chmod +x "$OUTPUT"
printf '%s\n' "$OUTPUT"
