"""Platform seam used internally by source acquisition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlsplit

from .models import UnsupportedSourceError


def _hostname(url: str) -> str:
    """Return a normalized hostname without accepting host-like path text."""

    value = str(url or "").strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        return (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def _is_host(hostname: str, root: str) -> bool:
    return hostname == root or hostname.endswith(f".{root}")


class PlatformAdapter(ABC):
    """Small interface hiding a platform's yt-dlp policy."""

    name: str
    language_priority: tuple[str, ...]

    @classmethod
    @abstractmethod
    def matches(cls, url: str) -> bool:
        """Return whether *url* belongs to this platform."""

    @abstractmethod
    def yt_dlp_options(self) -> dict[str, Any]:
        """Return platform-specific options for metadata and subtitle access."""


def resolve_adapter(url: str) -> PlatformAdapter:
    # Imports stay local so this module remains the single registry seam.
    from .bilibili import BilibiliAdapter
    from .youtube import YouTubeAdapter

    for adapter_type in (YouTubeAdapter, BilibiliAdapter):
        if adapter_type.matches(url):
            return adapter_type()
    raise UnsupportedSourceError(url)
