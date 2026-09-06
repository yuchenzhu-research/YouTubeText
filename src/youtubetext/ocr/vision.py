"""Python adapter for the native macOS Apple Vision OCR command."""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .types import BoundingBox, OCRFrame, OCRObservation

VISION_OCR_ENV = "YOUTUBETEXT_VISION_OCR"
VISION_OCR_EXECUTABLE = "youtubetext-vision-ocr"


class OCRUnavailableError(RuntimeError):
    """Raised when the Apple Vision helper cannot be used."""


class OCRExecutionError(RuntimeError):
    """Raised when the helper fails or violates its JSON contract."""


def vision_language_codes(language: str | None) -> tuple[str, ...]:
    """Convert a user language choice to likely Apple Vision BCP-47 tags."""

    code = (language or "").strip().replace("_", "-")
    lowered = code.casefold()
    if not code or lowered == "auto":
        return ()

    aliases = {
        "zh": ("zh-Hans", "zh-Hant"),
        "zh-cn": ("zh-Hans",),
        "zh-sg": ("zh-Hans",),
        "zh-hans": ("zh-Hans",),
        "zh-tw": ("zh-Hant",),
        "zh-hk": ("zh-Hant",),
        "zh-mo": ("zh-Hant",),
        "zh-hant": ("zh-Hant",),
        "en": ("en-US",),
        "es": ("es-ES",),
        "pt": ("pt-BR", "pt-PT"),
    }
    if lowered in aliases:
        return aliases[lowered]

    regional = {
        "ar": "ar-SA",
        "de": "de-DE",
        "fr": "fr-FR",
        "hi": "hi-IN",
        "it": "it-IT",
        "ja": "ja-JP",
        "ko": "ko-KR",
        "ru": "ru-RU",
        "th": "th-TH",
        "tr": "tr-TR",
        "vi": "vi-VN",
    }
    if len(code) == 2:
        return (regional.get(lowered, lowered),)
    return (code,)


def _project_root() -> Path:
    # .../src/youtubetext/ocr/vision.py -> repository root
    return Path(__file__).resolve().parents[3]


def _binary_candidates() -> Iterable[Path]:
    configured = os.environ.get(VISION_OCR_ENV, "").strip()
    if configured:
        yield Path(configured).expanduser()

    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        yield executable_dir / VISION_OCR_EXECUTABLE
        bundle_root = Path(getattr(sys, "_MEIPASS", executable_dir))
        yield bundle_root / VISION_OCR_EXECUTABLE

    yield _project_root() / "bin" / VISION_OCR_EXECUTABLE

    on_path = shutil.which(VISION_OCR_EXECUTABLE)
    if on_path:
        yield Path(on_path)


def find_vision_ocr_binary() -> Path | None:
    """Return the first executable helper in the documented search order."""

    for candidate in _binary_candidates():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        except OSError:
            continue
    return None


def _finite_number(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OCRExecutionError(f"Apple Vision OCR returned an invalid {field}") from exc
    if not math.isfinite(number):
        raise OCRExecutionError(f"Apple Vision OCR returned an invalid {field}")
    return number


def _parse_response(stdout: str, *, expected_frames: int) -> tuple[OCRFrame, ...]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise OCRExecutionError("Apple Vision OCR returned invalid JSON") from exc

    if not isinstance(payload, dict) or payload.get("engine") != "apple-vision":
        raise OCRExecutionError("Apple Vision OCR returned an unknown response")
    raw_frames = payload.get("frames")
    if not isinstance(raw_frames, list) or len(raw_frames) != expected_frames:
        raise OCRExecutionError("Apple Vision OCR returned an unexpected frame count")

    frames: list[OCRFrame] = []
    for raw_frame in raw_frames:
        if not isinstance(raw_frame, dict) or not isinstance(raw_frame.get("path"), str):
            raise OCRExecutionError("Apple Vision OCR returned an invalid frame")
        raw_observations = raw_frame.get("observations")
        if not isinstance(raw_observations, list):
            raise OCRExecutionError("Apple Vision OCR returned invalid observations")

        observations: list[OCRObservation] = []
        for raw_observation in raw_observations:
            if not isinstance(raw_observation, dict):
                raise OCRExecutionError("Apple Vision OCR returned an invalid observation")
            text = raw_observation.get("text")
            raw_box = raw_observation.get("boundingBox")
            if not isinstance(text, str) or not isinstance(raw_box, dict):
                raise OCRExecutionError("Apple Vision OCR returned an invalid observation")
            clean_text = text.strip()
            if not clean_text:
                continue
            observations.append(
                OCRObservation(
                    text=clean_text,
                    confidence=_finite_number(
                        raw_observation.get("confidence"), field="confidence"
                    ),
                    bounding_box=BoundingBox(
                        x=_finite_number(raw_box.get("x"), field="bounding box"),
                        y=_finite_number(raw_box.get("y"), field="bounding box"),
                        width=_finite_number(raw_box.get("width"), field="bounding box"),
                        height=_finite_number(raw_box.get("height"), field="bounding box"),
                    ),
                )
            )

        error = raw_frame.get("error")
        if error is not None and not isinstance(error, str):
            raise OCRExecutionError("Apple Vision OCR returned an invalid frame error")
        frames.append(
            OCRFrame(
                path=raw_frame["path"],
                observations=tuple(observations),
                error=error or None,
            )
        )
    return tuple(frames)


class MacVisionOCR:
    """Invoke Apple Vision through the bundled Swift JSON command."""

    def __init__(
        self,
        binary_path: str | os.PathLike[str] | None = None,
        *,
        timeout_seconds: float = 300.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._explicit_binary = (
            Path(binary_path).expanduser().resolve() if binary_path is not None else None
        )
        self._timeout_seconds = float(timeout_seconds)

    @property
    def binary_path(self) -> Path | None:
        if self._explicit_binary is not None:
            try:
                if self._explicit_binary.is_file() and os.access(self._explicit_binary, os.X_OK):
                    return self._explicit_binary
            except OSError:
                pass
            return None
        return find_vision_ocr_binary()

    @property
    def available(self) -> bool:
        return platform.system() == "Darwin" and self.binary_path is not None

    def recognize_images(
        self,
        image_paths: Sequence[str | os.PathLike[str]],
        *,
        languages: Sequence[str] = (),
        accurate: bool = True,
        minimum_text_height: float = 0.012,
    ) -> tuple[OCRFrame, ...]:
        """Recognize a batch of local images while preserving input order."""

        if not image_paths:
            return ()
        if platform.system() != "Darwin":
            raise OCRUnavailableError("Apple Vision OCR requires macOS")
        binary = self.binary_path
        if binary is None:
            raise OCRUnavailableError(
                "Apple Vision OCR is not built; run scripts/build_vision_ocr.sh"
            )
        if not 0.0 <= minimum_text_height <= 1.0:
            raise ValueError("minimum_text_height must be between 0 and 1")

        paths = [str(Path(path).expanduser().resolve()) for path in image_paths]
        for path in paths:
            if not Path(path).is_file():
                raise FileNotFoundError(path)

        normalized_languages = list(
            dict.fromkeys(item.strip() for item in languages if item.strip())
        )
        request = json.dumps(
            {
                "images": paths,
                "languages": normalized_languages,
                "accurate": bool(accurate),
                "minimumTextHeight": float(minimum_text_height),
            },
            ensure_ascii=False,
        )

        try:
            completed = subprocess.run(
                [str(binary)],
                input=request,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise OCRExecutionError("Apple Vision OCR timed out") from exc
        except OSError as exc:
            raise OCRExecutionError(f"Could not start Apple Vision OCR: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "native OCR failed").strip()
            try:
                parsed_error = json.loads(detail)
                if isinstance(parsed_error, dict) and parsed_error.get("error"):
                    detail = str(parsed_error["error"])
            except json.JSONDecodeError:
                pass
            raise OCRExecutionError(detail[:500])

        return _parse_response(completed.stdout, expected_frames=len(paths))

    async def recognize_images_async(
        self,
        image_paths: Sequence[str | os.PathLike[str]],
        *,
        languages: Sequence[str] = (),
        accurate: bool = True,
        minimum_text_height: float = 0.012,
    ) -> tuple[OCRFrame, ...]:
        """Asynchronous wrapper for task-queue integrations."""

        return await asyncio.to_thread(
            self.recognize_images,
            image_paths,
            languages=languages,
            accurate=accurate,
            minimum_text_height=minimum_text_height,
        )
