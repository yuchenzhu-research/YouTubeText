"""Build, inspect and install release artifacts without publishing them."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from pathlib import Path

HELPER = "youtubetext/_bin/youtubetext-vision-ocr"


def inspect_artifacts(wheel: Path, sdist: Path, *, native: bool) -> None:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name, = (name for name in archive.namelist() if name.endswith(".dist-info/WHEEL"))
        metadata = archive.read(metadata_name).decode("utf-8")
        if native:
            if "Tag: py3-none-macosx_13_0_arm64" not in metadata:
                raise RuntimeError("The native wheel has an incorrect platform tag")
            if "Root-Is-Purelib: false" not in metadata:
                raise RuntimeError("The native wheel must not be marked pure Python")
            helper = archive.getinfo(HELPER)
            if not helper.external_attr >> 16 & 0o111:
                raise RuntimeError("The packaged native helper is not executable")
            if archive.read(helper)[:4] != b"\xcf\xfa\xed\xfe":
                raise RuntimeError("The packaged helper is not a 64-bit Mach-O binary")
        elif HELPER in archive.namelist() or "Tag: py3-none-any" not in metadata:
            raise RuntimeError("The portable wheel contains native files or an incorrect tag")
    with tarfile.open(sdist, "r:gz") as archive:
        names = {name.partition("/")[2] for name in archive.getnames()}
        required = {
            "pyproject.toml", "hatch_build.py", "native/macos/VisionOCR.swift",
            "scripts/build_vision_ocr.sh", "scripts/build_release.py",
        }
        if not required.issubset(names):
            raise RuntimeError(f"Source archive is missing build resources: {sorted(required - names)}")
        if any(name.startswith(("bin/", "src/youtubetext/_bin/")) for name in names):
            raise RuntimeError("Source archive contains generated native binaries")


def smoke_install(wheel: Path, uv: str, *, native: bool) -> None:
    # Running from an unrelated directory and clearing PYTHONPATH prevents a
    # working checkout or editable installation from hiding wheel omissions.
    with tempfile.TemporaryDirectory(prefix="youtubetext-install-") as directory:
        root = Path(directory)
        environment = root / "environment"
        venv.EnvBuilder(with_pip=False).create(environment)
        executable_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = executable_dir / ("python.exe" if os.name == "nt" else "python")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("YOUTUBETEXT_VISION_OCR", None)
        env.update(PYTHONNOUSERSITE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        subprocess.run(
            [uv, "pip", "install", "--python", str(python), str(wheel.resolve())],
            cwd=root, env=env, check=True,
        )
        command = executable_dir / ("youtubetext.exe" if os.name == "nt" else "youtubetext")
        subprocess.run([str(command), "--help"], cwd=root, env=env, check=True)
        doctor = subprocess.run(
            [str(command), "doctor", "--json"], cwd=root, env=env,
            capture_output=True, text=True, check=False,
        )
        if doctor.returncode not in {0, 1}:
            raise RuntimeError(f"Installed doctor crashed: {doctor.stderr}")
        report = json.loads(doctor.stdout)
        if not isinstance(report.get("checks"), list):
            raise RuntimeError("Installed doctor returned an invalid report")
        if native:
            vision = next(check for check in report["checks"] if check["key"] == "vision_ocr")
            if not vision["ok"] or not Path(vision["path"]).resolve().is_relative_to(environment.resolve()):
                raise RuntimeError(f"Installed wheel did not use its own working Vision helper: {vision}")
        subprocess.run(
            [str(python), "-c", (
                "import pathlib, sys, youtubetext; "
                "assert pathlib.Path(youtubetext.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())"
            )],
            cwd=root, env=env, check=True,
        )
        print(json.dumps({"installed_doctor_ready": report["ready"], "wheel": wheel.name}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/release"))
    arguments = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Install uv before building release artifacts")
    # The default uv build also rebuilds the wheel from the source archive.
    subprocess.run([uv, "build", "--out-dir", str(output)], cwd=project, check=True)
    native = platform.system() == "Darwin"
    suffix = "py3-none-macosx_13_0_arm64.whl" if native else "py3-none-any.whl"
    wheel = output / f"youtubetext-{version}-{suffix}"
    sdist = output / f"youtubetext-{version}.tar.gz"
    inspect_artifacts(wheel, sdist, native=native)
    smoke_install(wheel, uv, native=native)
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (wheel, sdist)
    )
    (output / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    print(checksums, end="")


if __name__ == "__main__":
    main()
