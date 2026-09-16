"""Local speech recognition for YouTubeText.

Whisper only converts audio into timestamped text.  This package intentionally
does not generate summaries and does not depend on an LLM.
"""

from .backend import ASRBackend, ASRResult
from .faster_whisper import FasterWhisperASR, FasterWhisperUnavailableError
from .mlx_whisper import MLXWhisperASR
from .models import MODELS, WhisperModel, WhisperModelCache, select_model

__all__ = [
    "ASRBackend",
    "ASRResult",
    "FasterWhisperASR",
    "FasterWhisperUnavailableError",
    "MLXWhisperASR",
    "MODELS",
    "WhisperModel",
    "WhisperModelCache",
    "select_model",
]
