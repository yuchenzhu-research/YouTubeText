from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from youtubetext.sources import (
    SourceClient,
    SourceFetchError,
    SubtitleKind,
    UnsupportedSourceError,
)


class FakeRunner:
    def __init__(
        self,
        info: Mapping[str, Any] | None = None,
        *,
        subtitle: str | None = None,
        suffix: str = "vtt",
        failure: Exception | None = None,
    ) -> None:
        self.info = dict(
            info
            or {
                "id": "abc123",
                "title": "A video",
                "uploader": "A creator",
                "duration": 42,
                "webpage_url": "https://www.youtube.com/watch?v=abc123",
            }
        )
        self.subtitle = subtitle
        self.suffix = suffix
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def run(
        self,
        url: str,
        options: Mapping[str, Any],
        *,
        download: bool,
    ) -> Mapping[str, Any]:
        copied = dict(options)
        self.calls.append((url, copied, download))
        if self.failure is not None:
            raise self.failure
        if download and self.subtitle is not None:
            destination = Path(str(options["outtmpl"]).replace("%(ext)s", self.suffix))
            destination.write_text(self.subtitle, encoding="utf-8")
        return self.info


def youtube_info(**updates: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": "abc123",
        "title": "A video",
        "uploader": "A creator",
        "duration": 42,
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "subtitles": {},
        "automatic_captions": {},
    }
    info.update(updates)
    return info


VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world
"""


@pytest.mark.parametrize(
    "url,platform",
    [
        ("https://youtube.com/watch?v=abc123", "youtube"),
        ("https://m.youtube.com/watch?v=abc123", "youtube"),
        ("https://youtu.be/abc123", "youtube"),
        ("https://www.bilibili.com/video/BV1abc", "bilibili"),
        ("https://space.bilibili.com/123/video/BV1abc", "bilibili"),
        ("https://b23.tv/xyz", "bilibili"),
    ],
)
def test_supported_urls_are_dispatched_to_real_adapters(url: str, platform: str):
    runner = FakeRunner()
    result = SourceClient(runner).fetch(url)

    assert result.metadata.platform == platform
    assert runner.calls[0][0] == url
    if platform == "youtube":
        assert "youtube" in runner.calls[0][1]["extractor_args"]
    else:
        assert runner.calls[0][1]["http_headers"]["Referer"].startswith(
            "https://www.bilibili.com"
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=abc123",
        "https://youtube.com.evil.test/watch?v=abc123",
        "https://bilibili.com.evil.test/video/BV1abc",
        "youtube.com/watch?v=abc123",
        "file:///tmp/youtube.com/video",
        "",
    ],
)
def test_unknown_or_non_http_sources_fail_before_runner(url: str):
    runner = FakeRunner()

    with pytest.raises(UnsupportedSourceError, match="unsupported source URL"):
        SourceClient(runner).fetch(url)

    assert runner.calls == []


def test_metadata_without_subtitles_is_a_successful_source_result():
    runner = FakeRunner(
        youtube_info(
            id="video-id",
            title="No captions",
            uploader="Channel",
            duration=90.5,
            subtitles={"live_chat": [{}]},
        )
    )

    result = SourceClient(runner).fetch("https://youtu.be/video-id")

    assert result.subtitle is None
    assert result.metadata.source_id == "video-id"
    assert result.metadata.title == "No captions"
    assert result.metadata.author == "Channel"
    assert result.metadata.duration_seconds == 90.5
    assert len(runner.calls) == 1
    assert runner.calls[0][2] is False


def test_metadata_only_mode_never_downloads_an_advertised_caption():
    runner = FakeRunner(
        youtube_info(subtitles={"en": [{"ext": "vtt"}]}),
        subtitle=VTT,
    )

    result = SourceClient(runner).fetch(
        "https://youtu.be/abc123",
        include_subtitles=False,
    )

    assert result.subtitle is None
    assert len(runner.calls) == 1
    assert runner.calls[0][2] is False


def test_manual_captions_are_selected_and_cleaned():
    rolling_vtt = """WEBVTT

00:00.000 --> 00:02.000 align:start position:0%
<c>Hello &amp;</c>

00:01.500 --> 00:04.000
<00:01.500>Hello &amp; welcome

00:05.000 --> 00:07.000
to YouTubeText
"""
    runner = FakeRunner(
        youtube_info(
            subtitles={"fr": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]},
            automatic_captions={"zh-Hant": [{"ext": "vtt"}]},
        ),
        subtitle=rolling_vtt,
    )

    result = SourceClient(runner).fetch("https://youtube.com/watch?v=abc123")

    assert result.subtitle is not None
    assert result.subtitle.kind is SubtitleKind.MANUAL
    assert result.subtitle.language == "en"
    assert result.subtitle.text == "Hello & welcome\nto YouTubeText"
    assert result.subtitle.segments[0].start_seconds == 0
    assert result.subtitle.segments[0].end_seconds == 4
    download_options = runner.calls[1][1]
    assert runner.calls[1][2] is True
    assert download_options["writesubtitles"] is True
    assert download_options["writeautomaticsub"] is False
    assert download_options["subtitleslangs"] == ["en"]
    assert download_options["skip_download"] is True


def test_explicit_language_can_choose_automatic_over_other_manual_language():
    runner = FakeRunner(
        youtube_info(
            subtitles={"en": [{"ext": "vtt"}]},
            automatic_captions={"es-ES": [{"ext": "vtt"}]},
        ),
        subtitle=VTT,
    )

    result = SourceClient(runner).fetch(
        "https://youtu.be/abc123", preferred_languages=("es", "en")
    )

    assert result.subtitle is not None
    assert result.subtitle.language == "es-ES"
    assert result.subtitle.kind is SubtitleKind.AUTOMATIC
    assert runner.calls[1][1]["writesubtitles"] is False
    assert runner.calls[1][1]["writeautomaticsub"] is True


def test_bilibili_default_language_priority_prefers_simplified_chinese():
    runner = FakeRunner(
        youtube_info(
            webpage_url="https://www.bilibili.com/video/BV1abc",
            subtitles={"en": [{"ext": "vtt"}], "zh-Hans": [{"ext": "vtt"}]},
        ),
        subtitle=VTT,
    )

    result = SourceClient(runner).fetch("https://www.bilibili.com/video/BV1abc")

    assert result.subtitle is not None
    assert result.subtitle.language == "zh-Hans"


def test_traditional_chinese_alias_matches_requested_language():
    runner = FakeRunner(
        youtube_info(automatic_captions={"zh-TW": [{"ext": "vtt"}]}),
        subtitle=VTT,
    )

    result = SourceClient(runner).fetch(
        "https://youtu.be/abc123", preferred_languages=("zh-Hant",)
    )

    assert result.subtitle is not None
    assert result.subtitle.language == "zh-TW"


def test_srt_is_parsed_to_shared_timestamped_segments():
    srt = """1
00:00:01,250 --> 00:00:03,500
First <b>line</b>

2
00:01:02,000 --> 00:01:04,000
Second line
"""
    runner = FakeRunner(
        youtube_info(subtitles={"en": [{"ext": "srt"}]}),
        subtitle=srt,
        suffix="srt",
    )

    result = SourceClient(runner).fetch("https://youtu.be/abc123")

    assert result.subtitle is not None
    assert [(segment.start_seconds, segment.end_seconds, segment.text) for segment in result.subtitle.segments] == [
        (1.25, 3.5, "First line"),
        (62.0, 64.0, "Second line"),
    ]


def test_metadata_failure_is_not_misreported_as_missing_captions():
    runner = FakeRunner(failure=RuntimeError("network unavailable"))

    with pytest.raises(SourceFetchError, match="metadata.*network unavailable") as error:
        SourceClient(runner).fetch("https://youtu.be/abc123")

    assert error.value.stage == "metadata"


def test_advertised_track_without_downloaded_file_is_an_error():
    runner = FakeRunner(youtube_info(subtitles={"en": [{"ext": "vtt"}]}))

    with pytest.raises(SourceFetchError, match="no VTT/SRT file") as error:
        SourceClient(runner).fetch("https://youtu.be/abc123")

    assert error.value.stage == "subtitle download"


def test_empty_downloaded_subtitle_is_a_parse_error():
    runner = FakeRunner(
        youtube_info(subtitles={"en": [{"ext": "vtt"}]}),
        subtitle="WEBVTT\n\nNOTE no cues here\n",
    )

    with pytest.raises(SourceFetchError, match="no usable cues") as error:
        SourceClient(runner).fetch("https://youtu.be/abc123")

    assert error.value.stage == "subtitle parse"


def test_malformed_required_metadata_is_reported_at_the_module_interface():
    runner = FakeRunner(youtube_info(id="", duration=-1))

    with pytest.raises(SourceFetchError, match="metadata") as error:
        SourceClient(runner).fetch("https://youtu.be/abc123")

    assert error.value.stage == "metadata"
