"""Provider-independent LLM facade with optional fallback."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, TypeVar

from count_and_design.application.config import (
    BedrockLlmConfig,
    OpenRouterLlmConfig,
    QwenLlmConfig,
)

from .factory import create_llm_provider_from_env
from .providers import (
    BedrockLlmProvider,
    OpenRouterLlmProvider,
    QwenLlmProvider,
)
from .types import (
    ChatMessage,
    LlmCompletionResult,
    LlmProvider,
    LlmServiceError,
    ResponsesLlmProvider,
)

_ResultT = TypeVar("_ResultT")
_KNOWN_PROVIDERS = (QwenLlmProvider, BedrockLlmProvider, OpenRouterLlmProvider)


class LlmClient:
    """Stable facade over Qwen, Bedrock, OpenRouter, or another LLM provider."""

    def __init__(
        self,
        config: QwenLlmConfig | None = None,
        *,
        provider: LlmProvider | None = None,
        fallback: LlmProvider | None = None,
    ) -> None:
        if config is not None and provider is not None:
            raise ValueError("Pass either config or provider, not both.")
        if provider is None:
            provider = QwenLlmProvider(config or QwenLlmConfig.from_env())
        self._provider = provider
        self._fallback = fallback

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
        *,
        provider: str | None = None,
        fallback: LlmProvider | None = None,
    ) -> "LlmClient":
        return cls(
            provider=create_llm_provider_from_env(
                env_path,
                provider=provider,
            ),
            fallback=fallback,
        )

    @property
    def config(self) -> QwenLlmConfig | BedrockLlmConfig | OpenRouterLlmConfig:
        if isinstance(self._provider, _KNOWN_PROVIDERS):
            return self._provider.config
        raise AttributeError(
            "This LLM provider does not expose a known config object."
        )

    @property
    def provider(self) -> LlmProvider:
        return self._provider

    @property
    def fallback(self) -> LlmProvider | None:
        return self._fallback

    def list_models(self, *, refresh: bool = False) -> list[str]:
        if isinstance(self._provider, _KNOWN_PROVIDERS):
            return self._provider.list_models(refresh=refresh)
        raise LlmServiceError(
            "The configured LLM provider does not support model discovery."
        )

    def resolve_model(self, *, refresh: bool = False) -> str:
        if isinstance(self._provider, _KNOWN_PROVIDERS):
            return self._provider.resolve_model(refresh=refresh)
        raise LlmServiceError(
            "The configured LLM provider does not support model discovery."
        )

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
        include_reasoning: bool | None = None,
        stream: bool = False,
    ) -> LlmCompletionResult:
        if not messages:
            raise LlmServiceError("No chat messages were provided.")
        return self._with_fallback(
            "Chat completion",
            lambda provider: provider.chat(
                messages,
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
                reasoning_effort=reasoning_effort,
                include_reasoning=include_reasoning,
                stream=stream,
            ),
        )

    def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
        include_reasoning: bool | None = None,
        stream: bool = False,
    ) -> LlmCompletionResult:
        if not prompt.strip():
            raise LlmServiceError("Prompt is empty.")
        if isinstance(self._provider, _KNOWN_PROVIDERS):
            return self._with_fallback(
                "Chat completion",
                lambda provider: provider.complete(
                    prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_completion_tokens=max_completion_tokens,
                    reasoning_effort=reasoning_effort,
                    include_reasoning=include_reasoning,
                    stream=stream,
                ),
            )
        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(
                ChatMessage(role="system", content=system_prompt)
            )
        messages.append(ChatMessage(role="user", content=prompt))
        return self.chat(
            messages,
            temperature=temperature,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
            include_reasoning=include_reasoning,
            stream=stream,
        )

    def respond(
        self,
        input_text: str,
        *,
        instructions: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        stream: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmCompletionResult:
        """Call a provider that supports the OpenAI Responses API."""

        if not input_text.strip():
            raise LlmServiceError("Responses input is empty.")

        def call(provider: LlmProvider) -> LlmCompletionResult:
            if not isinstance(provider, ResponsesLlmProvider):
                raise LlmServiceError(
                    "The configured LLM provider does not support "
                    "the Responses API."
                )
            return provider.respond(
                input_text,
                instructions=instructions,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                stream=stream,
                extra_body=extra_body,
            )

        return self._with_fallback("Responses completion", call)

    def _with_fallback(
        self,
        operation: str,
        call: Callable[[LlmProvider], _ResultT],
    ) -> _ResultT:
        try:
            return call(self._provider)
        except LlmServiceError as primary_error:
            if self._fallback is None:
                raise
            try:
                return call(self._fallback)
            except LlmServiceError as fallback_error:
                raise LlmServiceError(
                    f"{operation} failed on primary service "
                    f"({primary_error}) and fallback service "
                    f"({fallback_error})."
                ) from fallback_error
