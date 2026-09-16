"""Offline environment diagnostics for the ``youtubetext doctor`` command.

The doctor only observes local state.  In particular, inspecting Whisper model
caches never calls ``ensure`` and therefore never downloads model weights.
"""
from __future__ import annotations

import importlib
import json
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from .asr.models import MODELS, WhisperModel, WhisperModelCache
from .ocr.vision import find_vision_ocr_binary
from .runtime import HostKind, HostProfile


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
        vision_probe: Callable[[Path], tuple[bool, str]] | None = None,
        import_module: Callable[[str], ModuleType | Any] | None = None,
        model_cache: _ModelCacheProbe | None = None,
    ) -> None:
        self._system = system or platform.system
        self._machine = machine or platform.machine
        self._which = which or shutil.which
        self._vision_binary = vision_binary or find_vision_ocr_binary
        self._vision_probe = vision_probe or _probe_vision_binary
        self._import_module = import_module or importlib.import_module
        self._model_cache = model_cache

    def run(self) -> DoctorReport:
        system = self._system()
        machine = self._machine()
        host = HostProfile(system, machine, memory_bytes=0, cpu_count=1)

        if system.casefold() == "darwin":
            checks = self._mac_host_checks(host)
        elif system.casefold() == "windows":
            checks = self._windows_host_checks(host)
        else:
            checks = [
                DiagnosticCheck(
                    key="supported_host",
                    ok=False,
                    required=True,
                    detail=(
                        "requires Apple Silicon macOS or 64-bit Windows; "
                        f"detected {system or 'unknown'} {machine or 'unknown'}"
                    ),
                )
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

        if system.casefold() == "darwin":
            checks.extend(self._mac_backend_checks())
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
        elif system.casefold() == "windows":
            checks.extend(self._windows_backend_checks())
            # faster-whisper uses CTranslate2 weights, not the MLX catalogue.
            # Keep the list empty until its separate cache format is inspected.
            models = ()
        else:
            models = ()
        return DoctorReport(checks=tuple(checks), whisper_models=models)

    @staticmethod
    def _mac_host_checks(host: HostProfile) -> list[DiagnosticCheck]:
        return [
            DiagnosticCheck(
                key="macos",
                ok=host.system.casefold() == "darwin",
                required=True,
                detail=f"macOS detected ({host.system})",
            ),
            DiagnosticCheck(
                key="apple_silicon",
                ok=host.kind is HostKind.APPLE_SILICON,
                required=True,
                detail=(
                    "Apple Silicon detected (arm64)"
                    if host.kind is HostKind.APPLE_SILICON
                    else f"requires Apple Silicon; detected {host.machine or 'unknown'}"
                ),
            ),
        ]

    @staticmethod
    def _windows_host_checks(host: HostProfile) -> list[DiagnosticCheck]:
        supported = host.kind is HostKind.WINDOWS_X64
        return [
            DiagnosticCheck(
                key="windows_x64",
                ok=supported,
                required=True,
                detail=(
                    f"64-bit Windows detected ({host.machine})"
                    if supported
                    else f"requires 64-bit x86 Windows; detected {host.machine or 'unknown'}"
                ),
            )
        ]

    def _mac_backend_checks(self) -> list[DiagnosticCheck]:
        vision = _path_from_probe(self._vision_binary())
        if vision is None:
            vision_ok = False
            vision_detail = "Swift Apple Vision OCR helper is not built"
        else:
            try:
                vision_ok, vision_detail = self._vision_probe(vision)
            except Exception as exc:
                vision_ok = False
                vision_detail = f"Swift Apple Vision OCR helper failed: {_one_line_error(exc)}"
        return [
            DiagnosticCheck(
                key="vision_ocr",
                ok=vision_ok,
                required=True,
                detail=vision_detail,
                path=vision,
            ),
            self._check_mlx_whisper(),
        ]

    def _windows_backend_checks(self) -> list[DiagnosticCheck]:
        checks = [
            self._check_module_api(
                "rapidocr",
                "RapidOCR",
                key="rapidocr",
                label="RapidOCR",
            ),
            self._check_onnxruntime(),
            self._check_module_api(
                "faster_whisper",
                "WhisperModel",
                key="faster_whisper",
                label="faster-whisper",
            ),
        ]
        checks.extend(self._check_ctranslate2())
        return checks

    def _check_module_api(
        self,
        module_name: str,
        attribute: str,
        *,
        key: str,
        label: str,
    ) -> DiagnosticCheck:
        """Resolve a public engine class without constructing or downloading it."""

        try:
            module = self._import_module(module_name)
            api = getattr(module, attribute)
            if not callable(api):
                raise TypeError(f"{module_name}.{attribute} is not callable")
        except Exception as exc:
            return DiagnosticCheck(
                key=key,
                ok=False,
                required=True,
                detail=f"{label} cannot be loaded: {_one_line_error(exc)}",
            )
        return DiagnosticCheck(
            key=key,
            ok=True,
            required=True,
            detail=_available_detail(label, module),
        )

    def _check_onnxruntime(self) -> DiagnosticCheck:
        try:
            module = self._import_module("onnxruntime")
            provider_probe = getattr(module, "get_available_providers")
            if not callable(provider_probe):
                raise TypeError("onnxruntime.get_available_providers is not callable")
            providers = {str(provider) for provider in provider_probe()}
            if "CPUExecutionProvider" not in providers:
                return DiagnosticCheck(
                    key="onnxruntime_cpu",
                    ok=False,
                    required=True,
                    detail="ONNX Runtime has no CPUExecutionProvider",
                )
        except Exception as exc:
            return DiagnosticCheck(
                key="onnxruntime_cpu",
                ok=False,
                required=True,
                detail=f"ONNX Runtime cannot be loaded: {_one_line_error(exc)}",
            )
        return DiagnosticCheck(
            key="onnxruntime_cpu",
            ok=True,
            required=True,
            detail=_available_detail("ONNX Runtime CPU", module),
        )

    def _check_ctranslate2(self) -> list[DiagnosticCheck]:
        try:
            module = self._import_module("ctranslate2")
            compute_probe = getattr(module, "get_supported_compute_types")
            if not callable(compute_probe):
                raise TypeError(
                    "ctranslate2.get_supported_compute_types is not callable"
                )
            cpu_types = {
                str(compute_type).casefold()
                for compute_type in compute_probe("cpu")
            }
            cpu_ok = "int8" in cpu_types
            cpu_detail = (
                _available_detail("CTranslate2 CPU int8", module)
                if cpu_ok
                else "CTranslate2 does not support CPU int8 on this host"
            )
        except Exception as exc:
            detail = _one_line_error(exc)
            return [
                DiagnosticCheck(
                    key="ctranslate2_cpu",
                    ok=False,
                    required=True,
                    detail=f"CTranslate2 CPU cannot be loaded: {detail}",
                ),
                DiagnosticCheck(
                    key="cuda",
                    ok=False,
                    required=False,
                    detail="CUDA availability could not be checked",
                ),
            ]

        cpu_check = DiagnosticCheck(
            key="ctranslate2_cpu",
            ok=cpu_ok,
            required=True,
            detail=cpu_detail,
        )
        try:
            device_probe = getattr(module, "get_cuda_device_count")
            if not callable(device_probe):
                raise TypeError("ctranslate2.get_cuda_device_count is not callable")
            device_count = int(device_probe())
            cuda_types = (
                {
                    str(compute_type).casefold()
                    for compute_type in compute_probe("cuda")
                }
                if device_count > 0
                else set()
            )
            cuda_ok = device_count > 0 and "float16" in cuda_types
            cuda_detail = (
                f"CUDA float16 is available on {device_count} device(s)"
                if cuda_ok
                else "CUDA float16 is unavailable; CPU int8 will be used"
            )
        except Exception as exc:
            cuda_ok = False
            cuda_detail = f"CUDA probe failed; CPU int8 will be used: {_one_line_error(exc)}"
        return [
            cpu_check,
            DiagnosticCheck(
                key="cuda",
                ok=cuda_ok,
                required=False,
                detail=cuda_detail,
            ),
        ]

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
        model_cache = self._model_cache or WhisperModelCache()
        statuses: list[CachedModelStatus] = []
        for model in MODELS.values():
            try:
                cached_directory = model_cache.cached_directory(model)
            except OSError:
                cached_directory = None
            directory = cached_directory or model_cache.directory_for(model)
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


def _available_detail(label: str, module: ModuleType | Any) -> str:
    try:
        version = str(getattr(module, "__version__", "") or "").strip()
    except Exception:
        version = ""
    return f"{label} is available" + (f" ({version})" if version else "")


def _probe_vision_binary(binary: Path) -> tuple[bool, str]:
    request = json.dumps(
        {
            "images": ["/nonexistent/youtubetext-doctor-probe.jpg"],
            "languages": ["en-US"],
            "accurate": False,
            "minimumTextHeight": 0.01,
        }
    )
    try:
        completed = subprocess.run(
            [str(binary)],
            input=request,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Swift Apple Vision OCR helper could not run: {_one_line_error(exc)}"
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or completed.stdout).split())
        return False, f"Swift Apple Vision OCR helper exited with an error: {detail[:200]}"
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False, "Swift Apple Vision OCR helper returned invalid JSON"
    if not isinstance(response, dict):
        return False, "Swift Apple Vision OCR helper returned an invalid response"
    frames = response.get("frames")
    if (
        response.get("engine") != "apple-vision"
        or not isinstance(frames, list)
        or len(frames) != 1
    ):
        return False, "Swift Apple Vision OCR helper returned an invalid response"
    return True, "Swift Apple Vision OCR helper passed its protocol check"
