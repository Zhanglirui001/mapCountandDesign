"""Concrete OCR provider implementations."""

from .mineru import MinerUOcrService, MinerUParseResult
from .screenshot import DeepSeekOcrService

__all__ = [
    "DeepSeekOcrService",
    "MinerUOcrService",
    "MinerUParseResult",
]
