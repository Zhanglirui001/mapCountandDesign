"""Legacy import path for screenshot OCR."""

from .providers.screenshot import DeepSeekOcrService, OcrResult, OcrServiceError

__all__ = [
    "DeepSeekOcrService",
    "OcrResult",
    "OcrServiceError",
]
