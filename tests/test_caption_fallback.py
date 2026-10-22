"""Advertised caption alternatives should precede expensive local inference."""
from __future__ import annotations

from pathlib import Path

import pytest

from youtubetext.sources import SourceClient, SourceFetchError, SubtitleKind, YtDlpAuth

URL = "https://youtu.be/fallback"
VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nRecovered text 中文\n"
FORMATS = [{"ext": "vtt"}]


class TrackRunner:
    def __init__(self, *, manual=(), automatic=(), language="", outcomes=None):
        self.info = {
            "id": "fallback",
            "title": "Caption fallback",
            "duration": 90,
            "language": language,
            "subtitles": dict.fromkeys(manual, FORMATS),
            "automatic_captions": dict.fromkeys(automatic, FORMATS),
        }
        self.outcomes = outcomes or {}
        self.calls = []
        self.attempts = []

    def run(self, _url, options, *, download):
        self.calls.append((dict(options), download))
        if not download:
            return self.info
        assert options["skip_download"] is True
        assert options["writesubtitles"] != options["writeautomaticsub"]
        kind = "manual" if options["writesubtitles"] else "automatic"
        language, = options["subtitleslangs"]
        self.attempts.append((kind, language))
        outcome = self.outcomes.get((kind, language))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            path = Path(str(options["outtmpl"]).replace("%(ext)s", "vtt"))
            path.write_text(outcome, encoding="utf-8")
        return self.info


@pytest.mark.parametrize("failed_track", [
    None,
    "WEBVTT\n\nNOTE no usable cues\n",
    "WEBVTT\n\n00:00:05.000 --> 00:00:01.000\nBackwards cue\n",
    RuntimeError("caption endpoint failed"),
])
def test_failed_manual_track_tries_same_language_automatic_before_unrelated_manual(failed_track):
    runner = TrackRunner(
        manual=("en", "fr"),
        automatic=("en",),
        outcomes={
            ("manual", "en"): failed_track,
            ("manual", "fr"): VTT,
            ("automatic", "en"): VTT,
        },
    )

    result = SourceClient(runner).fetch(URL)

    assert result.subtitle is not None
    assert result.subtitle.kind is SubtitleKind.AUTOMATIC
    assert result.subtitle.language == "en"
    assert result.subtitle.text == "Recovered text 中文"
    assert runner.attempts == [("manual", "en"), ("automatic", "en")]
    assert len(result.warnings) == 1
    assert "fallback" in result.warnings[0].lower()
    assert "automatic 'en'" in result.warnings[0]
    assert "manual 'en'" in result.warnings[0]


def test_existing_explicit_first_choice_and_requested_language_order_are_preserved():
    runner = TrackRunner(
        manual=("en", "es-ES", "fr"),
        automatic=("es", "en"),
        outcomes={("manual", "en"): VTT},
    )

    result = SourceClient(runner).fetch(URL, preferred_languages=("es", "en"))

    assert runner.attempts == [
        ("automatic", "es"),
        ("manual", "es-ES"),
        ("manual", "en"),
    ]
    assert result.subtitle.language == "en"
    assert "manual 'en'" in result.warnings[0]
    assert "automatic 'es'" in result.warnings[0]


def test_source_language_is_tried_before_unrelated_advertised_languages():
    runner = TrackRunner(
        manual=("en", "fr"), automatic=("ja",), language="ja",
        outcomes={("automatic", "ja"): VTT, ("manual", "fr"): VTT},
    )

    result = SourceClient(runner).fetch(URL)

    assert runner.attempts == [("manual", "en"), ("automatic", "ja")]
    assert result.subtitle.language == "ja"
    assert "automatic 'ja'" in result.warnings[0]
    assert "manual 'en'" in result.warnings[0]


def test_unrelated_advertised_language_fallback_is_explicit_in_warning():
    runner = TrackRunner(
        manual=("en", "fr"), outcomes={("manual", "fr"): VTT},
    )

    result = SourceClient(runner).fetch(URL, preferred_languages=("en",))

    assert runner.attempts == [("manual", "en"), ("manual", "fr")]
    assert result.subtitle.language == "fr"
    assert "manual 'fr'" in result.warnings[0]
    assert "manual 'en'" in result.warnings[0]
    assert all("translate" not in option for options, _download in runner.calls for option in options)


@pytest.mark.parametrize("strict", [False, True])
def test_all_failed_tracks_report_bounded_aggregated_reasons(strict):
    runner = TrackRunner(
        manual=("en",), automatic=("en",),
        outcomes={
            ("manual", "en"): RuntimeError("download unavailable " + "x" * 3000),
            ("automatic", "en"): "WEBVTT\n\nNOTE empty\n",
        },
    )
    if strict:
        with pytest.raises(SourceFetchError) as error:
            SourceClient(runner).fetch(URL)
        message = str(error.value)
        assert error.value.stage == "subtitle candidates"
    else:
        result = SourceClient(runner).fetch(URL, strict_subtitles=False)
        assert result.subtitle is None
        assert result.metadata.source_id == "fallback"
        message, = result.warnings

    assert runner.attempts == [("manual", "en"), ("automatic", "en")]
    assert "all 2 attempted" in message
    assert "manual 'en'" in message
    assert "automatic 'en'" in message
    assert "download unavailable" in message
    assert "no usable cues" in message
    assert len(message) < 800


def test_candidate_limit_deduplicates_languages_and_excludes_non_caption_tracks():
    runner = TrackRunner(
        manual=("en", "EN", "en-US", "en_US", "fr", "es", "de", "ko", "ja", "live_chat"),
        automatic=("en", "EN", "danmaku", "comments"),
    )

    result = SourceClient(runner).fetch(URL, strict_subtitles=False)

    assert len(runner.attempts) == 5
    assert runner.attempts[:3] == [
        ("manual", "en"), ("automatic", "EN"), ("manual", "en-US"),
    ]
    normalized = [(kind, language.lower().replace("_", "-")) for kind, language in runner.attempts]
    assert len(set(normalized)) == len(normalized)
    assert all(language not in {"live_chat", "danmaku", "comments"} for _kind, language in runner.attempts)
    assert "Attempt limit 5 reached" in result.warnings[0]
    assert "other advertised track(s) were not tried" in result.warnings[0]


def test_inspect_reports_same_initial_candidate_without_downloading_any_alternatives():
    runner = TrackRunner(manual=("en", "fr"), automatic=("es", "en"))

    result = SourceClient(runner).inspect(URL, preferred_languages=("es", "en"))

    assert result.subtitle.language == "es"
    assert result.subtitle.kind is SubtitleKind.AUTOMATIC
    assert runner.attempts == []
    assert len(runner.calls) == 1
    assert runner.calls[0][1] is False


def test_successful_first_candidate_has_no_fallback_warning_or_extra_download():
    runner = TrackRunner(
        manual=("en", "fr"), automatic=("en",),
        outcomes={("manual", "en"): VTT},
    )

    result = SourceClient(runner).fetch(URL)

    assert runner.attempts == [("manual", "en")]
    assert result.warnings == ()


def test_each_fallback_download_gets_an_isolated_auth_copy(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("private original cookie material", encoding="utf-8")
    runner = TrackRunner(
        manual=("en",), automatic=("en",),
        outcomes={("automatic", "en"): VTT},
    )

    SourceClient(runner, auth=YtDlpAuth(cookie_file=cookie_file)).fetch(URL)

    copies = [options["cookiefile"] for options, _download in runner.calls]
    assert len(copies) == 3
    assert len({id(copy) for copy in copies}) == 3
    assert all(copy.getvalue() == "private original cookie material" for copy in copies)
    copies[1].seek(0)
    copies[1].write("changed only in memory")
    assert copies[2].getvalue() == "private original cookie material"
    assert cookie_file.read_text(encoding="utf-8") == "private original cookie material"
