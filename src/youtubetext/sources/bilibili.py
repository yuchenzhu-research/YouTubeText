"""Bilibili source adapter."""

from __future__ import annotations

from typing import Any

from urllib.parse import urlsplit, urlunsplit

from ._adapter import PlatformAdapter, _hostname, _is_host


class BilibiliAdapter(PlatformAdapter):
    name = "bilibili"
    retry_delays = (0.5, 1.5, 3.0)
    language_priority = (
        "zh-Hans",
        "zh-Hant",
        "zh",
        "en",
        "ja",
        "ko",
    )

    @classmethod
    def matches(cls, url: str) -> bool:
        host = _hostname(url)
        return _is_host(host, "bilibili.com") or _is_host(host, "b23.tv")

    def request_url(self, url: str) -> str:
        """Avoid Bilibili's redirect path, which can return HTTP 412."""

        parsed = urlsplit(url)
        path = parsed.path.rstrip("/")
        parts = path.split("/")
        if len(parts) == 3 and parts[1].lower() == "video" and parts[2]:
            return urlunsplit(parsed._replace(path=f"{path}/"))
        return url

    def is_transient_error(self, error: Exception) -> bool:
        return "HTTP Error 412" in str(error)

    def yt_dlp_options(self) -> dict[str, Any]:
        return {
            "socket_timeout": 60,
            "retries": 8,
            "fragment_retries": 8,
        }
