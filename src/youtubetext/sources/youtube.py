"""YouTube source adapter."""

from __future__ import annotations

from typing import Any

from ._adapter import PlatformAdapter, _hostname, _is_host


class YouTubeAdapter(PlatformAdapter):
    name = "youtube"
    language_priority = (
        "en",
        "zh-Hans",
        "zh-Hant",
        "zh",
        "es",
        "ja",
        "ko",
        "fr",
        "de",
        "pt",
        "it",
    )

    @classmethod
    def matches(cls, url: str) -> bool:
        host = _hostname(url)
        return _is_host(host, "youtube.com") or _is_host(host, "youtu.be")

    def yt_dlp_options(self) -> dict[str, Any]:
        return {
            "socket_timeout": 30,
            "retries": 3,
            "extractor_retries": 3,
        }
