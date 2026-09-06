"""Transcript acquisition policy: captions first, then OCR, then speech recognition."""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from platformdirs import user_cache_dir

from .asr import ASRBackend, ASRResult, MLXWhisperASR
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
    MacVisionOCR,
    OCRFrame,
    TimedOCRFrame,
    normalize_caption,
    subtitle_segments_from_frames,
    vision_language_codes,
)
from .progress import ProgressEvent, ProgressSink, Stage
from .runtime import ResourceGates
from .sources import SourceClient, SourceResult


class TranscriptAcquisitionError(RuntimeError):
    """A recognized source could not produce a transcript in the requested mode."""


class SourceProvider(Protocol):
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


class OCRProvider(Protocol):
    async def recognize_images_async(
        self,
        image_paths: Sequence[str | Path],
        *,
        languages: Sequence[str] = (),
        accurate: bool = True,
        minimum_text_height: float = 0.012,
    ) -> tuple[OCRFrame, ...]: ...


ASRFactory = Callable[[str], ASRBackend]


def _default_asr_factory(model: str) -> ASRBackend:
    return MLXWhisperASR(model)


class TranscriptPipeline:
    """Turn one supported URL into a normalized transcript.

    Platform captions are authoritative when present. With no caption track,
    automatic mode tries Apple Vision on sampled subtitle frames and only then
    falls back to local MLX Whisper. All temporary media is deleted when the
    acquisition attempt finishes.
    """

    def __init__(
        self,
        *,
        sources: SourceProvider | None = None,
        media: MediaProvider | None = None,
        frames: FrameProvider | None = None,
        ocr: OCRProvider | None = None,
        asr_factory: ASRFactory | None = None,
        temp_root: Path | None = None,
        ocr_batch_size: int = 32,
    ) -> None:
        if ocr_batch_size < 1:
            raise ValueError("ocr_batch_size must be at least one")
        self._sources = sources or SourceClient()
        self._media = media or MediaDownloader()
        self._frames = frames or FrameSampler()
        self._ocr = ocr or MacVisionOCR()
        self._asr_factory = asr_factory or _default_asr_factory
        self._temp_root = Path(temp_root).expanduser() if temp_root else None
        self._ocr_batch_size = int(ocr_batch_size)
        self._asr_backends: dict[str, ASRBackend] = {}

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
                warnings=source.warnings,
            )
        if options.mode is ProcessingMode.CAPTIONS:
            raise TranscriptAcquisitionError("no platform caption track is available")

        work_root = self._temporary_root()
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="task-", dir=work_root) as directory:
            workspace = Path(directory)
            if options.mode is ProcessingMode.WHISPER:
                media_path = await self._download(
                    url,
                    workspace,
                    MediaPurpose.AUDIO,
                    gates,
                    progress,
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
                video_path = await self._download(
                    url,
                    workspace,
                    MediaPurpose.ANALYSIS_VIDEO,
                    gates,
                    progress,
                )
                ocr_segments, ocr_warnings = await self._read_burned_captions(
                    url,
                    source.metadata,
                    video_path,
                    workspace / "frames",
                    options,
                    gates,
                    progress,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ocr_warnings = (f"Apple Vision OCR was unavailable or failed: {exc}",)
                if options.mode is ProcessingMode.OCR:
                    raise TranscriptAcquisitionError(ocr_warnings[0]) from exc

            usable_ocr = _ocr_is_usable(ocr_segments, source.metadata.duration_seconds)
            if options.mode is ProcessingMode.OCR:
                if not usable_ocr:
                    raise TranscriptAcquisitionError(
                        "Apple Vision OCR found no usable burned-in captions"
                    )
                return _ocr_transcript(
                    source.metadata,
                    options.language,
                    ocr_segments,
                    warnings=(*source.warnings, *ocr_warnings),
                )

            if options.mode is ProcessingMode.AUTO and usable_ocr:
                return _ocr_transcript(
                    source.metadata,
                    options.language,
                    ocr_segments,
                    warnings=(*source.warnings, *ocr_warnings),
                )

            if video_path is None:
                video_path = await self._download(
                    url,
                    workspace,
                    MediaPurpose.AUDIO,
                    gates,
                    progress,
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
                "Apple Vision OCR found no usable burned-in captions; used Whisper.",
            )
            return Transcript(
                metadata=source.metadata,
                language=asr_result.language,
                method=TranscriptMethod.MLX_WHISPER,
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
        workspace: Path,
        purpose: MediaPurpose,
        gates: ResourceGates,
        progress: ProgressSink,
    ) -> Path:
        label = "audio" if purpose is MediaPurpose.AUDIO else "analysis video"
        await progress(ProgressEvent(url, Stage.DOWNLOAD, f"Downloading temporary {label}"))
        async with gates.network:
            return await self._media.download(url, workspace / "media", purpose)

    async def _read_burned_captions(
        self,
        url: str,
        metadata: SourceMetadata,
        video_path: Path,
        frame_directory: Path,
        options: TaskOptions,
        gates: ResourceGates,
        progress: ProgressSink,
    ) -> tuple[tuple[TranscriptSegment, ...], tuple[str, ...]]:
        await progress(ProgressEvent(url, Stage.OCR, "Sampling subtitle frames"))
        recognized: list[TimedOCRFrame] = []
        failed_frames = 0
        async with gates.ocr:
            sampled = await self._frames.sample(
                video_path,
                frame_directory,
                metadata.duration_seconds,
            )
            if not sampled:
                return (), ()
            languages = _ocr_languages(options.language, metadata)
            for start in range(0, len(sampled), self._ocr_batch_size):
                batch = sampled[start : start + self._ocr_batch_size]
                fraction = min(1.0, start / len(sampled))
                await progress(
                    ProgressEvent(url, Stage.OCR, "Recognizing burned-in captions", fraction)
                )
                frames = await self._ocr.recognize_images_async(
                    [item.path for item in batch],
                    languages=languages,
                )
                if len(frames) != len(batch):
                    raise TranscriptAcquisitionError(
                        "Apple Vision OCR returned an unexpected frame count"
                    )
                failed_frames += sum(frame.error is not None for frame in frames)
                recognized.extend(
                    TimedOCRFrame(item.timestamp_seconds, frame)
                    for item, frame in zip(batch, frames)
                )
                for item in batch:
                    item.path.unlink(missing_ok=True)
        failure_ratio = failed_frames / len(sampled)
        if failure_ratio > 0.1:
            raise TranscriptAcquisitionError(
                f"Apple Vision OCR failed on {failed_frames} of {len(sampled)} frames"
            )
        warnings = (
            (f"Apple Vision OCR could not read {failed_frames} sampled frames.",)
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
            method=TranscriptMethod.MLX_WHISPER,
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
        await progress(ProgressEvent(url, Stage.WHISPER, f"Transcribing with Whisper ({model})"))
        async with gates.asr:
            result = await asyncio.to_thread(
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


def _ocr_languages(language: str, metadata: SourceMetadata) -> tuple[str, ...]:
    explicit = vision_language_codes(language)
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
    characters = sum(len(normalize_caption(segment.text)) for segment in segments)
    if not duration_seconds or duration_seconds <= 30:
        return characters >= 4
    minimum_characters = max(60, round(duration_seconds / 60 * 15))
    coverage = (segments[-1].end_seconds - segments[0].start_seconds) / duration_seconds
    return (
        len(segments) >= 5
        and characters >= minimum_characters
        and coverage >= 0.4
    )


def _ocr_transcript(
    metadata: SourceMetadata,
    requested_language: str,
    segments: tuple[TranscriptSegment, ...],
    *,
    warnings: tuple[str, ...] = (),
) -> Transcript:
    language = _detected_ocr_language(requested_language, metadata, segments)
    return Transcript(
        metadata=metadata,
        language=language,
        method=TranscriptMethod.APPLE_VISION_OCR,
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


def merge_ocr_and_asr(
    ocr_segments: Sequence[TranscriptSegment],
    asr_segments: Sequence[TranscriptSegment],
) -> tuple[TranscriptSegment, ...]:
    """Prefer visible captions and use speech segments only in uncovered gaps."""

    merged = list(ocr_segments)
    for speech in asr_segments:
        midpoint = (speech.start_seconds + speech.end_seconds) / 2
        covered = any(
            visible.start_seconds - 0.5 <= midpoint <= visible.end_seconds + 0.5
            for visible in ocr_segments
        )
        if not covered:
            merged.append(speech)
    merged.sort(key=lambda segment: (segment.start_seconds, segment.end_seconds))
    return tuple(merged)
