"""Deep source module: URL in, normalized metadata and captions out."""

from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from youtubetext.domain import SourceMetadata

from ._adapter import PlatformAdapter, resolve_adapter, run_with_platform_retries
from ._yt_dlp import QUIET_YT_DLP_LOGGER
from .models import SourceFetchError, SourceResult, SubtitleKind, SubtitleTrack
from .subtitles import parse_subtitle


class Runner(Protocol):
    """Internal seam around the true external yt-dlp dependency."""

    def run(
        self,
        url: str,
        options: Mapping[str, Any],
        *,
        download: bool,
    ) -> Mapping[str, Any]: ...


class YtDlpRunner:
    """Production runner backed by yt-dlp."""

    def run(
        self,
        url: str,
        options: Mapping[str, Any],
        *,
        download: bool,
    ) -> Mapping[str, Any]:
        try:
            import yt_dlp
        except ImportError as exc:  # pragma: no cover - packaging failure guard
            raise RuntimeError("yt-dlp is not installed") from exc

        with yt_dlp.YoutubeDL(dict(options)) as ydl:
            result = ydl.extract_info(url, download=download)
        if not isinstance(result, Mapping):
            raise RuntimeError("yt-dlp returned no video metadata")
        return result


class SourceClient:
    """Acquire one YouTube/Bilibili source through a single stable interface.

    ``fetch`` performs no audio or video download. It inspects metadata and, if
    the platform advertises captions, downloads only the selected subtitle.
    ``None`` is a normal subtitle result and means the caller should use OCR or
    ASR. Recognized sources that fail inspection/download/parsing raise
    :class:`SourceFetchError` instead of being mistaken for caption absence.
    """

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runner = runner or YtDlpRunner()
        self._sleeper = sleeper

    def fetch(
        self,
        url: str,
        *,
        preferred_languages: Sequence[str] = (),
        include_subtitles: bool = True,
        strict_subtitles: bool = True,
    ) -> SourceResult:
        adapter = resolve_adapter(url)
        request_url = adapter.request_url(url)
        try:
            info = run_with_platform_retries(
                adapter,
                lambda: self._runner.run(
                    request_url,
                    self._options(adapter),
                    download=False,
                ),
                sleep=self._sleeper,
            )
        except Exception as exc:
            if isinstance(exc, SourceFetchError):
                raise
            raise SourceFetchError("metadata", str(exc) or type(exc).__name__) from exc

        try:
            metadata = _metadata(url, adapter, info)
            selected = (
                _select_subtitle(
                    info,
                    adapter,
                    tuple(preferred_languages),
                )
                if include_subtitles
                else None
            )
        except Exception as exc:
            raise SourceFetchError("metadata", str(exc) or type(exc).__name__) from exc
        if selected is None:
            return SourceResult(metadata=metadata)

        language, kind = selected
        try:
            subtitle = self._download_subtitle(request_url, adapter, language, kind)
        except SourceFetchError as exc:
            if strict_subtitles:
                raise
            return SourceResult(
                metadata=metadata,
                warnings=(f"Platform captions could not be used: {exc}",),
            )
        return SourceResult(metadata=metadata, subtitle=subtitle)

    def _download_subtitle(
        self,
        url: str,
        adapter: PlatformAdapter,
        language: str,
        kind: SubtitleKind,
    ) -> SubtitleTrack:
        with tempfile.TemporaryDirectory(prefix="youtubetext-subtitles-") as directory:
            root = Path(directory)
            options = self._options(adapter)
            options.update(
                {
                    "skip_download": True,
                    "writesubtitles": kind is SubtitleKind.MANUAL,
                    "writeautomaticsub": kind is SubtitleKind.AUTOMATIC,
                    "subtitleslangs": [language],
                    "subtitlesformat": "vtt/srt/best",
                    "outtmpl": str(root / "subtitle.%(ext)s"),
                }
            )
            try:
                run_with_platform_retries(
                    adapter,
                    lambda: self._runner.run(url, options, download=True),
                    sleep=self._sleeper,
                )
            except Exception as exc:
                raise SourceFetchError(
                    "subtitle download", str(exc) or type(exc).__name__
                ) from exc

            candidates = sorted(
                (
                    path
                    for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".vtt", ".srt"}
                ),
                key=lambda path: (path.suffix.lower() != ".vtt", path.name),
            )
            if not candidates:
                raise SourceFetchError(
                    "subtitle download",
                    f"track {language!r} was advertised but no VTT/SRT file was produced",
                )
            try:
                segments = parse_subtitle(candidates[0])
            except (OSError, UnicodeError, ValueError) as exc:
                raise SourceFetchError("subtitle parse", str(exc)) from exc
            if not segments:
                raise SourceFetchError(
                    "subtitle parse", "downloaded subtitle contained no usable cues"
                )
            return SubtitleTrack(language=language, kind=kind, segments=segments)

    @staticmethod
    def _options(adapter: PlatformAdapter) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "noprogress": True,
            "skip_download": True,
            "logger": QUIET_YT_DLP_LOGGER,
        }
        options.update(adapter.yt_dlp_options())
        return options


def _metadata(
    requested_url: str,
    adapter: PlatformAdapter,
    info: Mapping[str, Any],
) -> SourceMetadata:
    source_id = str(info.get("id") or "").strip()
    if not source_id:
        raise ValueError("yt-dlp metadata did not contain a video id")
    duration_value = info.get("duration") or 0.0
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("yt-dlp returned an invalid duration") from exc
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("yt-dlp returned an invalid duration")

    return SourceMetadata(
        url=requested_url,
        platform=adapter.name,
        source_id=source_id,
        title=str(info.get("title") or source_id),
        author=str(
            info.get("uploader")
            or info.get("channel")
            or info.get("creator")
            or ""
        ),
        duration_seconds=duration,
        webpage_url=str(info.get("webpage_url") or requested_url),
    )


def _select_subtitle(
    info: Mapping[str, Any],
    adapter: PlatformAdapter,
    preferred_languages: tuple[str, ...],
) -> tuple[str, SubtitleKind] | None:
    manual = _available_languages(info.get("subtitles"))
    automatic = _available_languages(info.get("automatic_captions"))
    if not manual and not automatic:
        return None

    explicit = tuple(
        language.strip()
        for language in preferred_languages
        if language and language.strip().lower() != "auto"
    )
    if explicit:
        match = _best_requested_track(explicit, manual, automatic)
        if match is not None:
            return match

    # Without a matching explicit preference, preserve human-caption quality:
    # choose the best manual language first, then consider automatic captions.
    if manual:
        return _best_language(manual, adapter.language_priority), SubtitleKind.MANUAL
    return _best_language(automatic, adapter.language_priority), SubtitleKind.AUTOMATIC


def _available_languages(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Mapping):
        return ()
    ignored = {"live_chat", "danmaku", "comments"}
    return tuple(
        str(language)
        for language, formats in raw.items()
        if str(language).lower() not in ignored and bool(formats)
    )


def _best_requested_track(
    preferences: tuple[str, ...],
    manual: tuple[str, ...],
    automatic: tuple[str, ...],
) -> tuple[str, SubtitleKind] | None:
    for preference in preferences:
        for exact_only in (True, False):
            for languages, kind in (
                (manual, SubtitleKind.MANUAL),
                (automatic, SubtitleKind.AUTOMATIC),
            ):
                match = next(
                    (
                        language
                        for language in languages
                        if _language_matches(language, preference, exact_only=exact_only)
                    ),
                    None,
                )
                if match is not None:
                    return match, kind
    return None


def _best_language(
    available: tuple[str, ...],
    priority: tuple[str, ...],
) -> str:
    for preference in priority:
        exact = next(
            (
                language
                for language in available
                if _language_matches(language, preference, exact_only=True)
            ),
            None,
        )
        if exact is not None:
            return exact
    for preference in priority:
        compatible = next(
            (
                language
                for language in available
                if _language_matches(language, preference, exact_only=False)
            ),
            None,
        )
        if compatible is not None:
            return compatible
    return available[0]


def _language_matches(candidate: str, requested: str, *, exact_only: bool) -> bool:
    candidate_key = _language_key(candidate)
    requested_key = _language_key(requested)
    if candidate_key == requested_key:
        return True
    if exact_only:
        return False
    if requested_key == "zh-hans":
        return candidate_key in {"zh", "zh-cn", "zh-sg", "zh-chs"}
    if requested_key == "zh-hant":
        return candidate_key in {"zh", "zh-tw", "zh-hk", "zh-mo", "zh-cht"}
    return candidate_key.split("-", 1)[0] == requested_key.split("-", 1)[0]


def _language_key(language: str) -> str:
    return str(language).strip().replace("_", "-").lower()
