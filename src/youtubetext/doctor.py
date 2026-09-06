"""Offline environment diagnostics for the future ``youtubetext doctor`` CLI.

The doctor only observes local state.  In particular, inspecting Whisper model
caches never calls ``ensure`` and therefore never downloads model weights.
"""
from __future__ import annotations

import importlib
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from .asr.models import MODELS, WhisperModel, WhisperModelCache
from .ocr.vision import find_vision_ocr_binary


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One locally verifiable prerequisite."""

    key: str
    ok: bool
    required: bool
    detail: str
    path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
            "path": str(self.path) if self.path is not None else None,
        }


@dataclass(frozen=True, slots=True)
class CachedModelStatus:
    """Read-only status for one supported Whisper model."""

    name: str
    cached: bool
    directory: Path
    download_bytes: int
    runtime_memory_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cached": self.cached,
            "directory": str(self.directory),
            "download_bytes": self.download_bytes,
            "runtime_memory_bytes": self.runtime_memory_bytes,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Complete diagnostic result suitable for terminal or JSON rendering."""

    checks: tuple[DiagnosticCheck, ...]
    whisper_models: tuple[CachedModelStatus, ...]

    @property
    def ready(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    @property
    def exit_code(self) -> int:
        return 0 if self.ready else 1

    def check(self, key: str) -> DiagnosticCheck:
        for item in self.checks:
            if item.key == key:
                return item
        raise KeyError(key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "exit_code": self.exit_code,
            "checks": [check.as_dict() for check in self.checks],
            "whisper_models": [model.as_dict() for model in self.whisper_models],
        }


class _ModelCacheProbe(Protocol):
    def directory_for(self, model: WhisperModel | str) -> Path: ...

    def is_cached(self, model: WhisperModel | str) -> bool: ...

    def cached_directory(self, model: WhisperModel | str) -> Path | None: ...


class Doctor:
    """Run local probes with injectable system boundaries for deterministic tests."""

    def __init__(
        self,
        *,
        system: Callable[[], str] | None = None,
        machine: Callable[[], str] | None = None,
        which: Callable[[str], str | None] | None = None,
        vision_binary: Callable[[], Path | None] | None = None,
        import_module: Callable[[str], ModuleType | Any] | None = None,
        model_cache: _ModelCacheProbe | None = None,
    ) -> None:
        self._system = system or platform.system
        self._machine = machine or platform.machine
        self._which = which or shutil.which
        self._vision_binary = vision_binary or find_vision_ocr_binary
        self._import_module = import_module or importlib.import_module
        self._model_cache = model_cache or WhisperModelCache()

    def run(self) -> DoctorReport:
        system = self._system()
        machine = self._machine()

        checks = [
            DiagnosticCheck(
                key="macos",
                ok=system == "Darwin",
                required=True,
                detail=(
                    f"macOS detected ({system})"
                    if system == "Darwin"
                    else f"requires macOS; detected {system or 'unknown'}"
                ),
            ),
            DiagnosticCheck(
                key="apple_silicon",
                ok=machine == "arm64",
                required=True,
                detail=(
                    "Apple Silicon detected (arm64)"
                    if machine == "arm64"
                    else f"requires Apple Silicon; detected {machine or 'unknown'}"
                ),
            ),
        ]

        ffmpeg = _path_from_probe(self._which("ffmpeg"))
        checks.append(
            DiagnosticCheck(
                key="ffmpeg",
                ok=ffmpeg is not None,
                required=True,
                detail="ffmpeg is available" if ffmpeg else "ffmpeg was not found on PATH",
                path=ffmpeg,
            )
        )

        vision = _path_from_probe(self._vision_binary())
        checks.append(
            DiagnosticCheck(
                key="vision_ocr",
                ok=vision is not None,
                required=True,
                detail=(
                    "Swift Apple Vision OCR helper is available"
                    if vision
                    else "Swift Apple Vision OCR helper is not built"
                ),
                path=vision,
            )
        )

        checks.append(self._check_mlx_whisper())
        models = self._cached_models()
        cached_names = [model.name for model in models if model.cached]
        checks.append(
            DiagnosticCheck(
                key="whisper_cache",
                ok=bool(cached_names),
                required=False,
                detail=(
                    f"cached Whisper models: {', '.join(cached_names)}"
                    if cached_names
                    else "no Whisper model is cached; the selected model downloads on first use"
                ),
            )
        )
        return DoctorReport(checks=tuple(checks), whisper_models=models)

    def _check_mlx_whisper(self) -> DiagnosticCheck:
        try:
            module = self._import_module("mlx_whisper")
        except Exception as exc:  # imports can fail with native-linker errors
            detail = _one_line_error(exc)
            return DiagnosticCheck(
                key="mlx_whisper",
                ok=False,
                required=True,
                detail=f"mlx-whisper cannot be imported: {detail}",
            )
        version = str(getattr(module, "__version__", "")).strip()
        detail = "mlx-whisper is importable"
        if version:
            detail += f" ({version})"
        return DiagnosticCheck(
            key="mlx_whisper", ok=True, required=True, detail=detail
        )

    def _cached_models(self) -> tuple[CachedModelStatus, ...]:
        statuses: list[CachedModelStatus] = []
        for model in MODELS.values():
            try:
                cached_directory = self._model_cache.cached_directory(model)
            except OSError:
                cached_directory = None
            directory = cached_directory or self._model_cache.directory_for(model)
            statuses.append(
                CachedModelStatus(
                    name=model.name,
                    cached=cached_directory is not None,
                    directory=directory,
                    download_bytes=model.download_bytes,
                    runtime_memory_bytes=model.runtime_memory_bytes,
                )
            )
        return tuple(statuses)


def diagnose() -> DoctorReport:
    """Run the production offline diagnostic suite."""

    return Doctor().run()


def _path_from_probe(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text).expanduser() if text else None


def _one_line_error(exc: Exception) -> str:
    message = " ".join(str(exc).split()) or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"[:300]
