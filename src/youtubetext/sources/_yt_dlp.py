"""Shared yt-dlp integration details kept behind the source/media boundary."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path


COOKIE_BROWSERS = (
    "safari",
    "chrome",
    "firefox",
    "brave",
    "edge",
    "chromium",
    "opera",
    "vivaldi",
    "whale",
)


@dataclass(frozen=True, slots=True)
class YtDlpAuth:
    """Authentication source passed through to yt-dlp without copying secrets."""

    browser: str = ""
    cookie_file: Path | None = None

    def __post_init__(self) -> None:
        browser = self.browser.strip().lower()
        cookie_file = (
            Path(self.cookie_file).expanduser().resolve()
            if self.cookie_file is not None
            else None
        )
        if browser and cookie_file is not None:
            raise ValueError("choose browser cookies or a cookie file, not both")
        if browser and browser not in COOKIE_BROWSERS:
            raise ValueError(f"unsupported cookie browser: {browser}")
        if cookie_file is not None and not cookie_file.is_file():
            raise ValueError(f"cookie file does not exist or is not a file: {cookie_file}")
        object.__setattr__(self, "browser", browser)
        object.__setattr__(self, "cookie_file", cookie_file)

    def yt_dlp_options(self) -> dict[str, object]:
        if self.browser:
            return {"cookiesfrombrowser": (self.browser, None, None, None)}
        if self.cookie_file is not None:
            # yt-dlp writes its cookie jar when a YoutubeDL instance closes. Give
            # each invocation an isolated in-memory copy so parallel URL jobs can
            # never modify or race on the user's original file.
            return {
                "cookiefile": io.StringIO(self.cookie_file.read_text(encoding="utf-8"))
            }
        return {}


class QuietYtDlpLogger:
    """Let YouTubeText present retries and final failures through its own CLI."""

    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


QUIET_YT_DLP_LOGGER = QuietYtDlpLogger()
