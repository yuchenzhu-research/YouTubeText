"""Shared yt-dlp integration details kept behind the source/media boundary."""

from __future__ import annotations


class QuietYtDlpLogger:
    """Let YouTubeText present retries and final failures through its own CLI."""

    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


QUIET_YT_DLP_LOGGER = QuietYtDlpLogger()
