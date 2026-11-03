"""Provider-independent embedding facade with batching and fallback."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TypeVar

from count_and_design.application.config import EmbeddingConfig

from .providers import OpenAIEmbeddingProvider
from .types import (
    EmbeddingProvider,
    EmbeddingResult,
    EmbeddingServiceError,
    ImageInput,
)


EmbeddingProgressCallback = Callable[[int, int], None]
_ResultT = TypeVar("_ResultT")


class EmbeddingClient:
    """Stable facade over an internal provider and optional fallback."""

    def __init__(
        self,
        config: EmbeddingConfig | None = None,
        *,
        provider: EmbeddingProvider | None = None,
        fallback: EmbeddingProvider | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        if config is not None and provider is not None:
            raise ValueError("Pass either config or provider, not both.")
        if provider is None:
            resolved_config = config or EmbeddingConfig.from_env()
            provider = OpenAIEmbeddingProvider(resolved_config)
            default_concurrency = resolved_config.max_concurrency
        else:
            default_concurrency = 8
        self._provider = provider
        self._fallback = fallback
        self._max_concurrency = (
            default_concurrency
            if max_concurrency is None
            else max_concurrency
        )

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
        *,
        fallback: EmbeddingProvider | None = None,
    ) -> "EmbeddingClient":
        return cls(
            EmbeddingConfig.from_env(env_path),
            fallback=fallback,
        )

    @property
    def config(self) -> EmbeddingConfig:
        if isinstance(self._provider, OpenAIEmbeddingProvider):
            return self._provider.config
        raise AttributeError(
            "This embedding provider does not expose EmbeddingConfig."
        )

    @property
    def provider(self) -> EmbeddingProvider:
        return self._provider

    @property
    def fallback(self) -> EmbeddingProvider | None:
        return self._fallback

    def list_models(self, *, refresh: bool = False) -> list[str]:
        if not isinstance(self._provider, OpenAIEmbeddingProvider):
            raise EmbeddingServiceError(
                "The configured embedding provider does not support "
                "OpenAI model discovery."
            )
        return self._provider.list_models(refresh=refresh)

    def resolve_model(self, *, refresh: bool = False) -> str:
        if not isinstance(self._provider, OpenAIEmbeddingProvider):
            raise EmbeddingServiceError(
                "The configured embedding provider does not support "
                "OpenAI model discovery."
            )
        return self._provider.resolve_model(refresh=refresh)

    def embed_text(self, text: str) -> EmbeddingResult:
        if not text.strip():
            raise EmbeddingServiceError("Text input is empty.")
        return self._with_fallback(
            "Text embedding",
            lambda provider: provider.embed_text(text),
        )

    def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingResult]:
        normalized = [text for text in texts if text.strip()]
        if not normalized:
            raise EmbeddingServiceError("No text inputs were provided.")
        return self._with_fallback(
            "Text batch embedding",
            lambda provider: provider.embed_texts(normalized),
        )

    def embed_image(
        self,
        image: ImageInput,
        *,
        prompt: str | None = None,
        mime_type: str | None = None,
    ) -> EmbeddingResult:
        return self._with_fallback(
            "Image embedding",
            lambda provider: provider.embed_image(
                image,
                prompt=prompt,
                mime_type=mime_type,
            ),
        )

    def embed_images(
        self,
        images: Sequence[ImageInput],
        *,
        prompt: str | None = None,
        mime_type: str | None = None,
        max_concurrent: int | None = None,
        on_progress: EmbeddingProgressCallback | None = None,
    ) -> list[EmbeddingResult]:
        """Embed images concurrently while preserving their input order."""

        image_list = list(images)
        if not image_list:
            raise EmbeddingServiceError("No image inputs were provided.")
        workers = (
            self._max_concurrency
            if max_concurrent is None
            else max_concurrent
        )
        if workers <= 0:
            raise EmbeddingServiceError(
                "Image embedding concurrency must be greater than zero."
            )

        results: list[EmbeddingResult | None] = [None] * len(image_list)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self.embed_image,
                    image,
                    prompt=prompt,
                    mime_type=mime_type,
                ): index
                for index, image in enumerate(image_list)
            }
            completed = 0
            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()
                completed += 1
                if on_progress is not None:
                    on_progress(completed, len(image_list))
        return [result for result in results if result is not None]

    def _with_fallback(
        self,
        operation: str,
        call: Callable[[EmbeddingProvider], _ResultT],
    ) -> _ResultT:
        try:
            return call(self._provider)
        except EmbeddingServiceError as primary_error:
            if self._fallback is None:
                raise
            try:
                return call(self._fallback)
            except EmbeddingServiceError as fallback_error:
                raise EmbeddingServiceError(
                    f"{operation} failed on primary service "
                    f"({primary_error}) and fallback service "
                    f"({fallback_error})."
                ) from fallback_error
