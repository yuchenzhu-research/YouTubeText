from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from youtubetext.ocr import MacVisionOCR, OCRExecutionError, vision_language_codes


def make_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_language_choices_map_to_vision_tags() -> None:
    assert vision_language_codes("auto") == ()
    assert vision_language_codes("zh_CN") == ("zh-Hans",)
    assert vision_language_codes("zh-TW") == ("zh-Hant",)
    assert vision_language_codes("zh") == ("zh-Hans", "zh-Hant")
    assert vision_language_codes("es") == ("es-ES",)
    assert vision_language_codes("nl-NL") == ("nl-NL",)


def test_adapter_sends_json_and_parses_the_native_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("youtubetext.ocr.vision.platform.system", lambda: "Darwin")
    helper = make_executable(
        tmp_path / "fake-vision",
        """#!/usr/bin/env python3
import json, sys
request = json.load(sys.stdin)
frames = []
for path in request["images"]:
    frames.append({
        "path": path,
        "observations": [{
            "text": "  繁體字幕  ",
            "confidence": 0.94,
            "boundingBox": {"x": 0.1, "y": 0.2, "width": 0.7, "height": 0.1}
        }],
        "error": None,
    })
json.dump({"engine": "apple-vision", "frames": frames}, sys.stdout)
""",
    )
    first = tmp_path / "一.jpg"
    second = tmp_path / "two.jpg"
    first.write_bytes(b"not decoded by fake helper")
    second.write_bytes(b"not decoded by fake helper")

    frames = MacVisionOCR(helper).recognize_images(
        [first, second], languages=("zh-Hant", "zh-Hant")
    )

    assert [frame.path for frame in frames] == [str(first.resolve()), str(second.resolve())]
    assert frames[0].observations[0].text == "繁體字幕"
    assert frames[0].observations[0].confidence == pytest.approx(0.94)
    assert frames[0].observations[0].bounding_box.y == pytest.approx(0.2)


def test_adapter_surfaces_native_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("youtubetext.ocr.vision.platform.system", lambda: "Darwin")
    helper = make_executable(
        tmp_path / "failing-vision",
        """#!/usr/bin/env python3
import json, sys
json.dump({"error": "Vision rejected the image"}, sys.stderr)
raise SystemExit(2)
""",
    )
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"placeholder")

    with pytest.raises(OCRExecutionError, match="Vision rejected the image"):
        MacVisionOCR(helper).recognize_images([image])


def test_adapter_rejects_malformed_success_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("youtubetext.ocr.vision.platform.system", lambda: "Darwin")
    helper = make_executable(
        tmp_path / "malformed-vision",
        """#!/usr/bin/env python3
print('{"engine": "apple-vision", "frames": []}')
""",
    )
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"placeholder")

    with pytest.raises(OCRExecutionError, match="unexpected frame count"):
        MacVisionOCR(helper).recognize_images([image])


def test_empty_batch_does_not_require_a_binary() -> None:
    assert MacVisionOCR("/does/not/exist").recognize_images([]) == ()


def test_request_options_are_validated(tmp_path: Path) -> None:
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"placeholder")
    with pytest.raises(ValueError, match="timeout_seconds"):
        MacVisionOCR(timeout_seconds=0)
