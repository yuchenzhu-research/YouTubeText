"""Long transcript gaps must be checked without discarding recovered text."""

from pathlib import Path

import pytest

from youtubetext.acquisition import TranscriptPipeline
from youtubetext.asr import ASRResult
from youtubetext.domain import (
    ProcessingMode, SourceMetadata, TaskOptions, TranscriptMethod, TranscriptSegment,
)
from youtubetext.media import MediaPurpose
from youtubetext.runtime import CapacityPlan, HostProfile, ResourceGates
from youtubetext.resume import LocalResumeStore
from youtubetext.sources import SourceResult, SubtitleKind, SubtitleTrack

URL = "https://www.youtube.com/watch?v=coverage-regression"
META = SourceMetadata(URL, "youtube", "coverage-regression", "Coverage regression", duration_seconds=3600)
PREFIX = tuple(
    TranscriptSegment(i * 4, (i + 1) * 4, f"{i:04d} 这一段是不同的连续讲话内容，用于检查完整性规则。")
    for i in range(360)
)
LATE_SPEECH = TranscriptSegment(2400, 2404, "Previously missing later speech")


class Sources:
    def __init__(self, caption):
        self.caption = caption

    def fetch(self, _url, **_kwargs):
        track = SubtitleTrack("zh", SubtitleKind.MANUAL, PREFIX) if self.caption else None
        return SourceResult(META, track)


class Media:
    def __init__(self):
        self.purposes = []
        self.paths = []
        self.fail = False

    async def download(self, _url, directory, purpose):
        self.purposes.append(purpose)
        if self.fail:
            raise RuntimeError("media unavailable")
        path = directory / "media.mp4"
        path.write_bytes(b"injected media; activity detection is stubbed")
        self.paths.append(path)
        return path


class Speech:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.segments = (LATE_SPEECH,)

    def transcribe(self, _path, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no recognizable speech")
        return ASRResult("zh", "base", self.segments)


class Activity:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def check(self, path, gaps):
        from youtubetext.completeness import AudioActivity

        self.calls.append((path, gaps))
        if self.verdict == "raise":
            raise OSError("probe unavailable")
        return AudioActivity(self.verdict)


async def no_progress(_event):
    pass


def setup_pipeline(tmp_path, *, caption=False, verdict="active"):
    media, speech, activity = Media(), Speech(), Activity(verdict)
    instance = TranscriptPipeline(
        sources=Sources(caption), media=media, frames=object(), ocr=object(),
        asr_factory=lambda _model: speech, temp_root=tmp_path,
    )
    instance._audio_activity = activity

    async def read_ocr(*_args):
        return PREFIX, ()

    instance._read_burned_captions = read_ocr
    gates = ResourceGates(CapacityPlan.for_host(HostProfile("Darwin", "arm64", 8 * 1024**3, 8)))
    return instance, media, speech, activity, gates


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", (False, True))
async def test_auto_recovers_late_speech_when_only_first_24_minutes_have_text(tmp_path, caption):
    instance, media, speech, activity, gates = setup_pipeline(tmp_path, caption=caption)
    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert LATE_SPEECH in result.segments
    assert all(segment in result.segments for segment in PREFIX)
    assert speech.calls == 1
    assert len(activity.calls) == 1
    assert media.purposes == [MediaPurpose.AUDIO if caption else MediaPurpose.ANALYSIS_VIDEO]
    assert not any(path.exists() for path in media.paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", (False, True))
async def test_verified_quiet_gap_does_not_trigger_speech_inference(tmp_path, caption):
    instance, _media, speech, activity, gates = setup_pipeline(tmp_path, caption=caption, verdict="silent")
    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert result.segments == PREFIX
    assert speech.calls == 0
    assert len(activity.calls) == 1
    assert any("quiet" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ("unknown", "raise"))
async def test_unavailable_activity_check_still_attempts_speech_verification(tmp_path, verdict):
    instance, _media, speech, _activity, gates = setup_pipeline(tmp_path, verdict=verdict)
    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert LATE_SPEECH in result.segments
    assert speech.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", (False, True))
async def test_speech_verification_failure_retains_text_and_exposes_uncertainty(tmp_path, caption):
    instance, _media, speech, _activity, gates = setup_pipeline(tmp_path, caption=caption)
    speech.fail = True
    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert result.segments == PREFIX
    assert any("could not verify" in warning.lower() for warning in result.warnings)
    assert any("long" in warning.lower() and "gap" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_caption_verification_download_failure_does_not_lose_caption(tmp_path):
    instance, media, speech, _activity, gates = setup_pipeline(tmp_path, caption=True)
    media.fail = True
    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert result.segments == PREFIX
    assert speech.calls == 0
    assert any("could not verify" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_caption_verification_storage_failure_does_not_lose_caption(tmp_path):
    instance, media, speech, _activity, gates = setup_pipeline(tmp_path, caption=True)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a temporary directory", encoding="utf-8")
    instance._temp_root = blocked

    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert result.segments == PREFIX
    assert not media.purposes
    assert speech.calls == 0
    assert any("could not verify" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", (ProcessingMode.CAPTIONS, ProcessingMode.OCR))
async def test_explicit_modes_warn_without_adding_speech_recognition(tmp_path, mode):
    instance, media, speech, activity, gates = setup_pipeline(tmp_path, caption=mode is ProcessingMode.CAPTIONS)
    result = await instance.acquire(URL, TaskOptions(mode=mode), gates, no_progress)

    assert result.segments == PREFIX
    assert speech.calls == 0
    assert not activity.calls
    assert media.purposes == ([] if mode is ProcessingMode.CAPTIONS else [MediaPurpose.ANALYSIS_VIDEO])
    assert any("long" in warning.lower() and "gap" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_verified_platform_caption_merge_reports_its_actual_method(tmp_path):
    instance, _media, _speech, _activity, gates = setup_pipeline(tmp_path, caption=True)
    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert result.method.value == "platform-captions-whisper"


@pytest.mark.asyncio
async def test_gap_verification_does_not_duplicate_already_captioned_foreign_language_text(tmp_path):
    instance, _media, speech, _activity, gates = setup_pipeline(tmp_path, caption=True)
    translation = TranscriptSegment(10, 14, "ASR wording in another language")
    speech.segments = (translation, LATE_SPEECH)

    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert translation not in result.segments
    assert LATE_SPEECH in result.segments


@pytest.mark.asyncio
async def test_verification_without_supplements_preserves_actual_extraction_method(tmp_path):
    instance, _media, speech, _activity, gates = setup_pipeline(tmp_path, caption=True)
    speech.segments = (TranscriptSegment(10, 14, "Already captioned speech"),)

    result = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert result.method is TranscriptMethod.PLATFORM_CAPTIONS
    assert result.segments == PREFIX
    assert any("no additional text" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_resume_retries_failed_verification_instead_of_caching_incomplete_result(tmp_path):
    instance, media, speech, _activity, gates = setup_pipeline(tmp_path)
    instance._resume = LocalResumeStore(tmp_path / "resume")
    speech.fail = True
    first = await instance.acquire(URL, TaskOptions(), gates, no_progress)
    assert first.segments == PREFIX
    assert speech.calls == 1

    speech.fail = False
    recovered = await instance.acquire(URL, TaskOptions(), gates, no_progress)

    assert LATE_SPEECH in recovered.segments
    assert speech.calls == 2
    assert len(media.purposes) == 1  # The downloaded media is still reusable.
