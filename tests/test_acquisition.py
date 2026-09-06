from __future__ import annotations

from pathlib import Path

import pytest

from youtubetext.acquisition import (
    TranscriptAcquisitionError,
    TranscriptPipeline,
    _detected_ocr_language,
    _ocr_languages,
)
from youtubetext.asr import ASRResult
from youtubetext.domain import (
    ProcessingMode,
    SourceMetadata,
    TaskOptions,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.media import MediaPurpose, SampledFrame
from youtubetext.ocr import BoundingBox, OCRFrame, OCRObservation
from youtubetext.runtime import CapacityPlan, HostProfile, ResourceGates
from youtubetext.sources import SourceResult, SubtitleKind, SubtitleTrack


URL = "https://www.youtube.com/watch?v=video"
META = SourceMetadata(URL, "youtube", "video", "文钊测试视频", duration_seconds=120)


class FakeSources:
    def __init__(self, subtitle: SubtitleTrack | None = None):
        self.subtitle = subtitle
        self.languages = ()
        self.include_subtitles = None

    def fetch(self, url, *, preferred_languages=(), include_subtitles=True):
        self.languages = tuple(preferred_languages)
        self.include_subtitles = include_subtitles
        return SourceResult(META, self.subtitle)


class FakeMedia:
    def __init__(self):
        self.purposes = []
        self.paths = []

    async def download(self, url, directory, purpose):
        self.purposes.append(purpose)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("audio.m4a" if purpose is MediaPurpose.AUDIO else "video.mp4")
        path.write_bytes(b"temporary")
        self.paths.append(path)
        return path


class FakeFrames:
    async def sample(self, video_path, output_dir, duration_seconds):
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index in range(6):
            path = output_dir / f"{index}.jpg"
            path.write_bytes(b"frame")
            frames.append(SampledFrame(path, index * duration_seconds / 6))
        return tuple(frames)


class FakeOCR:
    def __init__(self, texts=()):
        self.texts = tuple(texts)
        self.languages = ()
        self.offset = 0

    async def recognize_images_async(self, image_paths, *, languages=(), **_options):
        self.languages = tuple(languages)
        result = []
        for index, path in enumerate(image_paths):
            position = self.offset + index
            text = self.texts[position] if position < len(self.texts) else ""
            observations = ()
            if text:
                observations = (
                    OCRObservation(text, 0.95, BoundingBox(0.1, 0.2, 0.8, 0.08)),
                )
            result.append(OCRFrame(str(path), observations))
        self.offset += len(image_paths)
        return tuple(result)


class FakeASR:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, *, language="auto"):
        self.calls.append((Path(path).name, language))
        return ASRResult(
            language="zh",
            model="small",
            segments=(TranscriptSegment(110, 112, "语音识别结果"),),
        )


def gates():
    host = HostProfile("Darwin", "arm64", 16 * 1024**3, 8)
    return ResourceGates(CapacityPlan.for_host(host))


async def no_progress(_event):
    return None


def pipeline(tmp_path, *, subtitle=None, ocr_texts=()):
    sources = FakeSources(subtitle)
    media = FakeMedia()
    ocr = FakeOCR(ocr_texts)
    asr = FakeASR()
    instance = TranscriptPipeline(
        sources=sources,
        media=media,
        frames=FakeFrames(),
        ocr=ocr,
        asr_factory=lambda _model: asr,
        temp_root=tmp_path,
        ocr_batch_size=2,
    )
    return instance, sources, media, ocr, asr


@pytest.mark.asyncio
async def test_platform_caption_short_circuits_media_and_models(tmp_path):
    track = SubtitleTrack(
        "en",
        SubtitleKind.MANUAL,
        (TranscriptSegment(0, 2, "Platform text"),),
    )
    instance, sources, media, _ocr, asr = pipeline(tmp_path, subtitle=track)

    result = await instance.acquire(URL, TaskOptions(), gates(), no_progress)

    assert result.method is TranscriptMethod.PLATFORM_CAPTIONS
    assert result.text == "Platform text"
    assert media.purposes == []
    assert asr.calls == []
    assert sources.languages == ()
    assert sources.include_subtitles is True


@pytest.mark.asyncio
async def test_caption_only_mode_reports_missing_track(tmp_path):
    instance, *_ = pipeline(tmp_path)
    with pytest.raises(TranscriptAcquisitionError, match="no platform caption"):
        await instance.acquire(
            URL,
            TaskOptions(mode=ProcessingMode.CAPTIONS),
            gates(),
            no_progress,
        )


@pytest.mark.asyncio
async def test_auto_mode_uses_burned_in_captions_before_whisper(tmp_path):
    instance, sources, media, ocr, asr = pipeline(
        tmp_path,
        ocr_texts=(
            "国际局势正在发生一系列深刻变化",
            "美联储政策仍然牵动全球资本市场",
            "欧洲各国面对新的安全经济压力",
            "北京近期释放出若干重要政策信号",
            "投资者需要区分短期波动长期趋势",
            "下面我们继续观察事件如何演化",
        ),
    )
    options = TaskOptions(language="zh-Hant")

    result = await instance.acquire(URL, options, gates(), no_progress)

    assert result.method is TranscriptMethod.APPLE_VISION_OCR
    assert result.language == "zh-Hant"
    assert media.purposes == [MediaPurpose.ANALYSIS_VIDEO]
    assert asr.calls == []
    assert sources.languages == ("zh-Hant",)
    assert ocr.languages == ("zh-Hant",)


@pytest.mark.asyncio
async def test_auto_mode_falls_back_to_whisper_using_downloaded_video(tmp_path):
    instance, _sources, media, _ocr, asr = pipeline(tmp_path, ocr_texts=("logo",) * 6)

    result = await instance.acquire(URL, TaskOptions(), gates(), no_progress)

    assert result.method is TranscriptMethod.MLX_WHISPER
    assert result.text == "语音识别结果"
    assert media.purposes == [MediaPurpose.ANALYSIS_VIDEO]
    assert asr.calls == [("video.mp4", "auto")]
    assert "used Whisper" in result.warnings[-1]
    assert not media.paths[0].exists()


@pytest.mark.asyncio
async def test_forced_whisper_downloads_audio_and_normalizes_language(tmp_path):
    instance, sources, media, _ocr, asr = pipeline(tmp_path)

    result = await instance.acquire(
        URL,
        TaskOptions(mode=ProcessingMode.WHISPER, language="zh-Hans"),
        gates(),
        no_progress,
    )

    assert result.method is TranscriptMethod.MLX_WHISPER
    assert media.purposes == [MediaPurpose.AUDIO]
    assert asr.calls == [("audio.m4a", "zh")]
    assert sources.include_subtitles is False


@pytest.mark.asyncio
async def test_forced_ocr_does_not_silently_switch_to_whisper(tmp_path):
    instance, _sources, _media, _ocr, asr = pipeline(tmp_path)

    with pytest.raises(TranscriptAcquisitionError, match="no usable"):
        await instance.acquire(
            URL,
            TaskOptions(mode=ProcessingMode.OCR),
            gates(),
            no_progress,
        )
    assert asr.calls == []


@pytest.mark.asyncio
async def test_hybrid_prefers_ocr_and_fills_uncovered_time_with_asr(tmp_path):
    advertised_caption = SubtitleTrack(
        "en",
        SubtitleKind.MANUAL,
        (TranscriptSegment(0, 120, "This forced mode must ignore me"),),
    )
    instance, *_ = pipeline(
        tmp_path,
        subtitle=advertised_caption,
        ocr_texts=(
            "国际局势正在发生一系列深刻变化",
            "美联储政策仍然牵动全球资本市场",
            "欧洲各国面对新的安全经济压力",
            "北京近期释放出若干重要政策信号",
            "投资者需要区分短期波动长期趋势",
            "",
        ),
    )

    result = await instance.acquire(
        URL,
        TaskOptions(mode=ProcessingMode.HYBRID),
        gates(),
        no_progress,
    )

    assert result.method is TranscriptMethod.OCR_WHISPER
    assert result.language == "zh"
    assert result.segments[-1].text == "语音识别结果"


def test_auto_ocr_language_prioritizes_traditional_chinese_for_cjk_metadata():
    assert _ocr_languages("auto", META) == (
        "zh-Hant",
        "zh-Hans",
        "en-US",
        "es-ES",
    )

    simplified_bilibili = SourceMetadata(
        "https://www.bilibili.com/video/BV1test",
        "bilibili",
        "BV1test",
        "视频发布现场",
    )
    assert _ocr_languages("auto", simplified_bilibili)[:2] == (
        "zh-Hans",
        "zh-Hant",
    )


def test_auto_ocr_language_reports_the_detected_chinese_script():
    traditional = (TranscriptSegment(0, 1, "這個國家發生變化"),)
    simplified = (TranscriptSegment(0, 1, "这个国家发生变化"),)

    assert _detected_ocr_language("auto", META, traditional) == "zh-Hant"
    neutral_meta = SourceMetadata(URL, "youtube", "video", "Test video")
    assert _detected_ocr_language("auto", neutral_meta, simplified) == "zh-Hans"
    assert _detected_ocr_language("es", neutral_meta, traditional) == "es"
