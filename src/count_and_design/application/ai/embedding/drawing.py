"""Drawing-specific embedding helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .client import EmbeddingClient, EmbeddingProgressCallback
from .types import EmbeddingProvider, EmbeddingResult, ImageInput


DRAWING_CLASSIFICATION_PROMPT = (
    "Represent this drawing image for classification."
)


def create_embedding_client(
    env_path: str | Path | None = None,
    *,
    fallback: EmbeddingProvider | None = None,
) -> EmbeddingClient:
    return EmbeddingClient.from_env(env_path, fallback=fallback)


def embed_text(
    text: str,
    *,
    env_path: str | Path | None = None,
) -> EmbeddingResult:
    return create_embedding_client(env_path).embed_text(text)


def embed_drawing_image(
    image: ImageInput,
    *,
    prompt: str | None = None,
    mime_type: str | None = None,
    env_path: str | Path | None = None,
) -> EmbeddingResult:
    resolved_prompt = (
        DRAWING_CLASSIFICATION_PROMPT if prompt is None else prompt
    )
    return create_embedding_client(env_path).embed_image(
        image,
        prompt=resolved_prompt,
        mime_type=mime_type,
    )


def embed_drawing_images(
    images: Sequence[ImageInput],
    *,
    prompt: str | None = None,
    mime_type: str | None = None,
    max_concurrent: int | None = None,
    on_progress: EmbeddingProgressCallback | None = None,
    env_path: str | Path | None = None,
) -> list[EmbeddingResult]:
    resolved_prompt = (
        DRAWING_CLASSIFICATION_PROMPT if prompt is None else prompt
    )
    return create_embedding_client(env_path).embed_images(
        images,
        prompt=resolved_prompt,
        mime_type=mime_type,
        max_concurrent=max_concurrent,
        on_progress=on_progress,
    )

