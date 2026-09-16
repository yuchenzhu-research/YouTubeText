"""Opt-in, offline Windows inference using locally generated test media.

The manual workflow downloads the base ASR model before setting
``HF_HUB_OFFLINE=1``. Normal push/PR tests skip this module.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

import youtubetext.cli as cli
from youtubetext.acquisition import TranscriptPipeline
from youtubetext.asr import FasterWhisperASR
from youtubetext.backends import default_local_backends
from youtubetext.domain import SourceMetadata
from youtubetext.media import MediaPurpose
from youtubetext.ocr import RapidOCRBackend
from youtubetext.sources import SourceResult


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


@pytest.fixture(scope="module")
def generated_speech(tmp_path_factory: pytest.TempPathFactory) -> Path:
    audio_path = tmp_path_factory.mktemp("offline-speech") / "generated-speech.wav"
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
    return audio_path


def test_faster_whisper_transcribes_locally_synthesized_speech(
    generated_speech: Path,
) -> None:
    audio_path = generated_speech

    cache_root = Path(os.environ["YOUTUBETEXT_REAL_MODEL_CACHE"]) / "faster"
    result = FasterWhisperASR(
        model="base", cache_root=cache_root, cuda_probe=lambda: False
    ).transcribe(audio_path, language="en")

    assert result.model == "base"
    assert result.language == "en"
    text = " ".join(segment.text.casefold() for segment in result.segments)
    assert "brown" in text and "fox" in text, text


def test_windows_cli_exports_offline_speech_with_real_whisper(
    generated_speech: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise CLI → pipeline → local ASR → four exports without a platform request."""

    url = "https://www.youtube.com/watch?v=offline-speech"

    class OfflineSource:
        def fetch(self, requested_url: str, **_options: object) -> SourceResult:
            return SourceResult(
                SourceMetadata(
                    requested_url,
                    "youtube",
                    "offline-speech",
                    "Offline speech smoke",
                    author="Windows System.Speech",
                    webpage_url=requested_url,
                )
            )

    class OfflineMedia:
        async def download(
            self,
            _url: str,
            directory: Path,
            purpose: MediaPurpose,
        ) -> Path:
            assert purpose is MediaPurpose.AUDIO
            directory.mkdir(parents=True, exist_ok=True)
            copied = directory / "generated-speech.wav"
            shutil.copyfile(generated_speech, copied)
            return copied

    cache_root = Path(os.environ["YOUTUBETEXT_REAL_MODEL_CACHE"]) / "faster"
    backends = replace(
        default_local_backends(),
        asr_factory=lambda model: FasterWhisperASR(
            model,
            cache_root=cache_root,
            cuda_probe=lambda: False,
        ),
    )
    pipeline = TranscriptPipeline(
        sources=OfflineSource(),
        media=OfflineMedia(),
        backends=backends,
        temp_root=tmp_path / "work",
    )
    monkeypatch.setattr(cli, "_transcript_pipeline", lambda *_args, **_kwargs: pipeline)

    output_root = tmp_path / "exports"
    result = CliRunner().invoke(
        cli.main,
        [
            url,
            "--mode",
            "whisper",
            "--language",
            "en",
            "--whisper-model",
            "base",
            "--jobs",
            "1",
            "--output",
            str(output_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert len(payload["results"]) == 1
    exported = payload["results"][0]
    assert exported["success"] is True
    assert exported["method"] == "faster-whisper"
    assert exported["language"] == "en"

    paths = {name: Path(path) for name, path in exported["output"].items()}
    directory = paths.pop("directory")
    assert directory.parent == output_root.resolve()
    assert {path.name for path in directory.iterdir()} == {
        "transcript.md",
        "transcript-clean.md",
        "transcript.txt",
        "metadata.json",
    }
    assert all(path.is_file() and path.stat().st_size > 0 for path in paths.values())

    metadata = json.loads(paths["metadata"].read_text(encoding="utf-8"))
    assert metadata["extraction_method"] == "faster-whisper"
    assert metadata["segment_count"] > 0
    assert metadata["language"] == "en"
    transcript_text = paths["text"].read_text(encoding="utf-8").casefold()
    assert "brown" in transcript_text and "fox" in transcript_text
    assert "## Transcript" in paths["markdown"].read_text(encoding="utf-8")
    assert "**[" not in paths["clean_markdown"].read_text(encoding="utf-8")
