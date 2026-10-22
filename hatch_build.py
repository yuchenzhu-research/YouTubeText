"""Bundle the native helper only in standard Apple Silicon wheel builds."""
from __future__ import annotations

import os
import platform
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class VisionBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        # Editable installs keep the existing scripts/build_vision_ocr.sh flow.
        if version == "editable" or platform.system() != "Darwin":
            return
        if platform.machine().lower() != "arm64":
            raise RuntimeError("macOS release wheels require an Apple Silicon build host")
        temporary = tempfile.TemporaryDirectory(prefix="youtubetext-wheel-")
        self._native_build = temporary
        binary = Path(temporary.name) / "youtubetext-vision-ocr"
        try:
            subprocess.run(
                ["bash", str(Path(self.root) / "scripts" / "build_vision_ocr.sh")],
                env={**os.environ, "YOUTUBETEXT_VISION_OCR_OUTPUT": str(binary)},
                check=True,
                timeout=180,
            )
            build_data["pure_python"] = False
            build_data["tag"] = "py3-none-macosx_13_0_arm64"
            build_data.setdefault("force_include", {})[str(binary)] = (
                "youtubetext/_bin/youtubetext-vision-ocr"
            )
        except BaseException:
            temporary.cleanup()
            raise

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        temporary = getattr(self, "_native_build", None)
        if temporary is not None:
            temporary.cleanup()
