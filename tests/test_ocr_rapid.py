from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from youtubetext.ocr import RapidOCRBackend


@dataclass
class FakeImage:
    shape: tuple[int, int, int] = (1000, 2000, 3)


@dataclass
class FakeResult:
    boxes: object
    txts: object
    scores: object
    img: object = field(default_factory=FakeImage)


class FakeEngine:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, image_path: str) -> FakeResult:
        self.paths.append(image_path)
        if image_path.endswith("broken.jpg"):
            raise RuntimeError("broken image")
        return FakeResult(
            boxes=(
                ((200, 700), (1000, 700), (1000, 800), (200, 800)),
                ((0, 990), (100, 990), (100, 995), (0, 995)),
            ),
            txts=("  字幕 text  ", "too small"),
            scores=(0.93, 0.8),
        )


def test_rapidocr_normalizes_coordinates_and_reuses_lazy_engine(tmp_path: Path) -> None:
    created = 0
    engine = FakeEngine()

    def factory() -> FakeEngine:
        nonlocal created
        created += 1
        return engine

    backend = RapidOCRBackend(engine_factory=factory)
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"

    first_frames = backend.recognize_images((first,))
    second_frames = backend.recognize_images((second,))

    assert created == 1
    assert engine.paths == [str(first.resolve()), str(second.resolve())]
    observation = first_frames[0].observations[0]
    assert observation.text == "字幕 text"
    assert observation.confidence == pytest.approx(0.93)
    assert observation.bounding_box.x == pytest.approx(0.1)
    assert observation.bounding_box.y == pytest.approx(0.2)
    assert observation.bounding_box.width == pytest.approx(0.4)
    assert observation.bounding_box.height == pytest.approx(0.1)
    assert len(first_frames[0].observations) == 1
    assert second_frames[0].path == str(second.resolve())


@pytest.mark.asyncio
async def test_rapidocr_async_preserves_order_and_isolates_frame_errors(
    tmp_path: Path,
) -> None:
    backend = RapidOCRBackend(engine_factory=FakeEngine)
    good = tmp_path / "good.jpg"
    broken = tmp_path / "broken.jpg"

    frames = await backend.recognize_images_async((good, broken))

    assert [Path(frame.path).name for frame in frames] == ["good.jpg", "broken.jpg"]
    assert frames[0].observations
    assert frames[0].error is None
    assert frames[1].observations == ()
    assert frames[1].error == "broken image"


def test_rapidocr_empty_input_does_not_create_engine() -> None:
    backend = RapidOCRBackend(
        engine_factory=lambda: (_ for _ in ()).throw(
            AssertionError("engine must stay lazy")
        )
    )

    assert backend.recognize_images(()) == ()


def test_rapidocr_rejects_invalid_minimum_height() -> None:
    backend = RapidOCRBackend(engine_factory=FakeEngine)

    with pytest.raises(ValueError, match="between zero and one"):
        backend.recognize_images(("frame.jpg",), minimum_text_height=2)


def test_rapidocr_marks_malformed_result_as_frame_error(tmp_path: Path) -> None:
    class MalformedEngine:
        def __call__(self, _image_path: str) -> FakeResult:
            return FakeResult(
                boxes=(((0, 0), (10, 10)),),
                txts=("text", "extra"),
                scores=(0.9,),
            )

    backend = RapidOCRBackend(engine_factory=MalformedEngine)

    frame = backend.recognize_images((tmp_path / "frame.jpg",))[0]

    assert frame.observations == ()
    assert frame.error == "RapidOCR returned mismatched observation counts"


def test_rapidocr_checkpoint_revision_is_stable() -> None:
    backend = RapidOCRBackend(engine_factory=FakeEngine)

    assert backend.checkpoint_revision == backend.checkpoint_revision
    assert len(backend.checkpoint_revision) == 64


def test_rapidocr_serializes_calls_to_the_shared_engine(tmp_path: Path) -> None:
    class ConcurrentEngine:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0

        def __call__(self, _image_path: str) -> FakeResult:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return FakeResult(boxes=None, txts=None, scores=None)

    engine = ConcurrentEngine()
    backend = RapidOCRBackend(engine_factory=lambda: engine)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(backend.recognize_images, (tmp_path / f"{index}.jpg",))
            for index in range(2)
        ]
        for future in futures:
            future.result(timeout=1)

    assert engine.peak == 1
