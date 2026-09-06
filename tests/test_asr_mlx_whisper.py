from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from youtubetext.asr import MLXWhisperASR
from youtubetext.asr.mlx_whisper import _ffmpeg_trailing_silence
from youtubetext.asr.models import GIB


def test_injected_transcriber_is_offline_and_normalizes_segments(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"not decoded by the injected engine")
    call: dict[str, Any] = {}

    def fake_transcribe(path: str, **kwargs: Any) -> dict[str, Any]:
        call.update(path=path, **kwargs)
        return {
            "language": "zh",
            "text": "ignored because timestamped segments exist",
            "segments": [
                {"start": 4.0, "end": 5.25, "text": "  第二句  "},
                {"start": 1.125, "end": 2.5, "text": "第一   句"},
                {"start": 3.0, "end": 3.2, "text": "   "},
            ],
        }

    backend = MLXWhisperASR(
        "auto",
        memory_bytes=16 * GIB,
        transcribe_callable=fake_transcribe,
        duration_probe=lambda _path: None,
    )
    assert not backend.model_cached

    result = backend.transcribe(audio)

    assert result.model == "large-v3-turbo"
    assert result.language == "zh"
    assert result.text == "第一 句\n第二句"
    assert result.timestamped_text == (
        "[00:00:01.125 --> 00:00:02.500] 第一 句\n"
        "[00:00:04.000 --> 00:00:05.250] 第二句"
    )
    assert call["path"] == str(audio)
    assert call["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    assert call["language"] is None
    assert call["word_timestamps"] is True
    assert call["hallucination_silence_threshold"] == 2.0
    assert "clip_timestamps" not in call
    assert not backend.model_cached


def test_explicit_language_is_passed_to_engine(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    seen: dict[str, Any] = {}

    def fake_transcribe(_path: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"text": "hola", "segments": []}

    result = MLXWhisperASR(
        "small",
        memory_bytes=48 * GIB,
        transcribe_callable=fake_transcribe,
        duration_probe=lambda _path: None,
    ).transcribe(audio, language="es")

    assert seen["language"] == "es"
    assert result.language == "es"
    assert result.text == "hola"
    assert result.segments[0].start_seconds == 0


def test_missing_audio_fails_before_engine_call(tmp_path: Path) -> None:
    called = False

    def fake_transcribe(_path: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"text": "unexpected"}

    backend = MLXWhisperASR(
        transcribe_callable=fake_transcribe,
        duration_probe=lambda _path: None,
    )
    with pytest.raises(FileNotFoundError):
        backend.transcribe(tmp_path / "missing.wav")
    assert not called


def test_empty_engine_result_is_an_error(tmp_path: Path) -> None:
    audio = tmp_path / "silence.wav"
    audio.touch()
    backend = MLXWhisperASR(
        transcribe_callable=lambda *_args, **_kwargs: {"text": "", "segments": []},
        duration_probe=lambda _path: None,
    )
    with pytest.raises(RuntimeError, match="no recognizable speech"):
        backend.transcribe(audio)


def test_asr_result_has_no_summary_responsibility(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    result = MLXWhisperASR(
        transcribe_callable=lambda *_args, **_kwargs: {"text": "transcript"},
        duration_probe=lambda _path: None,
    ).transcribe(audio)
    assert result.text == "transcript"
    assert not hasattr(result, "summary")


def test_known_duration_bounds_decode_and_normalized_segments(tmp_path: Path) -> None:
    audio = tmp_path / "speech.m4a"
    audio.touch()
    call: dict[str, Any] = {}

    def fake_transcribe(_path: str, **kwargs: Any) -> dict[str, Any]:
        call.update(kwargs)
        return {
            "language": "zh",
            "text": "valid clipped hallucination",
            "segments": [
                {"start": 1.0, "end": 2.0, "text": "valid"},
                {"start": 9.0, "end": 12.0, "text": "clipped"},
                {"start": 10.5, "end": 14.0, "text": "hallucination"},
            ],
        }

    result = MLXWhisperASR(
        transcribe_callable=fake_transcribe,
        duration_probe=lambda _path: 10.0,
    ).transcribe(audio, language="zh-Hant")

    assert call["clip_timestamps"] == [0.0, 10.0]
    assert call["word_timestamps"] is True
    assert call["hallucination_silence_threshold"] == 2.0
    assert [segment.text for segment in result.segments] == ["valid", "clipped"]
    assert result.segments[-1].end_seconds == 10.0


def test_duration_probe_failure_is_safe_and_does_not_limit_decode(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    call: dict[str, Any] = {}

    def broken_probe(_path: Path) -> float:
        raise RuntimeError("ffprobe unavailable")

    def fake_transcribe(_path: str, **kwargs: Any) -> dict[str, Any]:
        call.update(kwargs)
        return {"text": "still transcribed"}

    result = MLXWhisperASR(
        transcribe_callable=fake_transcribe,
        duration_probe=broken_probe,
    ).transcribe(audio)

    assert result.text == "still transcribed"
    assert "clip_timestamps" not in call


def test_trailing_silence_limits_decode_and_discards_tail_segments(tmp_path: Path) -> None:
    audio = tmp_path / "speech.m4a"
    audio.touch()
    call: dict[str, Any] = {}

    def fake_transcribe(_path: str, **kwargs: Any) -> dict[str, Any]:
        call.update(kwargs)
        return {
            "language": "zh",
            "segments": [
                {"start": 7.0, "end": 8.1, "text": "valid ending"},
                {"start": 9.0, "end": 10.0, "text": "silent hallucination"},
            ],
        }

    result = MLXWhisperASR(
        transcribe_callable=fake_transcribe,
        duration_probe=lambda _path: 10.0,
        trailing_silence_probe=lambda _path, _duration: 8.0,
    ).transcribe(audio)

    assert call["clip_timestamps"] == [0.0, 8.25]
    assert [segment.text for segment in result.segments] == ["valid ending"]


def test_short_or_invalid_trailing_silence_does_not_shorten_media(tmp_path: Path) -> None:
    audio = tmp_path / "speech.m4a"
    audio.touch()

    for reported_start in (9.0, -1.0, float("nan")):
        call: dict[str, Any] = {}

        def fake_transcribe(_path: str, **kwargs: Any) -> dict[str, Any]:
            call.update(kwargs)
            return {"text": "kept"}

        MLXWhisperASR(
            transcribe_callable=fake_transcribe,
            duration_probe=lambda _path: 10.0,
            trailing_silence_probe=lambda _path, _duration: reported_start,
        ).transcribe(audio)

        assert call["clip_timestamps"] == [0.0, 10.0]


def test_trailing_silence_probe_failure_keeps_full_duration(tmp_path: Path) -> None:
    audio = tmp_path / "speech.m4a"
    audio.touch()
    call: dict[str, Any] = {}

    def broken_probe(_path: Path, _duration: float) -> float:
        raise RuntimeError("ffmpeg unavailable")

    def fake_transcribe(_path: str, **kwargs: Any) -> dict[str, Any]:
        call.update(kwargs)
        return {"text": "kept"}

    MLXWhisperASR(
        transcribe_callable=fake_transcribe,
        duration_probe=lambda _path: 10.0,
        trailing_silence_probe=broken_probe,
    ).transcribe(audio)

    assert call["clip_timestamps"] == [0.0, 10.0]


def test_ffmpeg_probe_only_returns_silence_reaching_media_end(
    tmp_path: Path, monkeypatch
) -> None:
    audio = tmp_path / "speech.m4a"
    audio.touch()
    stderr = """
[silencedetect] silence_start: 10.0
[silencedetect] silence_end: 13.0 | silence_duration: 3.0
[silencedetect] silence_start: 80.0
[silencedetect] silence_end: 99.8 | silence_duration: 19.8
"""

    monkeypatch.setattr("youtubetext.asr.mlx_whisper.shutil.which", lambda _name: "/ffmpeg")
    monkeypatch.setattr(
        "youtubetext.asr.mlx_whisper.subprocess.run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": 0, "stderr": stderr}
        )(),
    )

    assert _ffmpeg_trailing_silence(audio, 100.0) == 80.0


def test_ffmpeg_probe_ignores_internal_silence(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "speech.m4a"
    audio.touch()
    stderr = """
[silencedetect] silence_start: 10.0
[silencedetect] silence_end: 13.0 | silence_duration: 3.0
"""

    monkeypatch.setattr("youtubetext.asr.mlx_whisper.shutil.which", lambda _name: "/ffmpeg")
    monkeypatch.setattr(
        "youtubetext.asr.mlx_whisper.subprocess.run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": 0, "stderr": stderr}
        )(),
    )

    assert _ffmpeg_trailing_silence(audio, 100.0) is None
