"""Public OCR API with DeepSeek and MinerU providers."""

from count_and_design.application.config import (
    DeepSeekOcrConfig,
    MinerUOcrConfig,
)

from .client import OcrClient
from .providers import DeepSeekOcrService, MinerUOcrService, MinerUParseResult
from .types import OcrProvider, OcrResult, OcrServiceError

__all__ = [
    "DeepSeekOcrConfig",
    "DeepSeekOcrService",
    "MinerUOcrConfig",
    "MinerUOcrService",
    "MinerUParseResult",
    "OcrClient",
    "OcrProvider",
    "OcrResult",
    "OcrServiceError",
]
