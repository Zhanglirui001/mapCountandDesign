"""Provider-independent OCR facade with optional fallback."""

from __future__ import annotations

from .types import OcrProvider, OcrResult, OcrServiceError


class OcrClient:
    """Recognize screenshots using a primary and optional fallback provider."""

    def __init__(
        self,
        provider: OcrProvider,
        *,
        fallback: OcrProvider | None = None,
    ) -> None:
        self._provider = provider
        self._fallback = fallback

    @property
    def provider(self) -> OcrProvider:
        return self._provider

    @property
    def fallback(self) -> OcrProvider | None:
        return self._fallback

    def recognize(self, image_bytes: bytes) -> OcrResult:
        if not image_bytes:
            raise OcrServiceError("No image was provided.")
        try:
            return self._provider.recognize(image_bytes)
        except OcrServiceError as primary_error:
            if self._fallback is None:
                raise
            try:
                return self._fallback.recognize(image_bytes)
            except OcrServiceError as fallback_error:
                raise OcrServiceError(
                    "OCR failed on primary service "
                    f"({primary_error}) and fallback service "
                    f"({fallback_error})."
                ) from fallback_error
