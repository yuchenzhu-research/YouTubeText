"""Opt-in, offline Windows inference using locally generated test media.

The manual workflow downloads the base ASR model before setting
``HF_HUB_OFFLINE=1``. Normal push/PR tests skip this module.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from youtubetext.asr import FasterWhisperASR
from youtubetext.ocr import RapidOCRBackend


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("YOUTUBETEXT_RUN_REAL_MODELS") != "1",
    reason="opt-in Windows real-model smoke test",
)


@pytest.fixture(autouse=True)
def no_inference_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("real-model inference attempted to access the network")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)


def test_rapidocr_reads_locally_rendered_text(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    assert font_path.is_file(), f"Windows Arial font is unavailable: {font_path}"

    image_path = tmp_path / "generated-text.png"
    image = Image.new("RGB", (1600, 350), "white")
    ImageDraw.Draw(image).text(
        (80, 95),
        "HELLO WORLD",
        font=ImageFont.truetype(str(font_path), 112),
        fill="black",
    )
    image.save(image_path)

    frames = RapidOCRBackend().recognize_images([image_path], languages=("en",))

    assert len(frames) == 1
    assert frames[0].error is None
    text = " ".join(observation.text.upper() for observation in frames[0].observations)
    assert "HELLO" in text and "WORLD" in text, text


def test_faster_whisper_transcribes_locally_synthesized_speech(tmp_path: Path) -> None:
    audio_path = tmp_path / "generated-speech.wav"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Add-Type -AssemblyName System.Speech; "
        "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { "
        "$voice.SetOutputToWaveFile($env:YOUTUBETEXT_SMOKE_AUDIO); "
        "$voice.Speak('The quick brown fox jumps over the lazy dog.'); "
        "} finally { $voice.Dispose() }"
    )
    environment = os.environ.copy()
    environment["YOUTUBETEXT_SMOKE_AUDIO"] = str(audio_path)
    generated = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert generated.returncode == 0, generated.stderr
    assert audio_path.is_file() and audio_path.stat().st_size > 1000

    cache_root = Path(os.environ["YOUTUBETEXT_REAL_MODEL_CACHE"]) / "faster"
    result = FasterWhisperASR(
        model="base", cache_root=cache_root, cuda_probe=lambda: False
    ).transcribe(audio_path, language="en")

    assert result.model == "base"
    assert result.language == "en"
    text = " ".join(segment.text.casefold() for segment in result.segments)
    assert "brown" in text and "fox" in text, text
