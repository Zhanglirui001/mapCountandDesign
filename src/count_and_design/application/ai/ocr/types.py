"""Shared OCR contracts and normalized results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class OcrServiceError(RuntimeError):
    """Raised when OCR recognition or provider access fails."""


@dataclass(frozen=True)
class OcrResult:
    """Provider-independent OCR content prepared for application use."""

    text: str
    is_table: bool = False
    raw: dict[str, Any] | None = None
    provider: str = ""


class OcrProvider(Protocol):
    """Minimum contract implemented by DeepSeek, MinerU, or future OCR."""

    def recognize(self, image_bytes: bytes) -> OcrResult: ...
