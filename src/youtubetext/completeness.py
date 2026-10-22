"""Conservative transcript coverage reporting and local audio activity checks.

Timeline coverage identifies intervals worth checking; it does not establish
whether anyone was speaking. FFmpeg can establish quiet audio, while active
audio may contain speech, music, or noise and requires further verification.
"""
from __future__ import annotations

import math
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .domain import TranscriptSegment
from .media import locate_ffmpeg


@dataclass(frozen=True, slots=True)
class CoverageGap:
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class CompletenessReport:
    duration_seconds: float
    covered_seconds: float
    gaps: tuple[CoverageGap, ...]

    @property
    def warning(self) -> str:
        if not self.gaps:
            return ""
        shown = self.gaps[:5]
        intervals = ", ".join(
            f"{_timestamp(gap.start_seconds)}–{_timestamp(gap.end_seconds)}"
            for gap in shown
        )
        remaining = len(self.gaps) - len(shown)
        if remaining:
            intervals += f", and {remaining} more"
        noun = "interval" if len(self.gaps) == 1 else "intervals"
        return (
            f"Transcript completeness needs review: {len(self.gaps)} long {noun} "
            f"without text ({intervals}). These gaps may contain silence, music, "
            "or missing speech."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "needs-review" if self.gaps else "unverified",
            "duration_seconds": self.duration_seconds,
            "covered_seconds": self.covered_seconds,
            "coverage_ratio": (
                self.covered_seconds / self.duration_seconds
                if self.duration_seconds > 0
                else None
            ),
            "long_gaps": [
                {
                    "start_seconds": gap.start_seconds,
                    "end_seconds": gap.end_seconds,
                }
                for gap in self.gaps
            ],
        }


def assess_completeness(
    segments: Sequence[TranscriptSegment],
    duration_seconds: float,
) -> CompletenessReport:
    """Measure interval unions and flag long gaps without judging their cause."""

    duration = float(duration_seconds or 0.0)
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    intervals: list[tuple[float, float]] = []
    for segment in segments:
        if not segment.text.strip():
            continue
        start = max(0.0, segment.start_seconds)
        end = segment.end_seconds
        if duration:
            start = min(start, duration)
            end = min(end, duration)
        if end > start:
            intervals.append((start, end))
    merged = _merge_intervals(intervals)
    covered = sum(end - start for start, end in merged)
    gaps: list[CoverageGap] = []
    if duration >= 90:
        threshold = max(60.0, min(300.0, duration * 0.1))
        cursor = 0.0
        for start, end in (*merged, (duration, duration)):
            if start - cursor >= threshold:
                gaps.append(CoverageGap(cursor, start))
            cursor = max(cursor, end)
    return CompletenessReport(duration, covered, tuple(gaps))


class AudioActivity(str, Enum):
    SILENT = "silent"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class FFmpegAudioActivity:
    """Check uncovered intervals without new models or retained audio artifacts.

    Only successful decoding through every requested interval can establish
    silence. Active audio is not evidence of speech. Errors remain UNKNOWN and
    never expose FFmpeg output, media paths, or source details to the caller.
    """

    def __init__(
        self,
        ffmpeg: Path | None = None,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._ffmpeg = Path(ffmpeg) if ffmpeg is not None else None
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        self._timeout_seconds = min(timeout, 120.0)

    def check(
        self,
        media_path: Path,
        gaps: Sequence[CoverageGap],
    ) -> AudioActivity:
        if not gaps or not all(_valid_gap(gap) for gap in gaps):
            return AudioActivity.UNKNOWN
        try:
            executable = self._ffmpeg or locate_ffmpeg()
            result = subprocess.run(
                [
                    str(executable),
                    "-nostdin",
                    "-hide_banner",
                    "-nostats",
                    "-xerror",
                    "-i",
                    str(Path(media_path).resolve()),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-af",
                    # Observe real decoded samples before detecting quiet audio.
                    # Timestamp jumps can inflate both progress and silence logs;
                    # padding or resetting PTS would hide that missing evidence.
                    "ashowinfo,"
                    "silencedetect=noise=-50dB:duration=0.5",
                    "-progress",
                    "pipe:1",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, UnicodeError):
            return AudioActivity.UNKNOWN
        if result.returncode != 0:
            return AudioActivity.UNKNOWN
        return _classify_audio(result.stdout or "", result.stderr or "", gaps)


_BOUNDARY_TOLERANCE = 0.25
_DECODE_TIME_TOLERANCE = 0.002
_SILENCE_EVENT = re.compile(r"\bsilence_(start|end):\s*([^\s|]+)")
_SILENCE_LOG = re.compile(r"^\[(?:Parsed_)?silencedetect(?:_\d+)? @ [^\]]+\]\s")
_FRAME_LOG = re.compile(r"^\[(?:Parsed_)?ashowinfo(?:_\d+)? @ [^\]]+\]\s")
_FRAME_FIELDS = re.compile(r"\b(pts_time|rate|nb_samples):([^\s]+)")


def _classify_audio(
    progress: str,
    diagnostics: str,
    gaps: Sequence[CoverageGap],
) -> AudioActivity:
    decoded_seconds = _decoded_duration(progress)
    if decoded_seconds is None or any(
        gap.end_seconds > decoded_seconds + _BOUNDARY_TOLERANCE for gap in gaps
    ):
        return AudioActivity.UNKNOWN
    decoded_frames = _decoded_frames(diagnostics, decoded_seconds)
    if decoded_frames is None or any(
        not _decoded_covers(gap, decoded_frames) for gap in gaps
    ):
        return AudioActivity.UNKNOWN
    quiet = _silence_intervals(diagnostics, decoded_seconds)
    if quiet is None:
        return AudioActivity.UNKNOWN
    for gap in gaps:
        if not _covers(gap, quiet):
            return AudioActivity.ACTIVE
    return AudioActivity.SILENT


def _decoded_frames(
    diagnostics: str,
    decoded_seconds: float,
) -> tuple[tuple[float, float], ...] | None:
    runs: list[tuple[float, float]] = []
    previous_start: float | None = None
    run_start: float | None = None
    expected_end = 0.0
    for line in diagnostics.splitlines():
        if not _FRAME_LOG.match(line):
            continue
        fields = dict(_FRAME_FIELDS.findall(line))
        try:
            start = float(fields["pts_time"])
            rate = int(fields["rate"])
            samples = int(fields["nb_samples"])
        except (KeyError, ValueError):
            return None
        if (
            not math.isfinite(start)
            or start < -_BOUNDARY_TOLERANCE
            or rate <= 0
            or samples <= 0
            or (previous_start is not None and start < previous_start)
        ):
            return None
        end = start + samples / rate
        if not math.isfinite(end) or end > decoded_seconds + _BOUNDARY_TOLERANCE:
            return None
        previous_start = start
        if end > 0:
            if run_start is None:
                run_start, expected_end = start, end
            elif abs(start - expected_end) <= _DECODE_TIME_TOLERANCE:
                # Compare against cumulative sample duration, rather than the
                # previous rounded timestamp. Millisecond container rounding
                # stays bounded; repeated small clock holes accumulate and
                # eventually break the run instead of being silently bridged.
                expected_end += samples / rate
            else:
                runs.append((max(0.0, run_start), expected_end))
                run_start, expected_end = start, end
    if run_start is not None:
        runs.append((max(0.0, run_start), expected_end))
    return tuple(runs) if runs else None


def _decoded_covers(
    gap: CoverageGap,
    runs: Sequence[tuple[float, float]],
) -> bool:
    # Outer boundaries may differ from metadata/cue rounding. Within a gap,
    # however, only one continuous sample run can establish decoded coverage;
    # applying that outer tolerance at every frame would hide missing packets.
    return any(
        start <= gap.start_seconds + _BOUNDARY_TOLERANCE
        and end >= gap.end_seconds - _BOUNDARY_TOLERANCE
        and max(start, gap.start_seconds) < min(end, gap.end_seconds)
        for start, end in runs
    )


def _covers(gap: CoverageGap, intervals: Sequence[tuple[float, float]]) -> bool:
    # Rounding tolerance applies at the requested outer boundaries only.
    # Joining separate silence intervals could otherwise erase short bursts
    # of speech/noise between them, repeatedly across the entire gap.
    return any(
        start <= gap.start_seconds + _BOUNDARY_TOLERANCE
        and end >= gap.end_seconds - _BOUNDARY_TOLERANCE
        and max(start, gap.start_seconds) < min(end, gap.end_seconds)
        for start, end in intervals
    )


def _decoded_duration(progress: str) -> float | None:
    complete = False
    values: list[float] = []
    for line in progress.splitlines():
        key, separator, value = line.partition("=")
        if separator and key == "progress":
            complete = value.strip() == "end"
        elif separator and key == "out_time_us":
            try:
                seconds = float(value) / 1_000_000
            except ValueError:
                return None
            if (
                not math.isfinite(seconds)
                or seconds < 0
                or (values and seconds < values[-1])
            ):
                return None
            values.append(seconds)
    if not complete or not values or max(values) <= 0:
        return None
    return max(values)


def _silence_intervals(
    diagnostics: str,
    decoded_seconds: float,
) -> tuple[tuple[float, float], ...] | None:
    quiet: list[tuple[float, float]] = []
    pending: float | None = None
    for line in diagnostics.splitlines():
        if not _SILENCE_LOG.match(line):
            continue
        matches = tuple(_SILENCE_EVENT.finditer(line))
        if "silence_" in line and not matches:
            return None
        for match in matches:
            try:
                timestamp = float(match.group(2))
            except ValueError:
                return None
            if (
                not math.isfinite(timestamp)
                or timestamp < -_BOUNDARY_TOLERANCE
                or timestamp > decoded_seconds + _BOUNDARY_TOLERANCE
            ):
                return None
            timestamp = min(decoded_seconds, max(0.0, timestamp))
            if match.group(1) == "start":
                if pending is not None or (quiet and timestamp < quiet[-1][1]):
                    return None
                pending = timestamp
            else:
                if pending is None or timestamp < pending:
                    return None
                quiet.append((pending, timestamp))
                pending = None
    if pending is not None:
        quiet.append((pending, decoded_seconds))
    return tuple(quiet)


def _valid_gap(gap: CoverageGap) -> bool:
    return (
        math.isfinite(gap.start_seconds)
        and math.isfinite(gap.end_seconds)
        and 0 <= gap.start_seconds < gap.end_seconds
    )


def _merge_intervals(
    intervals: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return tuple(result)


def _timestamp(seconds: float) -> str:
    whole_seconds, milliseconds = divmod(round(seconds * 1000), 1000)
    minutes, second = divmod(whole_seconds, 60)
    hour, minute = divmod(minutes, 60)
    base = f"{hour:02d}:{minute:02d}:{second:02d}"
    return f"{base}.{milliseconds:03d}" if milliseconds else base
