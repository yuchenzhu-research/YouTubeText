"""Transcript acquisition policy: captions first, then OCR, then speech recognition."""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from platformdirs import user_cache_dir

from .asr import ASRBackend, ASRResult
from .backends import (
    ASRFactory,
    LocalBackends,
    OCRLanguageResolver,
    OCRProvider,
    default_local_backends,
)
from .domain import (
    ProcessingMode,
    SourceMetadata,
    TaskOptions,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from .media import FrameSampler, MediaDownloader, MediaPurpose, SampledFrame
from .ocr import (
    OCRFrame,
    normalize_caption,
    subtitle_segments_from_frames,
    vision_language_codes,
)
from .planning import ProcessingPlan, build_processing_plan
from .progress import ProgressEvent, ProgressSink, Stage
from .resume import EphemeralResumeStore, OCRRecipe, ResumeSession, ResumeStore
from .runtime import ResourceGates, run_blocking
from .sources import SourceClient, SourceInspection, SourceResult, YtDlpAuth


class TranscriptAcquisitionError(RuntimeError):
    """A recognized source could not produce a transcript in the requested mode."""


class SourceProvider(Protocol):
    def inspect(
        self,
        url: str,
        *,
        preferred_languages: Sequence[str] = (),
    ) -> SourceInspection: ...

    def fetch(
        self,
        url: str,
        *,
        preferred_languages: Sequence[str] = (),
        include_subtitles: bool = True,
        strict_subtitles: bool = True,
    ) -> SourceResult: ...


class MediaProvider(Protocol):
    async def download(
        self,
        url: str,
        directory: Path,
        purpose: MediaPurpose,
    ) -> Path: ...


class FrameProvider(Protocol):
    async def sample(
        self,
        video_path: Path,
        output_dir: Path,
        duration_seconds: float,
    ) -> tuple[SampledFrame, ...]: ...


class TranscriptPipeline:
    """Turn one supported URL into a normalized transcript.

    Platform captions are authoritative when present. With no caption track,
    automatic mode tries local OCR on sampled subtitle frames and only then
    falls back to local Whisper. Disposable runs delete temporary media;
    resumable runs preserve incomplete media and remove it after success.
    """

    def __init__(
        self,
        *,
        sources: SourceProvider | None = None,
        media: MediaProvider | None = None,
        frames: FrameProvider | None = None,
        ocr: OCRProvider | None = None,
        asr_factory: ASRFactory | None = None,
        backends: LocalBackends | None = None,
        auth: YtDlpAuth | None = None,
        resume_store: ResumeStore | None = None,
        temp_root: Path | None = None,
        ocr_batch_size: int = 32,
    ) -> None:
        if ocr_batch_size < 1:
            raise ValueError("ocr_batch_size must be at least one")
        selected_backends = backends
        if selected_backends is None and (ocr is None or asr_factory is None):
            selected_backends = default_local_backends()
        self._sources = sources or SourceClient(auth=auth)
        self._media = media or MediaDownloader(auth=auth)
        self._frames = frames or FrameSampler()
        self._ocr = ocr or _required_backends(selected_backends).ocr
        self._asr_factory = (
            asr_factory or _required_backends(selected_backends).asr_factory
        )
        if selected_backends is None:
            self._ocr_label = "Apple Vision OCR"
            self._asr_label = "Whisper"
            self._ocr_method = TranscriptMethod.APPLE_VISION_OCR
            self._asr_method = TranscriptMethod.MLX_WHISPER
            self._ocr_language_codes = vision_language_codes
        else:
            self._ocr_label = selected_backends.ocr_label
            self._asr_label = selected_backends.asr_label
            self._ocr_method = selected_backends.ocr_method
            self._asr_method = selected_backends.asr_method
            self._ocr_language_codes = selected_backends.ocr_language_codes
        self._temp_root = Path(temp_root).expanduser() if temp_root else None
        self._resume = resume_store or EphemeralResumeStore(self._temporary_root())
        self._ocr_batch_size = int(ocr_batch_size)
        self._asr_backends: dict[str, ASRBackend] = {}

    async def plan(
        self,
        url: str,
        options: TaskOptions,
        gates: ResourceGates,
    ) -> ProcessingPlan:
        """Inspect one source and describe work without starting that work."""

        async with gates.network:
            inspection = await run_blocking(
                self._sources.inspect,
                url,
                preferred_languages=_caption_languages(options),
            )
        return build_processing_plan(inspection, options.mode)

    async def acquire(
        self,
        url: str,
        options: TaskOptions,
        gates: ResourceGates,
        progress: ProgressSink,
    ) -> Transcript:
        await progress(ProgressEvent(url, Stage.IDENTIFY, "Identifying video source"))
        caption_languages = _caption_languages(options)
        include_subtitles = options.mode in {
            ProcessingMode.AUTO,
            ProcessingMode.CAPTIONS,
        }
        async with gates.network:
            source = await asyncio.to_thread(
                self._sources.fetch,
                url,
                preferred_languages=caption_languages,
                include_subtitles=include_subtitles,
                strict_subtitles=options.mode is ProcessingMode.CAPTIONS,
            )
        await progress(
            ProgressEvent(
                url,
                Stage.METADATA,
                f"Read {source.metadata.platform} metadata: {source.metadata.title}",
            )
        )

        use_platform_caption = options.mode in {
            ProcessingMode.AUTO,
            ProcessingMode.CAPTIONS,
        }
        if source.subtitle is not None and use_platform_caption:
            cleanup_warning = await self._resume.discard_incomplete(
                source.metadata,
                options,
            )
            if cleanup_warning:
                await progress(ProgressEvent(url, Stage.CACHE, cleanup_warning))
            await progress(
                ProgressEvent(
                    url,
                    Stage.CAPTIONS,
                    f"Using {source.subtitle.kind.value} {source.subtitle.language} captions",
                    1.0,
                )
            )
            return Transcript(
                metadata=source.metadata,
                language=source.subtitle.language,
                method=TranscriptMethod.PLATFORM_CAPTIONS,
                segments=source.subtitle.segments,
                warnings=tuple(
                    dict.fromkeys(
                        (
                            *source.warnings,
                            *((cleanup_warning,) if cleanup_warning else ()),
                        )
                    )
                ),
            )
        if options.mode is ProcessingMode.CAPTIONS:
            raise TranscriptAcquisitionError("no platform caption track is available")

        resumed = await self._resume.run(
            source.metadata,
            options,
            lambda session: self._acquire_fallback(
                url,
                source,
                options,
                gates,
                progress,
                session,
            ),
        )
        if resumed.reused:
            await progress(
                ProgressEvent(url, Stage.CACHE, "Reusing completed local transcript", 1.0)
            )
        if resumed.warning:
            await progress(ProgressEvent(url, Stage.CACHE, resumed.warning))
        warnings = tuple(
            dict.fromkeys(
                (
                    *source.warnings,
                    *resumed.transcript.warnings,
                    *((resumed.warning,) if resumed.warning else ()),
                )
            )
        )
        return replace(
            resumed.transcript,
            metadata=source.metadata,
            warnings=warnings,
        )

    async def _acquire_fallback(
        self,
        url: str,
        source: SourceResult,
        options: TaskOptions,
        gates: ResourceGates,
        progress: ProgressSink,
        session: ResumeSession,
    ) -> Transcript:
        if options.mode is ProcessingMode.WHISPER:
            media_path = await session.media(
                MediaPurpose.AUDIO,
                lambda directory: self._download(
                    url,
                    directory,
                    MediaPurpose.AUDIO,
                    gates,
                    progress,
                ),
            )
            return await self._whisper_transcript(
                source.metadata,
                media_path,
                options,
                gates,
                progress,
            )

        video_path: Path | None = None
        ocr_segments: tuple[TranscriptSegment, ...] = ()
        ocr_warnings: tuple[str, ...] = ()
        try:
            video_path = await session.media(
                MediaPurpose.ANALYSIS_VIDEO,
                lambda directory: self._download(
                    url,
                    directory,
                    MediaPurpose.ANALYSIS_VIDEO,
                    gates,
                    progress,
                ),
            )
            frame_root = self._temporary_root()
            frame_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="frames-",
                dir=frame_root,
            ) as frame_directory:
                ocr_segments, ocr_warnings = await self._read_burned_captions(
                    url,
                    source.metadata,
                    video_path,
                    Path(frame_directory),
                    options,
                    gates,
                    progress,
                    session,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ocr_warnings = (
                f"{self._ocr_label} was unavailable or failed: {exc}",
            )
            if options.mode is ProcessingMode.OCR:
                raise TranscriptAcquisitionError(ocr_warnings[0]) from exc

        usable_ocr = _ocr_is_usable(ocr_segments, source.metadata.duration_seconds)
        if options.mode is ProcessingMode.OCR:
            if not usable_ocr:
                raise TranscriptAcquisitionError(
                    f"{self._ocr_label} found no usable burned-in captions"
                )
            return _ocr_transcript(
                source.metadata,
                options.language,
                ocr_segments,
                method=self._ocr_method,
                warnings=(*source.warnings, *ocr_warnings),
            )

        if options.mode is ProcessingMode.AUTO and usable_ocr:
            return _ocr_transcript(
                source.metadata,
                options.language,
                ocr_segments,
                method=self._ocr_method,
                warnings=(*source.warnings, *ocr_warnings),
            )

        if video_path is None:
            video_path = await session.media(
                MediaPurpose.AUDIO,
                lambda directory: self._download(
                    url,
                    directory,
                    MediaPurpose.AUDIO,
                    gates,
                    progress,
                ),
            )
        asr_result = await self._run_whisper(
            url,
            video_path,
            options,
            gates,
            progress,
        )

        if options.mode is ProcessingMode.HYBRID and usable_ocr:
            await progress(
                ProgressEvent(url, Stage.MERGE, "Merging OCR and speech transcript")
            )
            merged = merge_ocr_and_asr(ocr_segments, asr_result.segments)
            return Transcript(
                metadata=source.metadata,
                language=_resolved_language(options.language, asr_result.language),
                method=TranscriptMethod.OCR_WHISPER,
                segments=merged,
                warnings=(*source.warnings, *ocr_warnings),
            )

        warnings = (
            *source.warnings,
            *ocr_warnings,
            f"{self._ocr_label} found no usable burned-in captions; "
            f"used {self._asr_label}.",
        )
        return Transcript(
            metadata=source.metadata,
            language=asr_result.language,
            method=self._asr_method,
            segments=asr_result.segments,
            warnings=warnings,
        )

    def _temporary_root(self) -> Path:
        if self._temp_root is not None:
            return self._temp_root
        return Path(user_cache_dir("YouTubeText")) / "work"

    async def _download(
        self,
        url: str,
        media_directory: Path,
        purpose: MediaPurpose,
        gates: ResourceGates,
        progress: ProgressSink,
    ) -> Path:
        label = "audio" if purpose is MediaPurpose.AUDIO else "analysis video"
        await progress(ProgressEvent(url, Stage.DOWNLOAD, f"Downloading temporary {label}"))
        async with gates.network:
            return await self._media.download(url, media_directory, purpose)

    async def _read_burned_captions(
        self,
        url: str,
        metadata: SourceMetadata,
        video_path: Path,
        frame_directory: Path,
        options: TaskOptions,
        gates: ResourceGates,
        progress: ProgressSink,
        session: ResumeSession,
    ) -> tuple[tuple[TranscriptSegment, ...], tuple[str, ...]]:
        await progress(ProgressEvent(url, Stage.OCR, "Sampling subtitle frames"))
        async with gates.ocr:
            sampled = await self._frames.sample(
                video_path,
                frame_directory,
                metadata.duration_seconds,
            )
            if not sampled:
                return (), ()
            languages = _ocr_languages(
                options.language,
                metadata,
                self._ocr_language_codes,
            )
            engine_revision = await run_blocking(
                _ocr_checkpoint_revision,
                self._ocr,
            )
            recipe = OCRRecipe(
                languages=languages,
                batch_size=self._ocr_batch_size,
                engine_revision=engine_revision,
            )

            async def report_batch(start: int, total: int) -> None:
                fraction = min(1.0, start / total)
                await progress(
                    ProgressEvent(url, Stage.OCR, "Recognizing burned-in captions", fraction)
                )

            async def recognize_batch(
                paths: Sequence[Path],
                active_recipe: OCRRecipe,
            ) -> tuple[OCRFrame, ...]:
                return await self._ocr.recognize_images_async(
                    paths,
                    languages=active_recipe.languages,
                    accurate=active_recipe.accurate,
                    minimum_text_height=active_recipe.minimum_text_height,
                )

            recognized = await session.ocr(
                sampled,
                recipe,
                recognize_batch,
                report_batch,
            )
        failed_frames = sum(item.frame.error is not None for item in recognized)
        failure_ratio = failed_frames / len(sampled)
        if failure_ratio > 0.1:
            raise TranscriptAcquisitionError(
                f"{self._ocr_label} failed on {failed_frames} of "
                f"{len(sampled)} frames"
            )
        warnings = (
            (
                f"{self._ocr_label} could not read {failed_frames} "
                "sampled frames.",
            )
            if failed_frames
            else ()
        )
        await progress(ProgressEvent(url, Stage.OCR, "Cleaned OCR captions", 1.0))
        segments = subtitle_segments_from_frames(
            recognized,
            frame_duration_seconds=_frame_duration(sampled),
        )
        return segments, warnings

    async def _whisper_transcript(
        self,
        metadata: SourceMetadata,
        media_path: Path,
        options: TaskOptions,
        gates: ResourceGates,
        progress: ProgressSink,
    ) -> Transcript:
        result = await self._run_whisper(
            metadata.url,
            media_path,
            options,
            gates,
            progress,
        )
        return Transcript(
            metadata=metadata,
            language=result.language,
            method=self._asr_method,
            segments=result.segments,
        )

    async def _run_whisper(
        self,
        url: str,
        media_path: Path,
        options: TaskOptions,
        gates: ResourceGates,
        progress: ProgressSink,
    ) -> ASRResult:
        model = options.whisper_model
        backend = self._asr_backends.get(model)
        if backend is None:
            backend = self._asr_factory(model)
            self._asr_backends[model] = backend
        await progress(
            ProgressEvent(
                url,
                Stage.WHISPER,
                f"Transcribing with {self._asr_label} ({model})",
            )
        )
        async with gates.asr:
            result = await run_blocking(
                backend.transcribe,
                media_path,
                language=_whisper_language(options.language),
            )
        await progress(ProgressEvent(url, Stage.WHISPER, "Speech transcription complete", 1.0))
        return result


def _caption_languages(options: TaskOptions) -> tuple[str, ...]:
    if options.preferred_caption_languages:
        return options.preferred_caption_languages
    if options.language.strip().lower() != "auto":
        return (options.language,)
    return ()


def _whisper_language(language: str) -> str:
    normalized = (language or "auto").strip().replace("_", "-").lower()
    if normalized == "auto":
        return "auto"
    return normalized.split("-", 1)[0]


def _ocr_checkpoint_revision(provider: object) -> str:
    revision = getattr(provider, "checkpoint_revision", "")
    if callable(revision):
        revision = revision()
    if isinstance(revision, str) and revision.strip():
        return revision.strip()
    provider_type = type(provider)
    return f"python-provider:{provider_type.__module__}.{provider_type.__qualname__}"


def _ocr_languages(
    language: str,
    metadata: SourceMetadata,
    language_codes: OCRLanguageResolver = vision_language_codes,
) -> tuple[str, ...]:
    explicit = language_codes(language)
    if explicit:
        return explicit
    context = f"{metadata.title} {metadata.author}"
    has_cjk = any("\u3400" <= character <= "\u9fff" for character in context)
    script = _detected_ocr_language("auto", metadata, ())
    if script == "zh-Hant":
        return ("zh-Hant", "zh-Hans", "en-US", "es-ES")
    if script == "zh-Hans" or metadata.platform == "bilibili":
        return ("zh-Hans", "zh-Hant", "en-US", "es-ES")
    if has_cjk:
        return ("zh-Hant", "zh-Hans", "en-US", "es-ES")
    return ("en-US", "zh-Hant", "zh-Hans", "es-ES")


def _resolved_language(requested: str, detected: str) -> str:
    return detected if requested.strip().lower() == "auto" else requested


def _frame_duration(frames: Sequence[SampledFrame]) -> float:
    differences = [
        current.timestamp_seconds - previous.timestamp_seconds
        for previous, current in zip(frames, frames[1:])
        if current.timestamp_seconds > previous.timestamp_seconds
    ]
    return min(differences) if differences else 1.0


def _ocr_is_usable(
    segments: Sequence[TranscriptSegment],
    duration_seconds: float,
) -> bool:
    if not segments:
        return False
    normalized = [normalize_caption(segment.text) for segment in segments]
    characters = sum(len(text) for text in normalized)
    duration = max(0.0, float(duration_seconds or 0.0))
    # Screen recordings can yield continuously changing UI text from the
    # subtitle crop. Coverage and uniqueness alone mistake it for speech.
    # Sustained text above a plausible caption reading rate is not a usable
    # transcript, so auto/hybrid must fall back to the audio track.
    if duration and characters / duration > 30:
        return False
    if _looks_like_repeated_screen_label(normalized):
        return False
    if not duration or duration <= 10:
        return characters >= 4
    covered_ratio = _covered_duration(segments, duration) / duration
    if duration <= 30:
        return (
            len(segments) >= 2
            and len(set(normalized)) >= 2
            and characters >= 12
            and covered_ratio >= 0.2
        )
    minimum_characters = max(60, round(duration_seconds / 60 * 15))
    unique_ratio = len(set(normalized)) / len(normalized)
    return (
        len(segments) >= 5
        and characters >= minimum_characters
        and unique_ratio >= 0.4
        and covered_ratio >= 0.35
    )


def _looks_like_repeated_screen_label(texts: Sequence[str]) -> bool:
    """Reject short changing suffixes on an otherwise fixed screen label.

    A counter or slide index can make every OCR segment unique while still
    containing almost no spoken content. This is deliberately a narrow signal:
    ordinary captions with a shared speaker label and varied sentences pass.
    """

    if len(texts) < 5:
        return False
    prefix = texts[0]
    for value in texts[1:]:
        matching = 0
        for left, right in zip(prefix, value):
            if left != right:
                break
            matching += 1
        prefix = prefix[:matching]
        if len(prefix) < 6:
            return False
    return all(
        len(prefix) / len(value) >= 0.7 and len(value) - len(prefix) <= 6
        for value in texts
    )


def _covered_duration(
    segments: Sequence[TranscriptSegment],
    duration_seconds: float,
) -> float:
    intervals = sorted(
        (
            max(0.0, segment.start_seconds),
            min(duration_seconds, segment.end_seconds),
        )
        for segment in segments
        if segment.start_seconds < duration_seconds and segment.end_seconds > 0
    )
    total = 0.0
    active_start = 0.0
    active_end = 0.0
    for start, end in intervals:
        if end <= start:
            continue
        if active_end <= active_start:
            active_start, active_end = start, end
        elif start <= active_end:
            active_end = max(active_end, end)
        else:
            total += active_end - active_start
            active_start, active_end = start, end
    if active_end > active_start:
        total += active_end - active_start
    return total


def _ocr_transcript(
    metadata: SourceMetadata,
    requested_language: str,
    segments: tuple[TranscriptSegment, ...],
    *,
    method: TranscriptMethod = TranscriptMethod.APPLE_VISION_OCR,
    warnings: tuple[str, ...] = (),
) -> Transcript:
    language = _detected_ocr_language(requested_language, metadata, segments)
    return Transcript(
        metadata=metadata,
        language=language,
        method=method,
        segments=segments,
        warnings=warnings,
    )


def _detected_ocr_language(
    requested_language: str,
    metadata: SourceMetadata,
    segments: Sequence[TranscriptSegment],
) -> str:
    if requested_language.strip().lower() != "auto":
        return requested_language

    text = f"{metadata.title} {metadata.author} " + " ".join(
        segment.text for segment in segments
    )
    if not any("\u3400" <= character <= "\u9fff" for character in text):
        return "und"

    pairs = {
        "爱": "愛",
        "边": "邊",
        "变": "變",
        "场": "場",
        "处": "處",
        "发": "發",
        "个": "個",
        "国": "國",
        "还": "還",
        "后": "後",
        "会": "會",
        "仅": "僅",
        "开": "開",
        "来": "來",
        "里": "裡",
        "领": "領",
        "门": "門",
        "区": "區",
        "让": "讓",
        "时": "時",
        "说": "說",
        "台": "臺",
        "体": "體",
        "万": "萬",
        "为": "為",
        "现": "現",
        "学": "學",
        "亿": "億",
        "应": "應",
        "与": "與",
        "长": "長",
        "这": "這",
        "种": "種",
    }
    simplified_score = sum(text.count(character) for character in pairs)
    traditional_score = sum(text.count(character) for character in pairs.values())
    if traditional_score > simplified_score:
        return "zh-Hant"
    if simplified_score > traditional_score:
        return "zh-Hans"
    return "zh"


def _required_backends(backends: LocalBackends | None) -> LocalBackends:
    if backends is None:
        raise RuntimeError("local model backends were not configured")
    return backends


def merge_ocr_and_asr(
    ocr_segments: Sequence[TranscriptSegment],
    asr_segments: Sequence[TranscriptSegment],
) -> tuple[TranscriptSegment, ...]:
    """Prefer matching visible captions without losing different spoken content."""

    merged = list(ocr_segments)
    for speech in asr_segments:
        duration = speech.end_seconds - speech.start_seconds
        overlaps = tuple(
            TranscriptSegment(
                max(speech.start_seconds, visible.start_seconds),
                min(speech.end_seconds, visible.end_seconds),
                visible.text,
            )
            for visible in ocr_segments
            if max(speech.start_seconds, visible.start_seconds)
            < min(speech.end_seconds, visible.end_seconds)
        )
        covered = _covered_duration(overlaps, speech.end_seconds)
        coverage = covered / duration if duration > 0 else 0.0
        visible_texts: list[str] = []
        previous_normalized = ""
        for visible in sorted(
            overlaps,
            key=lambda segment: (segment.start_seconds, segment.end_seconds),
        ):
            normalized = normalize_caption(visible.text)
            if normalized != previous_normalized:
                visible_texts.append(visible.text)
                previous_normalized = normalized
        visible_text = " ".join(visible_texts)
        # Similar wording can differ on a negation or number; preserve speech
        # unless the covered visible text is the same after normalization.
        if coverage < 0.8 or normalize_caption(speech.text) != normalize_caption(
            visible_text
        ):
            merged.append(speech)
    merged.sort(key=lambda segment: (segment.start_seconds, segment.end_seconds))
    return tuple(merged)
