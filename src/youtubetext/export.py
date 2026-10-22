"""Atomic Markdown, TXT, SRT, WebVTT and JSON transcript export."""
from __future__ import annotations

import html
import json
import re
import threading
import unicodedata
import uuid
from dataclasses import asdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .domain import OutputFiles, Transcript, TranscriptSegment
from .completeness import assess_completeness

_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}

# Different URL spellings can resolve to the same video and output directory.
# Extraction stays concurrent; only the brief file replacement phase is
# serialized so a single process cannot interleave two transcript bundles.
_EXPORT_LOCK = threading.Lock()


def format_timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def safe_directory_name(value: str, *, fallback: str = "untitled") -> str:
    normalized = _portable_name(value)
    if not normalized:
        normalized = _portable_name(fallback) or "untitled"
    if normalized.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    return normalized


def _portable_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .-")
    return normalized[:100].rstrip(" .-")


def _segment_markdown(segment: TranscriptSegment) -> str:
    start = format_timestamp(segment.start_seconds)
    end = format_timestamp(segment.end_seconds)
    return f"**[{start} - {end}]**\n\n{segment.text}"


def _render_markdown_document(transcript: Transcript, body: str) -> str:
    meta = transcript.metadata
    lines = [
        f"# {meta.title}",
        "",
        f"- Source: {meta.webpage_url or meta.url}",
        f"- Platform: {meta.platform}",
        f"- Author: {meta.author or 'unknown'}",
        f"- Duration: {format_timestamp(meta.duration_seconds)}",
        f"- Language: {transcript.language or 'unknown'}",
        f"- Extraction: {transcript.method.value}",
        "",
        "## Transcript",
        "",
        body,
    ]
    if transcript.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in transcript.warnings)
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(transcript: Transcript) -> str:
    """Render the complete transcript with one timestamp per segment."""

    body = "\n\n".join(_segment_markdown(item) for item in transcript.segments)
    return _render_markdown_document(transcript, body)


def render_clean_markdown(transcript: Transcript) -> str:
    """Render the complete transcript without per-segment timestamps."""

    body = "\n\n".join(item.text for item in transcript.segments)
    return _render_markdown_document(transcript, body)


def render_text(transcript: Transcript) -> str:
    return "\n".join(
        f"[{format_timestamp(item.start_seconds)} - {format_timestamp(item.end_seconds)}] {item.text}"
        for item in transcript.segments
    ) + "\n"


def _subtitle_timestamp(milliseconds: int, *, separator: str) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _cue_milliseconds(segment: TranscriptSegment) -> tuple[int, int]:
    # Round the whole timestamp before splitting it, so 59.9995 carries into
    # the next minute. A zero/sub-millisecond cue still needs a positive span
    # in subtitle formats; segments.json retains the original precise values.
    start = int(
        (Decimal(str(segment.start_seconds)) * 1000).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    end = int(
        (Decimal(str(segment.end_seconds)) * 1000).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return start, max(start + 1, end)


def _render_subtitle_cues(transcript: Transcript, *, separator: str) -> str:
    blocks = []
    for index, segment in enumerate(transcript.segments, start=1):
        start, end = _cue_milliseconds(segment)
        timing = (
            f"{_subtitle_timestamp(start, separator=separator)} --> "
            f"{_subtitle_timestamp(end, separator=separator)}"
        )
        # Escape before writing cue text: literal tags must stay visible and
        # WebVTT forbids a raw '-->' inside cue text.
        text = html.escape(segment.text, quote=False)
        blocks.append(f"{index}\n{timing}\n{text}")
    return "\n\n".join(blocks) + "\n"


def render_srt(transcript: Transcript) -> str:
    return _render_subtitle_cues(transcript, separator=",")


def render_vtt(transcript: Transcript) -> str:
    return "WEBVTT\n\n" + _render_subtitle_cues(transcript, separator=".")


def segments_payload(transcript: Transcript) -> dict:
    """Version 1: source metadata and ordered, unrounded transcript segments.

    Language and extraction_method apply to every segment. Confidence is null
    when the extraction backend does not provide a score.
    """

    return {
        "schema_version": 1,
        "metadata": asdict(transcript.metadata),
        "language": transcript.language,
        "extraction_method": transcript.method.value,
        "warnings": list(transcript.warnings),
        "completeness": assess_completeness(
            transcript.segments, transcript.metadata.duration_seconds,
        ).as_dict(),
        "segments": [asdict(segment) for segment in transcript.segments],
    }


def metadata_payload(transcript: Transcript) -> dict:
    meta = asdict(transcript.metadata)
    return {
        **meta,
        "language": transcript.language,
        "extraction_method": transcript.method.value,
        "segment_count": len(transcript.segments),
        "warnings": list(transcript.warnings),
        "completeness": assess_completeness(
            transcript.segments, transcript.metadata.duration_seconds,
        ).as_dict(),
    }


def _atomic_write(path: Path, content: str) -> None:
    _atomic_write_many(((path, content),))


def _atomic_write_many(outputs: tuple[tuple[Path, str], ...]) -> None:
    staged: list[tuple[Path, Path]] = []
    for path, content in outputs:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        staged.append((temporary, path))
        try:
            temporary.write_text(content, encoding="utf-8")
        except Exception:
            for candidate, _destination in staged:
                candidate.unlink(missing_ok=True)
            raise
    try:
        for temporary, path in staged:
            temporary.replace(path)
    finally:
        for temporary, _path in staged:
            temporary.unlink(missing_ok=True)


def export_transcript(transcript: Transcript, output_root: Path) -> OutputFiles:
    meta = transcript.metadata
    prefix = safe_directory_name(meta.source_id, fallback=meta.platform)
    title = safe_directory_name(meta.title)
    directory = Path(output_root).expanduser().resolve() / f"{prefix}-{title}"
    directory.mkdir(parents=True, exist_ok=True)

    markdown = directory / "transcript.md"
    clean_markdown = directory / "transcript-clean.md"
    text = directory / "transcript.txt"
    metadata = directory / "metadata.json"
    result = OutputFiles(
        directory=directory,
        markdown=markdown,
        clean_markdown=clean_markdown,
        text=text,
        metadata=metadata,
    )
    outputs = (
        (markdown, render_markdown(transcript)),
        (clean_markdown, render_clean_markdown(transcript)),
        (text, render_text(transcript)),
        (
            metadata,
            json.dumps(
                metadata_payload(transcript),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        ),
        (result.srt, render_srt(transcript)),
        (result.vtt, render_vtt(transcript)),
        (
            result.segments,
            json.dumps(segments_payload(transcript), ensure_ascii=False, indent=2)
            + "\n",
        ),
    )
    with _EXPORT_LOCK:
        _atomic_write_many(outputs)
    return result
