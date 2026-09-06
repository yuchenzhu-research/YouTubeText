"""Atomic Markdown, TXT and JSON export for a completed transcript."""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import asdict
from pathlib import Path

from .domain import OutputFiles, Transcript, TranscriptSegment


def format_timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def safe_directory_name(value: str, *, fallback: str = "untitled") -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = re.sub(r"[\x00-\x1f/:\\]", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .-")
    return (normalized[:100].strip() or fallback)


def _segment_markdown(segment: TranscriptSegment) -> str:
    start = format_timestamp(segment.start_seconds)
    end = format_timestamp(segment.end_seconds)
    return f"**[{start} - {end}]**\n\n{segment.text}"


def render_markdown(transcript: Transcript) -> str:
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
    ]
    lines.append("\n\n".join(_segment_markdown(item) for item in transcript.segments))
    if transcript.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in transcript.warnings)
    return "\n".join(lines).rstrip() + "\n"


def render_text(transcript: Transcript) -> str:
    return "\n".join(
        f"[{format_timestamp(item.start_seconds)} - {format_timestamp(item.end_seconds)}] {item.text}"
        for item in transcript.segments
    ) + "\n"


def metadata_payload(transcript: Transcript) -> dict:
    meta = asdict(transcript.metadata)
    return {
        **meta,
        "language": transcript.language,
        "extraction_method": transcript.method.value,
        "segment_count": len(transcript.segments),
        "warnings": list(transcript.warnings),
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
    text = directory / "transcript.txt"
    metadata = directory / "metadata.json"
    _atomic_write_many(
        (
            (markdown, render_markdown(transcript)),
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
        )
    )
    return OutputFiles(directory=directory, markdown=markdown, text=text, metadata=metadata)
