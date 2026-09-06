"""Platform seam used internally by source acquisition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from .models import UnsupportedSourceError

T = TypeVar("T")


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
    retry_delays: tuple[float, ...] = ()

    @classmethod
    @abstractmethod
    def matches(cls, url: str) -> bool:
        """Return whether *url* belongs to this platform."""

    def request_url(self, url: str) -> str:
        """Return the URL form that should be sent to yt-dlp."""

        return url

    def is_transient_error(self, error: Exception) -> bool:
        """Return whether an operation may be repeated after a short delay."""

        return False

    @abstractmethod
    def yt_dlp_options(self) -> dict[str, Any]:
        """Return platform-specific options for metadata and subtitle access."""


def run_with_platform_retries(
    adapter: PlatformAdapter,
    operation: Callable[[], T],
    *,
    sleep: Callable[[float], None],
) -> T:
    """Retry only failures explicitly classified by the platform adapter."""

    delays = iter(adapter.retry_delays)
    while True:
        try:
            return operation()
        except Exception as exc:
            if not adapter.is_transient_error(exc):
                raise
            try:
                delay = next(delays)
            except StopIteration:
                raise
            sleep(delay)


def resolve_adapter(url: str) -> PlatformAdapter:
    # Imports stay local so this module remains the single registry seam.
    from .bilibili import BilibiliAdapter
    from .youtube import YouTubeAdapter

    for adapter_type in (YouTubeAdapter, BilibiliAdapter):
        if adapter_type.matches(url):
            return adapter_type()
    raise UnsupportedSourceError(url)
