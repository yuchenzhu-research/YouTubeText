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


def test_standard_huggingface_snapshot_is_reused_without_download(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed"
    hub_root = tmp_path / "huggingface" / "hub"
    repository = hub_root / "models--mlx-community--whisper-large-v3-turbo"
    snapshot = repository / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "weights.safetensors").write_bytes(b"weights")
    (repository / "refs").mkdir()
    (repository / "refs" / "main").write_text("abc123\n", encoding="utf-8")
    calls: list[str] = []

    cache = WhisperModelCache(
        managed_root,
        hub_cache_root=hub_root,
        downloader=lambda repository, _destination: calls.append(repository),
    )

    assert cache.is_cached("large-v3-turbo")
    assert cache.cached_directory("large-v3-turbo") == snapshot
    assert cache.ensure("large-v3-turbo") == snapshot
    assert calls == []
    assert not (managed_root / "large-v3-turbo").exists()


def test_huggingface_cache_skips_incomplete_main_snapshot(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    repository = hub_root / "models--mlx-community--whisper-base-mlx"
    incomplete = repository / "snapshots" / "new"
    complete = repository / "snapshots" / "old"
    incomplete.mkdir(parents=True)
    complete.mkdir(parents=True)
    (incomplete / "config.json").write_text("{}", encoding="utf-8")
    (complete / "config.json").write_text("{}", encoding="utf-8")
    (complete / "weights.npz").write_bytes(b"weights")
    (repository / "refs").mkdir()
    (repository / "refs" / "main").write_text("new", encoding="utf-8")

    cache = WhisperModelCache(tmp_path / "managed", hub_cache_root=hub_root)

    assert cache.cached_directory("base") == complete


def test_custom_cache_without_hub_root_remains_hermetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    snapshot = (
        fake_home
        / ".cache/huggingface/hub/models--mlx-community--whisper-small-mlx"
        / "snapshots/revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "weights.npz").write_bytes(b"weights")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cache = WhisperModelCache(tmp_path / "custom")

    assert not cache.is_cached("small")


def test_default_cache_discovers_standard_huggingface_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    snapshot = (
        fake_home
        / ".cache/huggingface/hub/models--mlx-community--whisper-base-mlx"
        / "snapshots/revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "weights.npz").write_bytes(b"weights")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cache = WhisperModelCache()

    assert cache.cached_directory("base") == snapshot
