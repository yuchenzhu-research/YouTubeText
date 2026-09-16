"""Read-only processing plans derived from source metadata.

A plan describes what the current acquisition pipeline would attempt.  Caption
availability is deliberately represented as an *advertisement*, not as proof
that the selected track can be downloaded or parsed successfully.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .domain import ProcessingMode, SourceMetadata
from .media import MediaPurpose
from .sources import SourceInspection, SubtitleAvailability, SubtitleKind


class PlanRoute(str, Enum):
    """The ordered route the acquisition pipeline will follow."""

    CAPTIONS_THEN_OCR_THEN_WHISPER = "platform-captions->ocr->whisper"
    OCR_THEN_WHISPER = "ocr->whisper"
    PLATFORM_CAPTIONS = "platform-captions"
    OCR = "ocr"
    WHISPER = "whisper"
    OCR_AND_WHISPER = "ocr+whisper"


class DownloadRequirement(str, Enum):
    """Whether a fresh, non-resumed run will download an artifact."""

    NONE = "none"
    CONDITIONAL = "conditional"
    REQUIRED = "required"


class SubtitleValidation(str, Enum):
    """How much is known about a subtitle candidate during planning."""

    ADVERTISED_UNVALIDATED = "advertised-unvalidated"


class CacheAssumption(str, Enum):
    """What planning assumes about completed local resume state."""

    NO_RESUME_REUSE = "no-resume-reuse"


@dataclass(frozen=True, slots=True)
class AdvertisedSubtitle:
    """A caption candidate reported by the platform but not fetched or parsed."""

    language: str
    kind: SubtitleKind
    validation: SubtitleValidation = SubtitleValidation.ADVERTISED_UNVALIDATED

    @classmethod
    def from_availability(cls, value: SubtitleAvailability) -> "AdvertisedSubtitle":
        return cls(language=value.language, kind=value.kind)

    def as_dict(self) -> dict[str, str]:
        return {
            "language": self.language,
            "kind": self.kind.value,
            "validation": self.validation.value,
        }


@dataclass(frozen=True, slots=True)
class ProcessingPlan:
    """A side-effect-free forecast of fresh acquisition work.

    Planning deliberately does not read resume state, so a completed local
    transcript can still let execution skip work marked here as required.  The
    media purpose is the primary artifact: AUTO and HYBRID normally reuse an
    analysis video's audio for Whisper, but execution can retry with a separate
    audio download if that video cannot be obtained.
    """

    metadata: SourceMetadata
    mode: ProcessingMode
    advertised_subtitle: AdvertisedSubtitle | None
    route: PlanRoute
    subtitle_download_requirement: DownloadRequirement
    media_download_requirement: DownloadRequirement
    media_purpose: MediaPurpose | None
    fallback_media_purpose: MediaPurpose | None
    processable: bool
    reason: str
    cache_assumption: CacheAssumption = CacheAssumption.NO_RESUME_REUSE

    def __post_init__(self) -> None:
        if (
            self.subtitle_download_requirement is not DownloadRequirement.NONE
            and self.advertised_subtitle is None
        ):
            raise ValueError("a subtitle download requires an advertised subtitle")
        if self.media_download_requirement is DownloadRequirement.NONE:
            if self.media_purpose is not None:
                raise ValueError("media purpose must be empty when no media is downloaded")
        elif self.media_purpose is None:
            raise ValueError("a media download requires a media purpose")
        if self.fallback_media_purpose is not None:
            if self.media_purpose is None:
                raise ValueError("fallback media requires a primary media purpose")
            if self.fallback_media_purpose is self.media_purpose:
                raise ValueError("fallback media purpose must differ from the primary")
        if not self.processable and not self.reason.strip():
            raise ValueError("an unprocessable plan requires a reason")

    def as_dict(self) -> dict[str, Any]:
        metadata = self.metadata
        return {
            "metadata": {
                "url": metadata.url,
                "platform": metadata.platform,
                "source_id": metadata.source_id,
                "title": metadata.title,
                "author": metadata.author,
                "duration_seconds": metadata.duration_seconds,
                "webpage_url": metadata.webpage_url,
            },
            "mode": self.mode.value,
            "advertised_subtitle": (
                self.advertised_subtitle.as_dict()
                if self.advertised_subtitle is not None
                else None
            ),
            "route": self.route.value,
            "subtitle_download_requirement": self.subtitle_download_requirement.value,
            "media_download_requirement": self.media_download_requirement.value,
            "media_purpose": self.media_purpose.value if self.media_purpose else None,
            "fallback_media_purpose": (
                self.fallback_media_purpose.value
                if self.fallback_media_purpose is not None
                else None
            ),
            "cache_assumption": self.cache_assumption.value,
            "processable": self.processable,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PlanResult:
    """The isolated result of planning one URL."""

    url: str
    plan: ProcessingPlan | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if self.plan is not None and self.error:
            raise ValueError("a plan result cannot contain both a plan and an error")

    @property
    def succeeded(self) -> bool:
        return self.plan is not None and not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "succeeded": self.succeeded,
            "plan": self.plan.as_dict() if self.plan is not None else None,
            "error": self.error,
        }


def build_processing_plan(
    inspection: SourceInspection,
    mode: ProcessingMode,
) -> ProcessingPlan:
    """Map a metadata inspection to the route used by ``TranscriptPipeline``."""

    subtitle = (
        AdvertisedSubtitle.from_availability(inspection.subtitle)
        if inspection.subtitle is not None
        else None
    )

    if mode is ProcessingMode.AUTO:
        if subtitle is not None:
            return ProcessingPlan(
                metadata=inspection.metadata,
                mode=mode,
                advertised_subtitle=subtitle,
                route=PlanRoute.CAPTIONS_THEN_OCR_THEN_WHISPER,
                subtitle_download_requirement=DownloadRequirement.REQUIRED,
                media_download_requirement=DownloadRequirement.CONDITIONAL,
                media_purpose=MediaPurpose.ANALYSIS_VIDEO,
                fallback_media_purpose=MediaPurpose.AUDIO,
                processable=True,
                reason=(
                    "The advertised caption will be tried first; OCR and Whisper "
                    "remain fallbacks until that track is downloaded and validated. "
                    "If analysis video is unavailable, Whisper can retry with audio."
                ),
            )
        return ProcessingPlan(
            metadata=inspection.metadata,
            mode=mode,
            advertised_subtitle=None,
            route=PlanRoute.OCR_THEN_WHISPER,
            subtitle_download_requirement=DownloadRequirement.NONE,
            media_download_requirement=DownloadRequirement.REQUIRED,
            media_purpose=MediaPurpose.ANALYSIS_VIDEO,
            fallback_media_purpose=MediaPurpose.AUDIO,
            processable=True,
            reason=(
                "No platform caption is advertised; OCR will run first and Whisper "
                "will be used if OCR is not usable. If analysis video is unavailable, "
                "Whisper can retry with audio."
            ),
        )

    if mode is ProcessingMode.CAPTIONS:
        return ProcessingPlan(
            metadata=inspection.metadata,
            mode=mode,
            advertised_subtitle=subtitle,
            route=PlanRoute.PLATFORM_CAPTIONS,
            subtitle_download_requirement=(
                DownloadRequirement.REQUIRED
                if subtitle is not None
                else DownloadRequirement.NONE
            ),
            media_download_requirement=DownloadRequirement.NONE,
            media_purpose=None,
            fallback_media_purpose=None,
            processable=subtitle is not None,
            reason=(
                "The advertised caption must still be downloaded and validated."
                if subtitle is not None
                else "No platform caption track is advertised for captions-only mode."
            ),
        )

    if mode is ProcessingMode.OCR:
        route = PlanRoute.OCR
        media_purpose = MediaPurpose.ANALYSIS_VIDEO
        fallback_media_purpose = None
        reason = "OCR mode ignores platform captions and analyzes video frames."
    elif mode is ProcessingMode.WHISPER:
        route = PlanRoute.WHISPER
        media_purpose = MediaPurpose.AUDIO
        fallback_media_purpose = None
        reason = "Whisper mode ignores platform captions and transcribes downloaded audio."
    elif mode is ProcessingMode.HYBRID:
        route = PlanRoute.OCR_AND_WHISPER
        media_purpose = MediaPurpose.ANALYSIS_VIDEO
        fallback_media_purpose = MediaPurpose.AUDIO
        reason = (
            "Hybrid mode ignores platform captions and runs OCR plus Whisper; "
            "if analysis video is unavailable, Whisper can retry with audio."
        )
    else:  # defensive if a future enum member reaches an older planner
        raise ValueError(f"unsupported processing mode: {mode}")

    return ProcessingPlan(
        metadata=inspection.metadata,
        mode=mode,
        advertised_subtitle=subtitle,
        route=route,
        subtitle_download_requirement=DownloadRequirement.NONE,
        media_download_requirement=DownloadRequirement.REQUIRED,
        media_purpose=media_purpose,
        fallback_media_purpose=fallback_media_purpose,
        processable=True,
        reason=reason,
    )
