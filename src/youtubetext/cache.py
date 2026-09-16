"""Private, versioned transcript cache used by the opt-in resume path."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from .domain import (
    SourceMetadata,
    TaskOptions,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)

CACHE_SCHEMA = 1
PIPELINE_REVISION = "fallback-transcript-v1"


class TranscriptCache:
    """Persist only complete normalized transcripts; never media or credentials."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        auth_scope: str = "anonymous",
    ) -> None:
        self.root = (
            Path(root).expanduser()
            if root is not None
            else Path(user_cache_dir("YouTubeText")) / "transcripts"
        )
        self._auth_scope = hashlib.sha256(auth_scope.encode("utf-8")).hexdigest()

    def load(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
    ) -> Transcript | None:
        path = self._path(metadata, options)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if payload.get("schema") != CACHE_SCHEMA:
                return None
            if payload.get("pipeline_revision") != PIPELINE_REVISION:
                return None
            transcript = _decode_transcript(payload["transcript"])
            if not _same_source_identity(transcript.metadata, metadata):
                return None
            return transcript
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def save(self, transcript: Transcript, options: TaskOptions) -> bool:
        path = self._path(transcript.metadata, options)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        payload = {
            "schema": CACHE_SCHEMA,
            "pipeline_revision": PIPELINE_REVISION,
            "transcript": _encode_transcript(transcript),
        }
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.chmod(0o700)
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            path.chmod(0o600)
            return True
        except (OSError, TypeError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def key(self, metadata: SourceMetadata, options: TaskOptions) -> str:
        """Return the opaque task key shared by transcript and stage storage."""

        return self._path(metadata, options).stem

    def _path(self, metadata: SourceMetadata, options: TaskOptions) -> Path:
        signature = {
            "schema": CACHE_SCHEMA,
            "pipeline_revision": PIPELINE_REVISION,
            "platform": metadata.platform,
            "source_id": metadata.source_id,
            "requested_url": metadata.url.strip(),
            "webpage_url": metadata.webpage_url.strip(),
            "duration_seconds": metadata.duration_seconds,
            "mode": options.mode.value,
            "language": options.language.strip(),
            "caption_languages": list(options.preferred_caption_languages),
            "whisper_model": options.whisper_model,
            "auth_scope": self._auth_scope,
        }
        encoded = json.dumps(
            signature,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.root / f"{hashlib.sha256(encoded).hexdigest()}.json"


def _encode_transcript(transcript: Transcript) -> dict[str, Any]:
    return {
        "metadata": asdict(transcript.metadata),
        "language": transcript.language,
        "method": transcript.method.value,
        "segments": [asdict(segment) for segment in transcript.segments],
        "warnings": list(transcript.warnings),
    }


def _same_source_identity(first: SourceMetadata, second: SourceMetadata) -> bool:
    return (
        first.platform,
        first.source_id,
        first.url.strip(),
        first.webpage_url.strip(),
        first.duration_seconds,
    ) == (
        second.platform,
        second.source_id,
        second.url.strip(),
        second.webpage_url.strip(),
        second.duration_seconds,
    )


def _decode_transcript(raw: object) -> Transcript:
    if not isinstance(raw, dict):
        raise TypeError("cached transcript must be an object")
    metadata_raw = raw["metadata"]
    segments_raw = raw["segments"]
    if not isinstance(metadata_raw, dict) or not isinstance(segments_raw, list):
        raise TypeError("cached transcript fields have invalid types")
    metadata = SourceMetadata(**metadata_raw)
    segments = tuple(
        TranscriptSegment(**segment)
        for segment in segments_raw
        if isinstance(segment, dict)
    )
    if len(segments) != len(segments_raw):
        raise TypeError("cached transcript segment must be an object")
    warnings_raw = raw.get("warnings", [])
    if not isinstance(warnings_raw, list):
        raise TypeError("cached warnings must be a list")
    return Transcript(
        metadata=metadata,
        language=str(raw["language"]),
        method=TranscriptMethod(str(raw["method"])),
        segments=segments,
        warnings=tuple(str(warning) for warning in warnings_raw),
    )
