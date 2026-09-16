"""Windows OCR adapter backed by RapidOCR and ONNX Runtime."""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..runtime import run_blocking
from .types import BoundingBox, OCRFrame, OCRObservation
from .vision import vision_language_codes

RAPID_OCR_REVISION = "rapidocr-ppocrv6-small-multilingual-v2-language-checked"
# RapidOCR >=3.9 ships PP-OCRv6 small by default. Its recognition model is
# multilingual, but does not cover every language accepted by the CLI.
RAPID_OCR_LANGUAGES = frozenset(
    {"zh", "en", "es", "ja", "fr", "de", "pt", "it", "vi"}
)


class RapidOCRUnavailableError(RuntimeError):
    """Raised when the Windows OCR runtime is not installed."""


class _RapidResult(Protocol):
    boxes: Any
    txts: Any
    scores: Any
    img: Any


class _RapidEngine(Protocol):
    def __call__(self, image_path: str) -> _RapidResult: ...


EngineFactory = Callable[[], _RapidEngine]


def rapid_language_codes(language: str | None) -> tuple[str, ...]:
    """Resolve a CLI language only if the bundled PP-OCRv6 model covers it."""

    codes = vision_language_codes(language)
    _validate_languages(codes)
    return codes


def _validate_languages(languages: Sequence[str]) -> None:
    for language in languages:
        code = language.strip().replace("_", "-").casefold()
        if not code or code == "auto":
            continue
        base = code.split("-", 1)[0]
        if base not in RAPID_OCR_LANGUAGES:
            raise ValueError(
                f"Windows RapidOCR PP-OCRv6 small does not support OCR language "
                f"'{base}'; use platform captions or --mode whisper"
            )


class RapidOCRBackend:
    """Reuse one lazily-created multilingual RapidOCR engine."""

    def __init__(self, *, engine_factory: EngineFactory | None = None) -> None:
        self._engine_factory = engine_factory or _load_engine
        self._engine: _RapidEngine | None = None
        self._engine_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @property
    def checkpoint_revision(self) -> str:
        signature = "|".join(
            (
                RAPID_OCR_REVISION,
                _package_version("rapidocr"),
                _package_version("onnxruntime"),
            )
        )
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()

    def recognize_images(
        self,
        image_paths: Sequence[str | Path],
        *,
        languages: Sequence[str] = (),
        accurate: bool = True,
        minimum_text_height: float = 0.012,
    ) -> tuple[OCRFrame, ...]:
        """Recognize images in order while isolating per-frame failures."""

        del accurate
        if not 0 <= minimum_text_height <= 1:
            raise ValueError("minimum_text_height must be between zero and one")
        _validate_languages(languages)
        if not image_paths:
            return ()

        frames: list[OCRFrame] = []
        # RapidOCR 3.9 mutates detector preprocessing state during inference.
        # Keep one lazy engine reusable without assuming it is thread-safe.
        with self._inference_lock:
            engine = self._get_engine()
            for image_path in image_paths:
                path = str(Path(image_path).expanduser().resolve())
                try:
                    result = engine(path)
                    observations = _observations(
                        result,
                        minimum_text_height=minimum_text_height,
                    )
                    frames.append(OCRFrame(path, observations))
                except Exception as error:
                    detail = " ".join(str(error).split()) or error.__class__.__name__
                    frames.append(OCRFrame(path, (), error=detail))
        return tuple(frames)

    async def recognize_images_async(
        self,
        image_paths: Sequence[str | Path],
        *,
        languages: Sequence[str] = (),
        accurate: bool = True,
        minimum_text_height: float = 0.012,
    ) -> tuple[OCRFrame, ...]:
        return await run_blocking(
            self.recognize_images,
            image_paths,
            languages=languages,
            accurate=accurate,
            minimum_text_height=minimum_text_height,
        )

    def _get_engine(self) -> _RapidEngine:
        engine = self._engine
        if engine is not None:
            return engine
        with self._engine_lock:
            if self._engine is None:
                self._engine = self._engine_factory()
            return self._engine


def _load_engine() -> _RapidEngine:
    try:
        from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

        # Language coverage is tied to this model family. Keep both detection
        # and recognition pinned even if a future 3.x release changes defaults.
        return RapidOCR(
            params={
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.model_type": ModelType.SMALL,
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.model_type": ModelType.SMALL,
                "Rec.ocr_version": OCRVersion.PPOCRV6,
            }
        )
    except ImportError as error:  # pragma: no cover - Windows packaging failure
        raise RapidOCRUnavailableError(
            "RapidOCR or ONNX Runtime is not installed correctly; "
            "reinstall YouTubeText on Windows"
        ) from error


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _observations(
    result: _RapidResult,
    *,
    minimum_text_height: float,
) -> tuple[OCRObservation, ...]:
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None and texts is None and scores is None:
        return ()
    if boxes is None or texts is None or scores is None:
        raise ValueError("RapidOCR returned incomplete observations")
    if len(boxes) != len(texts) or len(boxes) != len(scores):
        raise ValueError("RapidOCR returned mismatched observation counts")
    if not len(boxes):
        return ()

    image = getattr(result, "img", None)
    shape = getattr(image, "shape", ())
    if len(shape) < 2:
        raise ValueError("RapidOCR returned no image dimensions")
    image_height = _positive_number(shape[0], "image height")
    image_width = _positive_number(shape[1], "image width")

    observations: list[OCRObservation] = []
    for box, text, score in zip(boxes, texts, scores):
        clean_text = " ".join(str(text or "").split())
        if not clean_text:
            continue
        normalized_box = _normalized_box(box, image_width, image_height)
        if normalized_box.height < minimum_text_height:
            continue
        confidence = float(score)
        if not math.isfinite(confidence):
            continue
        observations.append(
            OCRObservation(
                text=clean_text,
                confidence=min(1.0, max(0.0, confidence)),
                bounding_box=normalized_box,
            )
        )
    return tuple(observations)


def _normalized_box(box: Any, width: float, height: float) -> BoundingBox:
    try:
        raw_points = tuple(tuple(point) for point in box)
        if len(raw_points) != 4 or any(len(point) != 2 for point in raw_points):
            raise ValueError
        points = tuple((float(point[0]), float(point[1])) for point in raw_points)
    except (TypeError, ValueError) as error:
        raise ValueError("RapidOCR returned an invalid bounding box") from error
    if not all(
        math.isfinite(value) for point in points for value in point
    ):
        raise ValueError("RapidOCR returned an invalid bounding box")

    x_coordinates = tuple(min(width, max(0.0, point[0])) for point in points)
    y_coordinates = tuple(min(height, max(0.0, point[1])) for point in points)
    left = min(x_coordinates)
    right = max(x_coordinates)
    top = min(y_coordinates)
    bottom = max(y_coordinates)
    if right <= left or bottom <= top:
        raise ValueError("RapidOCR returned an empty bounding box")
    return BoundingBox(
        x=left / width,
        y=(height - bottom) / height,
        width=(right - left) / width,
        height=(bottom - top) / height,
    )


def _positive_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"RapidOCR returned an invalid {field}") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"RapidOCR returned an invalid {field}")
    return number
