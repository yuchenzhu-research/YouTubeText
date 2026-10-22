"""Verify distributable source resources, wheel tags and bundled helper lookup."""
from __future__ import annotations

import platform
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from youtubetext.ocr import vision

PROJECT = Path(__file__).resolve().parents[1]
HELPER = "youtubetext/_bin/youtubetext-vision-ocr"


def test_installed_package_helper_is_discovered_before_checkout_or_path(tmp_path, monkeypatch):
    package = tmp_path / "site-packages" / "youtubetext"
    helper = package / "_bin" / vision.VISION_OCR_EXECUTABLE
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"packaged executable fixture")
    helper.chmod(0o755)
    monkeypatch.delenv(vision.VISION_OCR_ENV, raising=False)
    monkeypatch.setattr(vision, "__file__", str(package / "ocr" / "vision.py"))
    monkeypatch.setattr(vision, "_project_root", lambda: tmp_path / "absent-checkout")
    monkeypatch.setattr(vision.shutil, "which", lambda _name: None)
    monkeypatch.setattr(vision.sys, "frozen", False, raising=False)

    assert vision.find_vision_ocr_binary() == helper.resolve()


def test_explicit_helper_override_keeps_precedence_over_packaged_helper(tmp_path, monkeypatch):
    override = tmp_path / "override"
    override.write_bytes(b"explicit executable fixture")
    override.chmod(0o755)
    monkeypatch.setenv(vision.VISION_OCR_ENV, str(override))

    assert next(iter(vision._binary_candidates())) == override


@pytest.fixture(scope="module")
def release_artifacts(tmp_path_factory):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the distribution build test")
    if platform.system() == "Darwin" and platform.machine().lower() != "arm64":
        pytest.skip("macOS native release builds require Apple Silicon")
    output = tmp_path_factory.mktemp("distribution-build")
    result = subprocess.run(
        [uv, "build", "--out-dir", str(output)], cwd=PROJECT,
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheel, = output.glob("*.whl")
    source, = output.glob("*.tar.gz")
    return wheel, source


def test_wheel_has_correct_native_tag_and_executable_payload(release_artifacts):
    wheel, _source = release_artifacts
    version = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"youtubetext-{version}.dist-info/WHEEL").decode("utf-8")
        if platform.system() == "Darwin":
            assert wheel.name.endswith("py3-none-macosx_13_0_arm64.whl")
            assert "Root-Is-Purelib: false" in metadata
            assert "Tag: py3-none-macosx_13_0_arm64" in metadata
            assert archive.getinfo(HELPER).external_attr >> 16 & 0o111
            assert archive.read(HELPER)[:4] == b"\xcf\xfa\xed\xfe"
        else:
            assert wheel.name.endswith("py3-none-any.whl")
            assert "Root-Is-Purelib: true" in metadata
            assert "Tag: py3-none-any" in metadata
            assert HELPER not in archive.namelist()


def test_source_archive_can_rebuild_helper_without_generated_files(release_artifacts):
    _wheel, source = release_artifacts
    with tarfile.open(source, "r:gz") as archive:
        names = {name.partition("/")[2] for name in archive.getnames()}
    assert {
        "native/macos/VisionOCR.swift", "hatch_build.py", "pyproject.toml",
        "scripts/build_vision_ocr.sh", "scripts/build_release.py",
        "CHANGELOG.md", "tests/fixtures/ted_caption_timing.json",
    }.issubset(names)
    assert not any(name.startswith(("bin/", "src/youtubetext/_bin/")) for name in names)
    assert not any(name.endswith(('.whl', '.mp4', '.wav')) for name in names)


def test_distributions_include_selected_license_and_third_party_notices(release_artifacts):
    wheel, source = release_artifacts
    with zipfile.ZipFile(wheel) as archive:
        metadata_name, = (name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
        assert "License-Expression: MIT" in metadata
        licenses = {Path(name).name for name in archive.namelist() if ".dist-info/licenses/" in name}
        assert {"LICENSE", "THIRD_PARTY_NOTICES.md"}.issubset(licenses)
    with tarfile.open(source, "r:gz") as archive:
        names = {name.partition("/")[2] for name in archive.getnames()}
        assert {"LICENSE", "THIRD_PARTY_NOTICES.md"}.issubset(names)
