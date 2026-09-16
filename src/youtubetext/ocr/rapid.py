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

RAPID_OCR_REVISION = "rapidocr-ppocrv6-small-multilingual-v1"


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

        del languages, accurate
        if not 0 <= minimum_text_height <= 1:
            raise ValueError("minimum_text_height must be between zero and one")
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
        from rapidocr import RapidOCR
    except ImportError as error:  # pragma: no cover - Windows packaging failure
        raise RapidOCRUnavailableError(
            "RapidOCR is not installed; reinstall YouTubeText on Windows"
        ) from error
    return RapidOCR()


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
        points = tuple((float(point[0]), float(point[1])) for point in box)
    except (TypeError, ValueError, IndexError) as error:
        raise ValueError("RapidOCR returned an invalid bounding box") from error
    if len(points) < 2 or not all(
        math.isfinite(value) for point in points for value in point
    ):
        raise ValueError("RapidOCR returned an invalid bounding box")

    left = min(max(0.0, point[0]) for point in points)
    right = max(min(width, point[0]) for point in points)
    top = min(max(0.0, point[1]) for point in points)
    bottom = max(min(height, point[1]) for point in points)
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
