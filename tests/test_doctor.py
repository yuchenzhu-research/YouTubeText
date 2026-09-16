from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from youtubetext.asr.models import MODELS, WhisperModel
from youtubetext.doctor import Doctor


def working_vision(_path: Path) -> tuple[bool, str]:
    return True, "Vision protocol is ready"


class FakeCache:
    def __init__(self, root: Path, cached: set[str] | None = None) -> None:
        self.root = root
        self.cached = cached or set()
        self.ensure_called = False

    def directory_for(self, model: WhisperModel | str) -> Path:
        name = model if isinstance(model, str) else model.directory_name
        return self.root / name

    def is_cached(self, model: WhisperModel | str) -> bool:
        name = model if isinstance(model, str) else model.name
        return name in self.cached

    def cached_directory(self, model: WhisperModel | str) -> Path | None:
        return self.directory_for(model) if self.is_cached(model) else None

    def ensure(self, _model: WhisperModel | str) -> Path:
        self.ensure_called = True
        raise AssertionError("doctor must never download a model")


class MustNotConstruct:
    def __init__(self, *_args, **_kwargs) -> None:
        raise AssertionError("doctor must not construct model engines")


class MustNotProbeCache:
    def __getattribute__(self, _name: str):
        raise AssertionError("Windows doctor must not inspect the MLX cache")


def windows_modules(*, cuda_devices: int = 0) -> dict[str, SimpleNamespace]:
    def compute_types(device: str) -> set[str]:
        return {"float16"} if device == "cuda" else {"int8", "float32"}

    return {
        "rapidocr": SimpleNamespace(
            __version__="3.9.2",
            RapidOCR=MustNotConstruct,
        ),
        "onnxruntime": SimpleNamespace(
            __version__="1.23.2",
            get_available_providers=lambda: ["CPUExecutionProvider"],
        ),
        "faster_whisper": SimpleNamespace(
            __version__="1.2.1",
            WhisperModel=MustNotConstruct,
        ),
        "ctranslate2": SimpleNamespace(
            __version__="4.8.2",
            get_supported_compute_types=compute_types,
            get_cuda_device_count=lambda: cuda_devices,
        ),
    }


def windows_doctor(
    _tmp_path: Path,
    *,
    machine: str = "AMD64",
    modules: dict[str, SimpleNamespace] | None = None,
    import_module=None,
) -> Doctor:
    available = modules or windows_modules()

    def mac_probe(*_args, **_kwargs):
        raise AssertionError("Windows doctor must not probe macOS backends")

    return Doctor(
        system=lambda: "Windows",
        machine=lambda: machine,
        which=lambda command: "C:/ffmpeg/bin/ffmpeg.exe" if command == "ffmpeg" else None,
        vision_binary=mac_probe,
        vision_probe=mac_probe,
        import_module=import_module or (lambda name: available[name]),
        model_cache=MustNotProbeCache(),
    )


def test_ready_report_contains_paths_versions_and_cached_models(tmp_path: Path) -> None:
    cache = FakeCache(tmp_path / "models", {"small"})
    doctor = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda command: "/opt/homebrew/bin/ffmpeg" if command == "ffmpeg" else None,
        vision_binary=lambda: Path("/usr/local/bin/youtubetext-vision-ocr"),
        vision_probe=working_vision,
        import_module=lambda name: SimpleNamespace(__version__="0.4.3"),
        model_cache=cache,
    )

    report = doctor.run()

    assert report.ready
    assert report.exit_code == 0
    assert report.check("macos").ok
    assert report.check("apple_silicon").ok
    assert report.check("ffmpeg").path == Path("/opt/homebrew/bin/ffmpeg")
    assert report.check("vision_ocr").path == Path(
        "/usr/local/bin/youtubetext-vision-ocr"
    )
    assert "0.4.3" in report.check("mlx_whisper").detail
    assert report.check("whisper_cache").ok
    assert [item.name for item in report.whisper_models] == list(MODELS)
    assert [item.name for item in report.whisper_models if item.cached] == ["small"]
    assert not cache.ensure_called


def test_unsupported_host_and_missing_dependencies_are_structured(tmp_path: Path) -> None:
    def unavailable(_name: str) -> None:
        raise ImportError("native extension missing\nsecond line")

    report = Doctor(
        system=lambda: "Linux",
        machine=lambda: "x86_64",
        which=lambda _command: None,
        vision_binary=lambda: None,
        import_module=unavailable,
        model_cache=FakeCache(tmp_path),
    ).run()

    assert not report.ready
    assert report.exit_code == 1
    assert not report.check("supported_host").ok
    assert not report.check("ffmpeg").ok
    assert {check.key for check in report.checks} == {"supported_host", "ffmpeg"}
    assert report.whisper_models == ()


def test_windows_x64_checks_open_source_backends_without_loading_models(
    tmp_path: Path,
) -> None:
    report = windows_doctor(tmp_path).run()

    assert report.ready
    assert report.exit_code == 0
    assert {check.key for check in report.checks} == {
        "windows_x64",
        "ffmpeg",
        "rapidocr",
        "onnxruntime_cpu",
        "faster_whisper",
        "ctranslate2_cpu",
        "cuda",
    }
    assert report.check("windows_x64").ok
    assert report.check("rapidocr").ok
    assert report.check("onnxruntime_cpu").ok
    assert report.check("faster_whisper").ok
    assert report.check("ctranslate2_cpu").ok
    assert not report.check("cuda").ok
    assert not report.check("cuda").required
    assert report.whisper_models == ()
    with pytest.raises(KeyError):
        report.check("vision_ocr")
    with pytest.raises(KeyError):
        report.check("mlx_whisper")


def test_windows_x86_64_alias_and_optional_cuda_are_supported(tmp_path: Path) -> None:
    report = windows_doctor(
        tmp_path,
        machine="x86_64",
        modules=windows_modules(cuda_devices=1),
    ).run()

    assert report.ready
    assert report.check("windows_x64").ok
    assert report.check("cuda").ok
    assert "1 device" in report.check("cuda").detail


def test_windows_arm64_is_not_claimed_as_supported(tmp_path: Path) -> None:
    report = windows_doctor(tmp_path, machine="ARM64").run()

    assert not report.ready
    assert not report.check("windows_x64").ok
    assert "ARM64" in report.check("windows_x64").detail


@pytest.mark.parametrize(
    ("missing_module", "failed_check"),
    (
        ("rapidocr", "rapidocr"),
        ("onnxruntime", "onnxruntime_cpu"),
        ("faster_whisper", "faster_whisper"),
        ("ctranslate2", "ctranslate2_cpu"),
    ),
)
def test_windows_missing_runtime_is_a_structured_failure(
    tmp_path: Path,
    missing_module: str,
    failed_check: str,
) -> None:
    modules = windows_modules()

    def import_module(name: str):
        if name == missing_module:
            raise ImportError("native DLL missing\nsecond line")
        return modules[name]

    report = windows_doctor(
        tmp_path,
        modules=modules,
        import_module=import_module,
    ).run()

    assert not report.ready
    assert not report.check(failed_check).ok
    assert "ImportError: native DLL missing second line" in report.check(
        failed_check
    ).detail


def test_windows_rejects_missing_cpu_execution_support(tmp_path: Path) -> None:
    modules = windows_modules()
    modules["onnxruntime"].get_available_providers = lambda: [
        "CUDAExecutionProvider"
    ]
    modules["ctranslate2"].get_supported_compute_types = lambda _device: {
        "float32"
    }

    report = windows_doctor(tmp_path, modules=modules).run()

    assert not report.ready
    assert report.check("onnxruntime_cpu").detail == (
        "ONNX Runtime has no CPUExecutionProvider"
    )
    assert report.check("ctranslate2_cpu").detail == (
        "CTranslate2 does not support CPU int8 on this host"
    )


def test_empty_whisper_cache_is_nonfatal_and_never_downloads(tmp_path: Path) -> None:
    cache = FakeCache(tmp_path)
    report = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda _command: "/usr/bin/ffmpeg",
        vision_binary=lambda: Path("/usr/bin/vision-helper"),
        vision_probe=working_vision,
        import_module=lambda _name: SimpleNamespace(),
        model_cache=cache,
    ).run()

    assert report.ready
    assert not report.check("whisper_cache").ok
    assert "downloads on first use" in report.check("whisper_cache").detail
    assert not cache.ensure_called


def test_report_serializes_to_json_ready_primitives(tmp_path: Path) -> None:
    report = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda _command: "/usr/bin/ffmpeg",
        vision_binary=lambda: Path("/usr/bin/vision-helper"),
        vision_probe=working_vision,
        import_module=lambda _name: SimpleNamespace(),
        model_cache=FakeCache(tmp_path, {"base", "large-v3-turbo"}),
    ).run()

    payload = report.as_dict()
    assert payload["ready"] is True
    assert payload["exit_code"] == 0
    assert {item["key"] for item in payload["checks"]} == {
        "macos",
        "apple_silicon",
        "ffmpeg",
        "vision_ocr",
        "mlx_whisper",
        "whisper_cache",
    }
    assert payload["whisper_models"][0] == {
        "name": "base",
        "cached": True,
        "directory": str(tmp_path / "base"),
        "download_bytes": MODELS["base"].download_bytes,
        "runtime_memory_bytes": MODELS["base"].runtime_memory_bytes,
    }


def test_unknown_check_key_raises_key_error(tmp_path: Path) -> None:
    report = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda _command: "/usr/bin/ffmpeg",
        vision_binary=lambda: Path("/usr/bin/vision-helper"),
        vision_probe=working_vision,
        import_module=lambda _name: SimpleNamespace(),
        model_cache=FakeCache(tmp_path),
    ).run()

    try:
        report.check("missing")
    except KeyError as exc:
        assert exc.args == ("missing",)
    else:  # pragma: no cover - explicit failure without importing pytest
        raise AssertionError("expected KeyError")


def test_doctor_reports_actual_huggingface_snapshot_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    snapshot = (
        hub_root
        / "models--mlx-community--whisper-large-v3-turbo"
        / "snapshots/revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "weights.safetensors").write_bytes(b"weights")

    from youtubetext.asr.models import WhisperModelCache

    cache = WhisperModelCache(tmp_path / "managed", hub_cache_root=hub_root)
    report = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda _command: "/usr/bin/ffmpeg",
        vision_binary=lambda: Path("/usr/bin/vision-helper"),
        vision_probe=working_vision,
        import_module=lambda _name: SimpleNamespace(),
        model_cache=cache,
    ).run()

    turbo = next(item for item in report.whisper_models if item.name == "large-v3-turbo")
    assert turbo.cached
    assert turbo.directory == snapshot


def test_broken_vision_protocol_makes_doctor_not_ready(tmp_path: Path) -> None:
    report = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda _command: "/usr/bin/ffmpeg",
        vision_binary=lambda: Path("/usr/bin/broken-helper"),
        vision_probe=lambda _path: (False, "Vision returned invalid JSON"),
        import_module=lambda _name: SimpleNamespace(),
        model_cache=FakeCache(tmp_path),
    ).run()

    assert not report.ready
    assert not report.check("vision_ocr").ok
    assert "invalid JSON" in report.check("vision_ocr").detail
