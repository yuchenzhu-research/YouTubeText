"""Local speech recognition for YouTubeText.

Whisper only converts audio into timestamped text.  This package intentionally
does not generate summaries and does not depend on an LLM.
"""

from .backend import ASRBackend, ASRResult
from .mlx_whisper import MLXWhisperASR
from .models import MODELS, WhisperModel, WhisperModelCache, select_model

__all__ = [
    "ASRBackend",
    "ASRResult",
    "MLXWhisperASR",
    "MODELS",
    "WhisperModel",
    "WhisperModelCache",
    "select_model",
]
