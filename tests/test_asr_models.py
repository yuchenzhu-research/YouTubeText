from __future__ import annotations

from pathlib import Path

import pytest

from youtubetext.asr import MODELS, WhisperModelCache, select_model
from youtubetext.asr.models import GIB


def test_catalog_exposes_supported_directories_and_costs() -> None:
    assert tuple(MODELS) == ("base", "small", "large-v3-turbo")
    assert MODELS["base"].directory_name == "base"
    assert MODELS["base"].download_bytes == 144_000_000
    assert MODELS["small"].directory_name == "small"
    assert MODELS["small"].download_bytes == 481_000_000
    assert MODELS["large-v3-turbo"].directory_name == "large-v3-turbo"
    assert MODELS["large-v3-turbo"].download_bytes == 1_610_000_000
    assert MODELS["large-v3-turbo"].runtime_memory_bytes == 6 * GIB


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        (4 * GIB, "base"),
        (8 * GIB, "small"),
        (15 * GIB, "small"),
        (16 * GIB, "large-v3-turbo"),
        (48 * GIB, "large-v3-turbo"),
    ],
)
def test_auto_model_selection_by_physical_memory(memory: int, expected: str) -> None:
    assert select_model("auto", memory_bytes=memory).name == expected


def test_explicit_model_overrides_memory_policy() -> None:
    assert select_model("base", memory_bytes=48 * GIB).name == "base"
    assert select_model("small", memory_bytes=48 * GIB).name == "small"
    with pytest.raises(ValueError, match="unknown Whisper model"):
        select_model("medium", memory_bytes=48 * GIB)


def test_model_cache_downloads_on_first_ensure_and_reuses_files(tmp_path: Path) -> None:
    calls: list[tuple[str, Path]] = []

    def download(repository: str, destination: Path) -> None:
        calls.append((repository, destination))
        (destination / "config.json").write_text("{}", encoding="utf-8")
        (destination / "weights.npz").write_bytes(b"weights")

    cache = WhisperModelCache(tmp_path, downloader=download)
    assert not cache.is_cached("base")
    assert calls == []

    expected = tmp_path / "base"
    assert cache.ensure("base") == expected
    assert cache.ensure("base") == expected
    assert calls == [(MODELS["base"].repository, expected)]


def test_incomplete_model_download_is_rejected(tmp_path: Path) -> None:
    def incomplete_download(_repository: str, destination: Path) -> None:
        (destination / "config.json").write_text("{}", encoding="utf-8")

    cache = WhisperModelCache(tmp_path, downloader=incomplete_download)
    with pytest.raises(RuntimeError, match="incomplete"):
        cache.ensure("small")
