"""Shared LLM contracts and normalized results."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

ChatRole = Literal["system", "user", "assistant"]


class LlmServiceError(RuntimeError):
    """Raised when chat completion or provider access fails."""


@dataclass(frozen=True)
class ChatMessage:
    """Single chat message for OpenAI-compatible chat completions."""

    role: ChatRole
    content: str


@dataclass(frozen=True)
class LlmCompletionResult:
    """Provider-independent normalized chat completion result."""

    content: str
    model: str = ""
    finish_reason: str = ""
    reasoning: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


class LlmProvider(Protocol):
    """Minimum contract implemented by Qwen, Bedrock, or OpenRouter."""

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
        include_reasoning: bool | None = None,
        stream: bool = False,
    ) -> LlmCompletionResult: ...


@runtime_checkable
class ResponsesLlmProvider(Protocol):
    """Optional provider capability for OpenAI-compatible Responses APIs."""

    def respond(
        self,
        input_text: str,
        *,
        instructions: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        stream: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmCompletionResult: ...
