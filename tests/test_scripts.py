from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_repository(tmp_path: Path) -> Path:
    root = tmp_path / "YouTubeText fixture with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "native" / "macos").mkdir(parents=True)
    for name in ("install.sh", "dev.sh", "build_vision_ocr.sh"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, root / "scripts" / name)
    (root / "native" / "macos" / "VisionOCR.swift").write_text(
        "// The fake compiler does not read this fixture.\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    return root


def fake_toolchain(tmp_path: Path) -> tuple[Path, Path]:
    tools = tmp_path / "fake tools"
    log = tmp_path / "uv.log"
    write_executable(
        tools / "uname",
        """#!/bin/sh
case "$1" in
  -s) printf '%s\n' "${FAKE_SYSTEM:-Darwin}" ;;
  -m) printf '%s\n' "${FAKE_MACHINE:-arm64}" ;;
  *) exit 2 ;;
esac
""",
    )
    write_executable(
        tools / "python3",
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf '%s\n' "Python ${FAKE_PYTHON_VERSION:-3.12.8}"
fi
exit "${FAKE_PYTHON_EXIT:-0}"
""",
    )
    write_executable(tools / "ffmpeg", "#!/bin/sh\nexit 0\n")
    write_executable(
        tools / "swiftc",
        """#!/bin/sh
output=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    shift
    output="$1"
  fi
  shift
done
[ -n "$output" ] || exit 2
mkdir -p "$(dirname "$output")"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$output"
chmod +x "$output"
""",
    )
    write_executable(
        tools / "uv",
        """#!/bin/sh
printf '%s\n' "$*" >> "$UV_LOG"
if [ "$1" = "sync" ]; then
  mkdir -p .venv/bin
  printf '%s\n' '#!/bin/sh' 'exit 0' > .venv/bin/youtubetext
  chmod +x .venv/bin/youtubetext
fi
""",
    )
    return tools, log


def add_venv_capable_python(tools: Path) -> None:
    write_executable(
        tools / "python3",
        """#!/bin/sh
if [ "$1" = "-c" ]; then
  exit 0
fi
if [ "$1" = "--version" ]; then
  printf '%s\n' 'Python 3.12.8'
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  printf '%s\n' "$*" >> "$PIP_LOG"
  sibling="$(dirname "$0")/youtubetext"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$sibling"
  chmod +x "$sibling"
  exit 0
fi
exit 2
""",
    )


def script_environment(tools: Path, log: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = f"{tools}:/usr/bin:/bin"
    environment["UV_LOG"] = str(log)
    return environment


def run_script(
    script: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script)],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def test_install_rejects_non_macos_before_installing(tmp_path: Path) -> None:
    root = fake_repository(tmp_path)
    tools, log = fake_toolchain(tmp_path)
    environment = script_environment(tools, log)
    environment["FAKE_SYSTEM"] = "Linux"

    result = run_script(root / "scripts" / "install.sh", cwd=tmp_path, environment=environment)

    assert result.returncode != 0
    assert "requires macOS on Apple Silicon" in result.stderr
    assert not log.exists()


def test_install_rejects_python_older_than_311(tmp_path: Path) -> None:
    root = fake_repository(tmp_path)
    tools, log = fake_toolchain(tmp_path)
    environment = script_environment(tools, log)
    environment["FAKE_PYTHON_EXIT"] = "1"
    environment["FAKE_PYTHON_VERSION"] = "3.10.14"

    result = run_script(root / "scripts" / "install.sh", cwd=tmp_path, environment=environment)

    assert result.returncode != 0
    assert "Python 3.11 through 3.14 is required" in result.stderr
    assert not log.exists()


def test_install_reports_missing_ffmpeg_without_running_brew(tmp_path: Path) -> None:
    root = fake_repository(tmp_path)
    tools, log = fake_toolchain(tmp_path)
    (tools / "ffmpeg").unlink()
    environment = script_environment(tools, log)

    result = run_script(root / "scripts" / "install.sh", cwd=tmp_path, environment=environment)

    assert result.returncode != 0
    assert "brew install ffmpeg" in result.stderr
    assert not log.exists()


def test_install_uses_uv_builds_helper_and_prints_run_command(tmp_path: Path) -> None:
    root = fake_repository(tmp_path)
    tools, log = fake_toolchain(tmp_path)
    environment = script_environment(tools, log)

    result = run_script(root / "scripts" / "install.sh", cwd=tmp_path, environment=environment)

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "sync --no-dev --python python3"
    ]
    assert (root / "bin" / "youtubetext-vision-ocr").stat().st_mode & stat.S_IXUSR
    assert "uv run --no-dev youtubetext --help" in result.stdout
    assert str(root).replace(" ", "\\ ") in result.stdout


def test_install_falls_back_to_a_local_venv_when_uv_is_missing(tmp_path: Path) -> None:
    root = fake_repository(tmp_path)
    tools, log = fake_toolchain(tmp_path)
    (tools / "uv").unlink()
    add_venv_capable_python(tools)
    environment = script_environment(tools, log)
    pip_log = tmp_path / "pip.log"
    environment["PIP_LOG"] = str(pip_log)

    result = run_script(root / "scripts" / "install.sh", cwd=tmp_path, environment=environment)

    assert result.returncode == 0, result.stderr
    assert "using Python's built-in venv and pip" in result.stdout
    assert pip_log.read_text(encoding="utf-8").strip() == (
        f"-m pip install --editable {root}"
    )
    assert (root / ".venv" / "bin" / "youtubetext").is_file()
    assert (root / "bin" / "youtubetext-vision-ocr").is_file()


def test_dev_syncs_dependencies_builds_helper_and_runs_tests(tmp_path: Path) -> None:
    root = fake_repository(tmp_path)
    tools, log = fake_toolchain(tmp_path)
    environment = script_environment(tools, log)

    result = run_script(root / "scripts" / "dev.sh", cwd=tmp_path, environment=environment)

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "sync --python python3",
        "run --no-sync pytest",
    ]
    assert (root / "bin" / "youtubetext-vision-ocr").is_file()
    assert "Running tests" in result.stdout


@pytest.mark.parametrize("name", ["install.sh", "dev.sh"])
def test_scripts_do_not_request_sudo(name: str) -> None:
    source = (PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert "sudo" not in source
