"""Whisper model catalogue, selection and on-demand local caching.

This module owns model *availability*, not inference.  Keeping that concern
separate makes the ASR adapter replaceable and lets callers inspect disk and
memory costs without importing MLX.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

GIB = 1024**3


@dataclass(frozen=True, slots=True)
class WhisperModel:
    """A supported MLX Whisper model and its approximate resource cost."""

    name: str
    repository: str
    directory_name: str
    download_bytes: int
    runtime_memory_bytes: int

    @property
    def download_gb(self) -> float:
        return self.download_bytes / 1_000_000_000

    @property
    def runtime_memory_gib(self) -> float:
        return self.runtime_memory_bytes / GIB


_MODELS = {
    "base": WhisperModel(
        name="base",
        repository="mlx-community/whisper-base-mlx",
        directory_name="base",
        download_bytes=144_000_000,
        runtime_memory_bytes=1 * GIB,
    ),
    "small": WhisperModel(
        name="small",
        repository="mlx-community/whisper-small-mlx",
        directory_name="small",
        download_bytes=481_000_000,
        runtime_memory_bytes=2 * GIB,
    ),
    "large-v3-turbo": WhisperModel(
        name="large-v3-turbo",
        repository="mlx-community/whisper-large-v3-turbo",
        directory_name="large-v3-turbo",
        download_bytes=1_610_000_000,
        runtime_memory_bytes=6 * GIB,
    ),
}

MODELS: Mapping[str, WhisperModel] = MappingProxyType(_MODELS)
WEIGHT_FILENAMES = ("weights.safetensors", "weights.npz")


def physical_memory_bytes() -> int:
    """Return physical memory without importing platform-specific packages."""

    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        # The product only supports Apple Silicon Macs; this conservative
        # fallback still chooses a usable model in unusual test environments.
        return 8 * GIB


def select_model(
    requested: str = "auto",
    *,
    memory_bytes: int | None = None,
) -> WhisperModel:
    """Select a model, respecting an explicit model name.

    Automatic policy is deliberately simple and predictable:

    - under 8 GiB: ``base``
    - 8 GiB through under 16 GiB: ``small``
    - 16 GiB and above: ``large-v3-turbo``
    """

    choice = (requested or "auto").strip().lower()
    if choice != "auto":
        try:
            return MODELS[choice]
        except KeyError as exc:
            supported = ", ".join(("auto", *MODELS))
            raise ValueError(
                f"unknown Whisper model {requested!r}; choose one of: {supported}"
            ) from exc

    memory = physical_memory_bytes() if memory_bytes is None else int(memory_bytes)
    if memory < 0:
        raise ValueError("physical memory must not be negative")
    if memory >= 16 * GIB:
        return MODELS["large-v3-turbo"]
    if memory >= 8 * GIB:
        return MODELS["small"]
    return MODELS["base"]


ModelDownloader = Callable[[str, Path], None]


def _default_downloader(repository: str, destination: Path) -> None:
    """Download only files required by mlx-whisper, imported on first use."""

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - packaging failure path
        raise RuntimeError(
            "mlx-whisper installation is incomplete: huggingface-hub is missing"
        ) from exc

    snapshot_download(
        repo_id=repository,
        local_dir=str(destination),
        allow_patterns=["config.json", "*.json", *WEIGHT_FILENAMES],
    )


class WhisperModelCache:
    """Resolve model names to resumable, per-model cache directories.

    Construction performs no I/O.  ``ensure`` creates and downloads a model
    only when the first transcription actually requires it.  A process-local
    lock prevents concurrent URL jobs from downloading the same weights twice;
    ``snapshot_download`` itself resumes partial files across process restarts.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        downloader: ModelDownloader | None = None,
    ) -> None:
        self.root = Path(root or default_cache_root()).expanduser()
        self._download = downloader or _default_downloader
        self._lock = threading.Lock()

    def directory_for(self, model: WhisperModel | str) -> Path:
        spec = MODELS[model] if isinstance(model, str) else model
        return self.root / spec.directory_name

    def is_cached(self, model: WhisperModel | str) -> bool:
        directory = self.directory_for(model)
        return (directory / "config.json").is_file() and any(
            (directory / filename).is_file() for filename in WEIGHT_FILENAMES
        )

    def ensure(self, model: WhisperModel | str) -> Path:
        spec = MODELS[model] if isinstance(model, str) else model
        destination = self.directory_for(spec)
        if self.is_cached(spec):
            return destination

        with self._lock:
            if self.is_cached(spec):
                return destination
            destination.mkdir(parents=True, exist_ok=True)
            self._download(spec.repository, destination)
            if not self.is_cached(spec):
                raise RuntimeError(
                    f"Whisper download for {spec.name!r} is incomplete in {destination}"
                )
        return destination


def default_cache_root() -> Path:
    """The macOS cache location used for downloaded Whisper weights."""

    override = os.environ.get("YOUTUBETEXT_WHISPER_CACHE", "").strip()
    if override:
        return Path(override)
    return Path.home() / "Library" / "Caches" / "YouTubeText" / "whisper"
