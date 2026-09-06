"""Bilibili source adapter."""

from __future__ import annotations

from typing import Any

from ._adapter import PlatformAdapter, _hostname, _is_host


class BilibiliAdapter(PlatformAdapter):
    name = "bilibili"
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

    def yt_dlp_options(self) -> dict[str, Any]:
        return {
            "socket_timeout": 60,
            "retries": 8,
            "fragment_retries": 8,
            "http_headers": {
                "Referer": "https://www.bilibili.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
            },
        }
