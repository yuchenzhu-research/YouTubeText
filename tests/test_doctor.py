from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from youtubetext.asr.models import MODELS, WhisperModel
from youtubetext.doctor import Doctor


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


def test_ready_report_contains_paths_versions_and_cached_models(tmp_path: Path) -> None:
    cache = FakeCache(tmp_path / "models", {"small"})
    doctor = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda command: "/opt/homebrew/bin/ffmpeg" if command == "ffmpeg" else None,
        vision_binary=lambda: Path("/usr/local/bin/youtubetext-vision-ocr"),
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
    assert not report.check("macos").ok
    assert not report.check("apple_silicon").ok
    assert not report.check("ffmpeg").ok
    assert not report.check("vision_ocr").ok
    assert not report.check("mlx_whisper").ok
    assert "ImportError: native extension missing second line" in report.check(
        "mlx_whisper"
    ).detail
    assert not report.check("whisper_cache").ok
    assert not report.check("whisper_cache").required


def test_empty_whisper_cache_is_nonfatal_and_never_downloads(tmp_path: Path) -> None:
    cache = FakeCache(tmp_path)
    report = Doctor(
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        which=lambda _command: "/usr/bin/ffmpeg",
        vision_binary=lambda: Path("/usr/bin/vision-helper"),
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
        import_module=lambda _name: SimpleNamespace(),
        model_cache=cache,
    ).run()

    turbo = next(item for item in report.whisper_models if item.name == "large-v3-turbo")
    assert turbo.cached
    assert turbo.directory == snapshot
