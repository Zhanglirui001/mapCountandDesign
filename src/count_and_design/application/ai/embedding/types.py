"""Shared contracts and normalized results for embedding providers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


ImageInput = str | Path | bytes


class EmbeddingServiceError(RuntimeError):
    """Raised when embedding generation or provider access fails."""


@dataclass(frozen=True)
class EmbeddingResult:
    """Provider-independent normalized embedding result."""

    embedding: list[float]
    index: int = 0
    model: str = ""
    raw: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


class EmbeddingProvider(Protocol):
    """Minimum contract implemented by internal or external providers."""

    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingResult]: ...

    def embed_image(
        self,
        image: ImageInput,
        *,
        prompt: str | None = None,
        mime_type: str | None = None,
    ) -> EmbeddingResult: ...
