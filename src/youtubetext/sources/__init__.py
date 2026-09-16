"""Acquire normalized YouTube/Bilibili metadata and platform captions."""

from ._yt_dlp import COOKIE_BROWSERS, YtDlpAuth
from .client import Runner, SourceClient, YtDlpRunner
from .models import (
    SourceError,
    SourceFetchError,
    SourceResult,
    SubtitleKind,
    SubtitleTrack,
    UnsupportedSourceError,
)

__all__ = [
    "COOKIE_BROWSERS",
    "Runner",
    "SourceClient",
    "SourceError",
    "SourceFetchError",
    "SourceResult",
    "SubtitleKind",
    "SubtitleTrack",
    "UnsupportedSourceError",
    "YtDlpAuth",
    "YtDlpRunner",
]
