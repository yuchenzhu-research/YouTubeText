from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from youtubetext.asr import MLXWhisperASR
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
        "auto", memory_bytes=16 * GIB, transcribe_callable=fake_transcribe
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
    assert not backend.model_cached


def test_explicit_language_is_passed_to_engine(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    seen: dict[str, Any] = {}

    def fake_transcribe(_path: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"text": "hola", "segments": []}

    result = MLXWhisperASR(
        "small", memory_bytes=48 * GIB, transcribe_callable=fake_transcribe
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

    backend = MLXWhisperASR(transcribe_callable=fake_transcribe)
    with pytest.raises(FileNotFoundError):
        backend.transcribe(tmp_path / "missing.wav")
    assert not called


def test_empty_engine_result_is_an_error(tmp_path: Path) -> None:
    audio = tmp_path / "silence.wav"
    audio.touch()
    backend = MLXWhisperASR(
        transcribe_callable=lambda *_args, **_kwargs: {"text": "", "segments": []}
    )
    with pytest.raises(RuntimeError, match="no recognizable speech"):
        backend.transcribe(audio)


def test_asr_result_has_no_summary_responsibility(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    result = MLXWhisperASR(
        transcribe_callable=lambda *_args, **_kwargs: {"text": "transcript"}
    ).transcribe(audio)
    assert result.text == "transcript"
    assert not hasattr(result, "summary")
