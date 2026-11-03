"""LLM provider factory driven by environment configuration."""

from __future__ import annotations

from pathlib import Path

from count_and_design.application.config import (
    BedrockLlmConfig,
    OpenRouterLlmConfig,
    QwenLlmConfig,
)
from count_and_design.application.env import env_str, load_env_file

from .providers import (
    BedrockLlmProvider,
    OpenRouterLlmProvider,
    QwenLlmProvider,
)
from .types import LlmProvider


def create_llm_provider_from_env(
    env_path: str | Path | None = None,
    *,
    provider: str | None = None,
) -> LlmProvider:
    load_env_file(env_path)
    selected = (provider or env_str("LLM_PROVIDER", "qwen")).strip().lower()
    if selected == "bedrock":
        return BedrockLlmProvider(BedrockLlmConfig.from_env(env_path))
    if selected == "openrouter":
        return OpenRouterLlmProvider(OpenRouterLlmConfig.from_env(env_path))
    if selected == "qwen":
        return QwenLlmProvider(QwenLlmConfig.from_env(env_path))
    raise ValueError(
        f"Unsupported LLM provider '{selected}'. "
        "Expected 'qwen', 'bedrock', or 'openrouter'."
    )
