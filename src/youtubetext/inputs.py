"""Read-only local media inputs alongside the existing platform source client."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .domain import SourceMetadata
from .sources import (
    SourceClient, SourceFetchError, SourceInspection, SourceResult,
    SubtitleAvailability, SubtitleKind, SubtitleTrack,
)
from .sources.subtitles import parse_subtitle


def local_path(value: str) -> Path | None:
    """Resolve a local argument, leaving HTTP(S)/other remote URLs to sources."""
    parsed = urlsplit(str(value))
    if parsed.scheme.lower() == "file":
        if parsed.netloc.lower() not in {"", "localhost"}:
            raise ValueError("file URLs must refer to this computer")
        if parsed.query or parsed.fragment:
            raise ValueError("file URLs cannot contain a query or fragment")
        path = Path(url2pathname(parsed.path))
    elif "://" in str(value):
        return None
    else:
        path = Path(value).expanduser()
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"local media file does not exist: {path}")
    return path


def probe_local_media(path: Path) -> Mapping:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise SourceFetchError("local metadata", "FFprobe was not found; install FFmpeg")
    result = subprocess.run(
        [executable, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,duration:stream_disposition=attached_pic", "-of", "json", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise SourceFetchError("local metadata", "FFprobe could not read this media file")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise SourceFetchError("local metadata", "FFprobe returned invalid metadata") from exc


class InputSources:
    """One pipeline source seam for URLs, filesystem paths and file URLs."""

    def __init__(self, remote: SourceClient | None = None, *, probe: Callable = probe_local_media):
        self._remote = remote or SourceClient()
        self._probe = probe

    def _inspect_local(self, path: Path, preferred_languages: Sequence[str]) -> tuple[SourceMetadata, Path | None, str]:
        info = self._probe(path)
        if not isinstance(info, Mapping):
            raise SourceFetchError("local metadata", "media metadata must be an object")
        streams = info.get("streams") or ()
        kinds = {
            stream.get("codec_type") for stream in streams
            if isinstance(stream, Mapping)
            and not (stream.get("codec_type") == "video" and (stream.get("disposition") or {}).get("attached_pic"))
        }
        if not {"audio", "video"}.intersection(kinds):
            raise SourceFetchError("local metadata", "file has no audio or video stream")
        duration = (info.get("format") or {}).get("duration")
        if duration is None:
            duration = max((float(stream.get("duration") or 0) for stream in streams if isinstance(stream, Mapping)), default=0)
        duration = float(duration or 0)
        if not math.isfinite(duration) or duration < 0:
            raise SourceFetchError("local metadata", "media duration is invalid")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as media:
            for block in iter(lambda: media.read(1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SourceFetchError("local metadata", "media changed while it was being inspected")
        metadata = SourceMetadata(
            url=path.as_uri(), platform="local", source_id=f"local-{digest.hexdigest()}",
            title=path.stem, duration_seconds=duration, webpage_url=path.as_uri(),
            media_type="video" if "video" in kinds else "audio",
        )
        language = next((value for value in preferred_languages if value != "auto"), "und")
        # A nearby sidecar is an explicit local transcript. Embedded subtitle
        # extraction can be added separately without treating screen text as one.
        candidates = [path.with_suffix(suffix) for suffix in (".vtt", ".srt")]
        sidecar = next((candidate for candidate in candidates if candidate != path and candidate.is_file()), None)
        return metadata, sidecar, language

    def inspect(self, value: str, *, preferred_languages: Sequence[str] = ()) -> SourceInspection:
        path = local_path(value)
        if path is None:
            return self._remote.inspect(value, preferred_languages=preferred_languages)
        metadata, sidecar, language = self._inspect_local(path, preferred_languages)
        subtitle = SubtitleAvailability(language, SubtitleKind.MANUAL) if sidecar else None
        return SourceInspection(metadata, subtitle)

    def fetch(
        self, value: str, *, preferred_languages: Sequence[str] = (),
        include_subtitles: bool = True, strict_subtitles: bool = True,
    ) -> SourceResult:
        path = local_path(value)
        if path is None:
            return self._remote.fetch(
                value, preferred_languages=preferred_languages,
                include_subtitles=include_subtitles, strict_subtitles=strict_subtitles,
            )
        metadata, sidecar, language = self._inspect_local(path, preferred_languages)
        if sidecar is None or not include_subtitles:
            return SourceResult(metadata)
        failed = False
        for candidate in (path.with_suffix(".vtt"), path.with_suffix(".srt")):
            if candidate == path or not candidate.is_file():
                continue
            try:
                segments = parse_subtitle(candidate)
                if not segments:
                    raise ValueError("local subtitle file has no usable cues")
            except (OSError, UnicodeError, ValueError):
                failed = True
                continue
            return SourceResult(
                metadata, SubtitleTrack(language, SubtitleKind.MANUAL, segments),
                warnings=(("An earlier local subtitle file was unusable; used another local sidecar.",) if failed else ()),
            )
        if strict_subtitles:
            raise SourceFetchError("local subtitle", "no nearby SRT/VTT file contained usable cues")
        return SourceResult(metadata, warnings=("Local subtitle files could not be used; local recognition will be tried.",))
