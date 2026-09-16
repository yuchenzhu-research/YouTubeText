from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from youtubetext.acquisition import TranscriptPipeline
from youtubetext.domain import ProcessingMode, SourceMetadata, TaskOptions
from youtubetext.media import MediaPurpose
from youtubetext.planning import (
    CacheAssumption,
    DownloadRequirement,
    PlanRoute,
    SubtitleValidation,
    build_processing_plan,
)
from youtubetext.runtime import CapacityPlan, HostProfile, ResourceGates
from youtubetext.sources import (
    SourceInspection,
    SubtitleAvailability,
    SubtitleKind,
)


URL = "https://www.youtube.com/watch?v=video"
METADATA = SourceMetadata(
    URL,
    "youtube",
    "video",
    "Planning test",
    author="Author",
    duration_seconds=120,
    webpage_url=URL,
)
SUBTITLE = SubtitleAvailability("zh-Hant", SubtitleKind.MANUAL)


@dataclass(frozen=True)
class ExpectedPlan:
    route: PlanRoute
    subtitle_download: DownloadRequirement
    media_download: DownloadRequirement
    media_purpose: MediaPurpose | None
    fallback_media_purpose: MediaPurpose | None = None
    processable: bool = True


@pytest.mark.parametrize(
    ("mode", "has_subtitle", "expected"),
    (
        (
            ProcessingMode.AUTO,
            True,
            ExpectedPlan(
                PlanRoute.CAPTIONS_THEN_OCR_THEN_WHISPER,
                DownloadRequirement.REQUIRED,
                DownloadRequirement.CONDITIONAL,
                MediaPurpose.ANALYSIS_VIDEO,
                MediaPurpose.AUDIO,
            ),
        ),
        (
            ProcessingMode.AUTO,
            False,
            ExpectedPlan(
                PlanRoute.OCR_THEN_WHISPER,
                DownloadRequirement.NONE,
                DownloadRequirement.REQUIRED,
                MediaPurpose.ANALYSIS_VIDEO,
                MediaPurpose.AUDIO,
            ),
        ),
        (
            ProcessingMode.CAPTIONS,
            True,
            ExpectedPlan(
                PlanRoute.PLATFORM_CAPTIONS,
                DownloadRequirement.REQUIRED,
                DownloadRequirement.NONE,
                None,
                None,
            ),
        ),
        (
            ProcessingMode.CAPTIONS,
            False,
            ExpectedPlan(
                PlanRoute.PLATFORM_CAPTIONS,
                DownloadRequirement.NONE,
                DownloadRequirement.NONE,
                None,
                None,
                processable=False,
            ),
        ),
        (
            ProcessingMode.OCR,
            True,
            ExpectedPlan(
                PlanRoute.OCR,
                DownloadRequirement.NONE,
                DownloadRequirement.REQUIRED,
                MediaPurpose.ANALYSIS_VIDEO,
                None,
            ),
        ),
        (
            ProcessingMode.OCR,
            False,
            ExpectedPlan(
                PlanRoute.OCR,
                DownloadRequirement.NONE,
                DownloadRequirement.REQUIRED,
                MediaPurpose.ANALYSIS_VIDEO,
                None,
            ),
        ),
        (
            ProcessingMode.WHISPER,
            True,
            ExpectedPlan(
                PlanRoute.WHISPER,
                DownloadRequirement.NONE,
                DownloadRequirement.REQUIRED,
                MediaPurpose.AUDIO,
                None,
            ),
        ),
        (
            ProcessingMode.WHISPER,
            False,
            ExpectedPlan(
                PlanRoute.WHISPER,
                DownloadRequirement.NONE,
                DownloadRequirement.REQUIRED,
                MediaPurpose.AUDIO,
                None,
            ),
        ),
        (
            ProcessingMode.HYBRID,
            True,
            ExpectedPlan(
                PlanRoute.OCR_AND_WHISPER,
                DownloadRequirement.NONE,
                DownloadRequirement.REQUIRED,
                MediaPurpose.ANALYSIS_VIDEO,
                MediaPurpose.AUDIO,
            ),
        ),
        (
            ProcessingMode.HYBRID,
            False,
            ExpectedPlan(
                PlanRoute.OCR_AND_WHISPER,
                DownloadRequirement.NONE,
                DownloadRequirement.REQUIRED,
                MediaPurpose.ANALYSIS_VIDEO,
                MediaPurpose.AUDIO,
            ),
        ),
    ),
)
def test_processing_plan_matches_pipeline_routes(
    mode: ProcessingMode,
    has_subtitle: bool,
    expected: ExpectedPlan,
) -> None:
    inspection = SourceInspection(METADATA, SUBTITLE if has_subtitle else None)

    plan = build_processing_plan(inspection, mode)

    assert plan.metadata is METADATA
    assert plan.mode is mode
    assert plan.route is expected.route
    assert plan.subtitle_download_requirement is expected.subtitle_download
    assert plan.media_download_requirement is expected.media_download
    assert plan.media_purpose is expected.media_purpose
    assert plan.fallback_media_purpose is expected.fallback_media_purpose
    assert plan.cache_assumption is CacheAssumption.NO_RESUME_REUSE
    assert plan.processable is expected.processable
    assert bool(plan.reason)
    assert (plan.advertised_subtitle is not None) is has_subtitle
    if plan.advertised_subtitle is not None:
        assert plan.advertised_subtitle.language == "zh-Hant"
        assert plan.advertised_subtitle.kind is SubtitleKind.MANUAL
        assert (
            plan.advertised_subtitle.validation
            is SubtitleValidation.ADVERTISED_UNVALIDATED
        )


def test_plan_serialization_labels_caption_as_advertised_and_unvalidated() -> None:
    plan = build_processing_plan(
        SourceInspection(METADATA, SUBTITLE),
        ProcessingMode.AUTO,
    )

    payload = plan.as_dict()

    assert payload["metadata"]["source_id"] == "video"
    assert payload["mode"] == "auto"
    assert payload["advertised_subtitle"] == {
        "language": "zh-Hant",
        "kind": "manual",
        "validation": "advertised-unvalidated",
    }
    assert payload["route"] == "platform-captions->ocr->whisper"
    assert payload["subtitle_download_requirement"] == "required"
    assert payload["media_download_requirement"] == "conditional"
    assert payload["media_purpose"] == "analysis-video"
    assert payload["fallback_media_purpose"] == "audio"
    assert payload["cache_assumption"] == "no-resume-reuse"


class InspectOnlySources:
    def __init__(self) -> None:
        self.inspect_calls = 0
        self.fetch_calls = 0
        self.preferred_languages: tuple[str, ...] = ()

    def inspect(self, url: str, *, preferred_languages=()) -> SourceInspection:
        self.inspect_calls += 1
        self.preferred_languages = tuple(preferred_languages)
        assert url == URL
        return SourceInspection(METADATA, SUBTITLE)

    def fetch(self, *_args, **_kwargs):
        self.fetch_calls += 1
        raise AssertionError("planning must not fetch a subtitle")


class ForbiddenMedia:
    async def download(self, *_args, **_kwargs):
        raise AssertionError("planning must not download media")


class ForbiddenFrames:
    async def sample(self, *_args, **_kwargs):
        raise AssertionError("planning must not sample frames")


class ForbiddenOCR:
    @property
    def checkpoint_revision(self) -> str:
        raise AssertionError("planning must not inspect the OCR backend")

    async def recognize_images_async(self, *_args, **_kwargs):
        raise AssertionError("planning must not run OCR")


class ForbiddenResumeStore:
    async def run(self, *_args, **_kwargs):
        raise AssertionError("planning must not read or write resume state")

    async def discard_incomplete(self, *_args, **_kwargs):
        raise AssertionError("planning must not read or write resume state")


def gates() -> ResourceGates:
    host = HostProfile("Darwin", "arm64", 16 * 1024**3, 8)
    return ResourceGates(CapacityPlan.for_host(host))


@pytest.mark.asyncio
async def test_pipeline_plan_only_inspects_metadata_once(tmp_path: Path) -> None:
    sources = InspectOnlySources()

    def forbidden_asr(_model: str):
        raise AssertionError("planning must not construct an ASR backend")

    pipeline = TranscriptPipeline(
        sources=sources,
        media=ForbiddenMedia(),
        frames=ForbiddenFrames(),
        ocr=ForbiddenOCR(),
        asr_factory=forbidden_asr,
        resume_store=ForbiddenResumeStore(),
        temp_root=tmp_path / "work",
    )

    plan = await pipeline.plan(
        URL,
        TaskOptions(
            mode=ProcessingMode.OCR,
            language="zh-Hant",
            output_dir=tmp_path / "output",
        ),
        gates(),
    )

    assert plan.route is PlanRoute.OCR
    assert sources.inspect_calls == 1
    assert sources.fetch_calls == 0
    assert sources.preferred_languages == ("zh-Hant",)
    assert not (tmp_path / "work").exists()
    assert not (tmp_path / "output").exists()
