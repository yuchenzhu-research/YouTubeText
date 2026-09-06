"""Progress events emitted by the acquisition pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable


class Stage(str, Enum):
    IDENTIFY = "identify"
    METADATA = "metadata"
    CAPTIONS = "captions"
    OCR = "ocr"
    WHISPER = "whisper"
    MERGE = "merge"
    EXPORT = "export"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    url: str
    stage: Stage
    message: str
    fraction: float | None = None


ProgressSink = Callable[[ProgressEvent], Awaitable[None]]


async def discard_progress(_event: ProgressEvent) -> None:
    return None
