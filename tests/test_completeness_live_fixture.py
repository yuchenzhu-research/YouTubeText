"""Replay real platform timing, with deliberate missing intervals, offline.

The fixture keeps only timing and source provenance, not copyrighted caption
text. It checks omission detection rather than word or character accuracy.
"""
import json
from pathlib import Path

import pytest

from youtubetext.completeness import assess_completeness
from youtubetext.domain import TranscriptSegment


@pytest.mark.parametrize("missing", [None, (0, 250), (400, 650), (600, 1203)])
def test_live_ted_caption_timeline_detects_injected_missing_intervals(missing):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "ted_caption_timing.json").read_text()
    )
    segments = tuple(
        TranscriptSegment(start, end, f"Synthetic placeholder {index}")
        for index, (start, end) in enumerate(fixture["intervals"])
        if missing is None or end <= missing[0] or start >= missing[1]
    )

    report = assess_completeness(segments, fixture["duration_seconds"])

    if missing is None:
        assert report.gaps == ()
        assert report.as_dict()["status"] == "unverified"
    else:
        assert any(
            gap.start_seconds <= missing[0] and gap.end_seconds >= missing[1]
            for gap in report.gaps
        )
        assert report.as_dict()["status"] == "needs-review"
