"""VTT/SRT parsing and platform-caption cleanup."""

from __future__ import annotations

import html
import re
from pathlib import Path

from youtubetext.domain import TranscriptSegment

_TIMING_RE = re.compile(
    r"(?P<start>(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]*>")
_ASS_TAG_RE = re.compile(r"\{\\[^}]+}")


def parse_subtitle(path: Path) -> tuple[TranscriptSegment, ...]:
    """Parse and clean a UTF-8 WebVTT or SubRip subtitle file."""

    suffix = path.suffix.lower()
    if suffix not in {".vtt", ".srt"}:
        raise ValueError(f"unsupported subtitle format: {suffix or 'unknown'}")

    content = path.read_text(encoding="utf-8-sig", errors="replace")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[TranscriptSegment] = []
    for block in re.split(r"\n[ \t]*\n", content.strip()):
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        match = _TIMING_RE.search(lines[timing_index])
        if match is None:
            continue
        text = _clean_text(lines[timing_index + 1 :])
        if not text:
            continue
        start = _timestamp_seconds(match.group("start"))
        end = _timestamp_seconds(match.group("end"))
        if end < start:
            continue
        cues.append(TranscriptSegment(start, end, text))

    return _merge_rolling_cues(cues)


def _clean_text(lines: list[str]) -> str:
    cleaned_lines: list[str] = []
    for line in lines:
        value = _TAG_RE.sub("", line)
        value = _ASS_TAG_RE.sub("", value)
        value = html.unescape(value).replace("\u200e", "").replace("\u200f", "")
        value = " ".join(value.split())
        if value and (not cleaned_lines or value != cleaned_lines[-1]):
            cleaned_lines.append(value)
    return " ".join(cleaned_lines)


def _timestamp_seconds(value: str) -> float:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:  # Guarded by _TIMING_RE; retained as a clear parser invariant.
        raise ValueError(f"invalid subtitle timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _merge_rolling_cues(
    cues: list[TranscriptSegment],
) -> tuple[TranscriptSegment, ...]:
    """Collapse exact duplicates and YouTube's rolling-prefix caption cues."""

    merged: list[TranscriptSegment] = []
    for cue in sorted(cues, key=lambda item: (item.start_seconds, item.end_seconds)):
        if not merged:
            merged.append(cue)
            continue

        previous = merged[-1]
        nearby = cue.start_seconds <= previous.end_seconds + 0.5
        if nearby and cue.text == previous.text:
            merged[-1] = TranscriptSegment(
                previous.start_seconds,
                max(previous.end_seconds, cue.end_seconds),
                previous.text,
            )
        elif nearby and cue.text.startswith(previous.text):
            merged[-1] = TranscriptSegment(
                previous.start_seconds,
                max(previous.end_seconds, cue.end_seconds),
                cue.text,
            )
        elif nearby and previous.text.startswith(cue.text):
            merged[-1] = TranscriptSegment(
                previous.start_seconds,
                max(previous.end_seconds, cue.end_seconds),
                previous.text,
            )
        else:
            merged.append(cue)
    return tuple(merged)
