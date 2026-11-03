"""Application configuration loaded from environment variables and .env files."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
from urllib.parse import urlsplit

from .env import (
    ConfigError,
    env_bool,
    env_float,
    env_int,
    env_int_or_none,
    env_required,
    env_str,
    load_env_file,
)


@dataclass(frozen=True)
class EmbeddingConfig:
    """Remote embedding configuration loaded from .env/environment."""

    base_url: str
    endpoint_path: str = "/v1/embeddings"
    models_path: str = "/v1/models"
    model: str = ""
    timeout: float = 120.0
    model_probe_timeout: float = 5.0
    max_concurrency: int = 8
    dimensions: int | None = 2048
    encoding_format: str = "float"
    system_prompt: str = "Represent the user's input."

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "EmbeddingConfig":
        load_env_file(env_path)
        defaults = cls(base_url=env_required("EMBEDDING_BASE_URL"))
        return replace(
            defaults,
            endpoint_path=env_str("EMBEDDING_ENDPOINT_PATH", defaults.endpoint_path),
            models_path=env_str("EMBEDDING_MODELS_PATH", defaults.models_path),
            model=env_str("EMBEDDING_MODEL", ""),
            timeout=env_float("EMBEDDING_TIMEOUT", defaults.timeout),
            model_probe_timeout=env_float(
                "EMBEDDING_MODEL_PROBE_TIMEOUT",
                defaults.model_probe_timeout,
            ),
            max_concurrency=env_int(
                "EMBEDDING_MAX_CONCURRENCY",
                defaults.max_concurrency,
            ),
            dimensions=env_int_or_none(
                "EMBEDDING_DIMENSIONS",
                defaults.dimensions,
            ),
            encoding_format=env_str(
                "EMBEDDING_ENCODING_FORMAT",
                defaults.encoding_format,
            ),
            system_prompt=env_str(
                "EMBEDDING_SYSTEM_PROMPT",
                defaults.system_prompt,
            ),
        )

    @property
    def endpoint_url(self) -> str:
        return _join_url(self.base_url, self.endpoint_path)

    @property
    def models_url(self) -> str:
        return _join_url(self.base_url, self.models_path)


@dataclass(frozen=True)
class DrawingClassificationConfig:
    """Agent1 page classification policy loaded from .env/environment."""

    # Keep lower-confidence candidates available for Agent1 review.
    min_score: float = 0.675
    # Compatibility fallback for older .env files. New code uses the
    # stage-specific render and embedding settings below.
    max_workers: int = 12
    render_processes: int = 4
    embedding_concurrency: int = 8
    max_in_flight: int = 12

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "DrawingClassificationConfig":
        load_env_file(env_path)
        defaults = cls()
        legacy_workers = env_int(
            "AGENT1_MAX_WORKERS",
            defaults.max_workers,
        )
        embedding_default = (
            legacy_workers
            if "AGENT1_MAX_WORKERS" in os.environ
            else defaults.embedding_concurrency
        )
        return replace(
            defaults,
            min_score=env_float(
                "DRAWING_CLASSIFICATION_MIN_SCORE",
                defaults.min_score,
            ),
            max_workers=legacy_workers,
            render_processes=env_int(
                "AGENT1_RENDER_PROCESSES",
                defaults.render_processes,
            ),
            embedding_concurrency=env_int(
                "AGENT1_EMBEDDING_CONCURRENCY",
                embedding_default,
            ),
            max_in_flight=env_int(
                "AGENT1_MAX_IN_FLIGHT",
                defaults.max_in_flight,
            ),
        )


@dataclass(frozen=True)
class DeepSeekOcrConfig:
    """DeepSeek OCR service configuration."""

    base_url: str
    model: str = ""
    prompt: str = "Free OCR."
    api_key: str = "EMPTY"
    max_tokens: int = 4096
    timeout: float = 120.0

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "DeepSeekOcrConfig":
        load_env_file(env_path)
        defaults = cls(base_url=env_required("COUNT_AND_DESIGN_OCR_BASE_URL"))
        return replace(
            defaults,
            model=env_str("COUNT_AND_DESIGN_OCR_MODEL", defaults.model),
            prompt=env_str("COUNT_AND_DESIGN_OCR_PROMPT", defaults.prompt),
            api_key=env_str("COUNT_AND_DESIGN_OCR_API_KEY", defaults.api_key),
            max_tokens=env_int(
                "COUNT_AND_DESIGN_OCR_MAX_TOKENS",
                defaults.max_tokens,
            ),
        )


@dataclass(frozen=True)
class QwenLlmConfig:
    """Qwen LLM service configuration (OpenAI-compatible vLLM endpoint)."""

    base_url: str
    chat_completions_path: str = "/v1/chat/completions"
    responses_path: str = "/v1/responses"
    models_path: str = "/v1/models"
    model: str = ""
    api_key: str = "EMPTY"
    timeout: float = 120.0
    model_probe_timeout: float = 5.0
    max_completion_tokens: int = 4096
    temperature: float | None = None
    reasoning_effort: str = "none"
    include_reasoning: bool = False

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "QwenLlmConfig":
        load_env_file(env_path)
        defaults = cls(base_url=env_required("LLM_BASE_URL"))
        temperature_raw = env_str("LLM_TEMPERATURE", "")
        temperature = (
            float(temperature_raw)
            if temperature_raw
            else defaults.temperature
        )
        return replace(
            defaults,
            chat_completions_path=env_str(
                "LLM_CHAT_COMPLETIONS_PATH",
                defaults.chat_completions_path,
            ),
            responses_path=env_str(
                "LLM_RESPONSES_PATH",
                defaults.responses_path,
            ),
            models_path=env_str("LLM_MODELS_PATH", defaults.models_path),
            model=env_str("LLM_MODEL", defaults.model),
            api_key=env_str("LLM_API_KEY", defaults.api_key),
            timeout=env_float("LLM_TIMEOUT", defaults.timeout),
            model_probe_timeout=env_float(
                "LLM_MODEL_PROBE_TIMEOUT",
                defaults.model_probe_timeout,
            ),
            max_completion_tokens=env_int(
                "LLM_MAX_COMPLETION_TOKENS",
                defaults.max_completion_tokens,
            ),
            temperature=temperature,
            reasoning_effort=env_str(
                "LLM_REASONING_EFFORT",
                defaults.reasoning_effort,
            ),
            include_reasoning=env_bool(
                "LLM_INCLUDE_REASONING",
                defaults.include_reasoning,
            ),
        )

    @classmethod
    def for_responses_endpoint(
        cls,
        endpoint_url: str,
        *,
        model: str,
        api_key: str = "EMPTY",
        timeout: float = 120.0,
        max_completion_tokens: int = 4096,
    ) -> "QwenLlmConfig":
        """Build Qwen configuration from a complete Responses API URL."""

        parsed = urlsplit(endpoint_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError(
                "Qwen Responses endpoint must be a complete HTTP(S) URL "
                "without a query or fragment."
            )
        return cls(
            base_url=f"{parsed.scheme}://{parsed.netloc}",
            responses_path=parsed.path,
            model=model,
            api_key=api_key,
            timeout=timeout,
            max_completion_tokens=max_completion_tokens,
        )

    @property
    def chat_completions_url(self) -> str:
        return _join_url(self.base_url, self.chat_completions_path)

    @property
    def responses_url(self) -> str:
        return _join_url(self.base_url, self.responses_path)

    @property
    def models_url(self) -> str:
        return _join_url(self.base_url, self.models_path)


@dataclass(frozen=True)
class BedrockLlmConfig:
    """AWS Bedrock LLM configuration (Converse API with API key auth)."""

    api_key: str
    model: str = "us.anthropic.claude-opus-4-7"
    region: str = "us-east-1"
    max_tokens: int | None = None
    timeout: float = 120.0

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "BedrockLlmConfig":
        load_env_file(env_path)
        defaults = cls(api_key=env_required("BEDROCK_API_KEY"))
        return replace(
            defaults,
            model=env_str("BEDROCK_MODEL", defaults.model),
            region=env_str("BEDROCK_REGION", defaults.region),
            max_tokens=env_int_or_none(
                "BEDROCK_MAX_TOKENS",
                defaults.max_tokens,
            ),
            timeout=env_float("BEDROCK_TIMEOUT", defaults.timeout),
        )

    @property
    def converse_url(self) -> str:
        base = (
            f"https://bedrock-runtime.{self.region}.amazonaws.com"
        )
        return f"{base}/model/{self.model}/converse"


@dataclass(frozen=True)
class OpenRouterLlmConfig:
    """OpenRouter LLM configuration (OpenAI-compatible chat completions)."""

    api_key: str
    model: str = "z-ai/glm-5.3-flash"
    base_url: str = "https://openrouter.ai/api/v1"
    chat_completions_path: str = "/chat/completions"
    models_path: str = "/models"
    max_tokens: int | None = None
    temperature: float | None = None
    timeout: float = 120.0
    model_probe_timeout: float = 5.0
    http_referer: str = ""
    app_title: str = "count-and-design"

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "OpenRouterLlmConfig":
        load_env_file(env_path)
        defaults = cls(api_key=env_required("OPENROUTER_API_KEY"))
        temperature_raw = env_str("OPENROUTER_TEMPERATURE", "")
        temperature = (
            float(temperature_raw)
            if temperature_raw
            else defaults.temperature
        )
        return replace(
            defaults,
            model=env_str("OPENROUTER_MODEL", defaults.model),
            base_url=env_str("OPENROUTER_BASE_URL", defaults.base_url),
            chat_completions_path=env_str(
                "OPENROUTER_CHAT_COMPLETIONS_PATH",
                defaults.chat_completions_path,
            ),
            models_path=env_str(
                "OPENROUTER_MODELS_PATH",
                defaults.models_path,
            ),
            max_tokens=env_int_or_none(
                "OPENROUTER_MAX_TOKENS",
                defaults.max_tokens,
            ),
            temperature=temperature,
            timeout=env_float("OPENROUTER_TIMEOUT", defaults.timeout),
            model_probe_timeout=env_float(
                "OPENROUTER_MODEL_PROBE_TIMEOUT",
                defaults.model_probe_timeout,
            ),
            http_referer=env_str(
                "OPENROUTER_HTTP_REFERER",
                defaults.http_referer,
            ),
            app_title=env_str("OPENROUTER_APP_TITLE", defaults.app_title),
        )

    @property
    def chat_completions_url(self) -> str:
        return _join_url(self.base_url, self.chat_completions_path)

    @property
    def models_url(self) -> str:
        return _join_url(self.base_url, self.models_path)


@dataclass(frozen=True)
class BlockDiagramTableConfig:
    """Block-diagram table recognition loaded from .env."""

    strategy: str = "yolo_claude"
    bedrock_max_transport_bytes: int = 5 * 1024 * 1024
    yolo_detection_confidence: float = 0.25
    yolo_fallback_confidence: float = 0.8
    yolo_bbox_expansion_ratio: float = 0.01
    # Used by yolo_glm; falls back to OPENROUTER_MODEL when unset.
    openrouter_model: str = "z-ai/glm-5.3-flash"

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "BlockDiagramTableConfig":
        load_env_file(env_path)
        defaults = cls()
        yolo = YoloDetectionConfig.from_env(env_path)
        return replace(
            defaults,
            strategy=env_str(
                "BLOCK_DIAGRAM_TABLE_STRATEGY",
                defaults.strategy,
            ),
            bedrock_max_transport_bytes=env_int(
                "BLOCK_DIAGRAM_TABLE_BEDROCK_MAX_TRANSPORT_BYTES",
                defaults.bedrock_max_transport_bytes,
            ),
            yolo_detection_confidence=yolo.block_schedule_confidence,
            yolo_fallback_confidence=yolo.block_schedule_fallback_confidence,
            yolo_bbox_expansion_ratio=yolo.block_schedule_bbox_expansion_ratio,
            openrouter_model=env_str(
                "BLOCK_DIAGRAM_TABLE_OPENROUTER_MODEL",
                env_str("OPENROUTER_MODEL", defaults.openrouter_model),
            ),
        )


@dataclass(frozen=True)
class MinerUOcrConfig:
    """Built-in MinerU parsing service configuration."""

    base_url: str
    timeout: float = 300.0
    connect_timeout: float = 10.0
    backend: str = "hybrid-engine"
    effort: str = "medium"
    lang_list: tuple[str, ...] = ("en",)

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "MinerUOcrConfig":
        load_env_file(env_path)
        defaults = cls(base_url=env_required("MINERU_BASE_URL"))
        lang_raw = env_str("MINERU_LANG_LIST", ",".join(defaults.lang_list))
        lang_list = tuple(
            part.strip()
            for part in lang_raw.split(",")
            if part.strip()
        )
        return replace(
            defaults,
            timeout=env_float("MINERU_TIMEOUT", defaults.timeout),
            connect_timeout=env_float(
                "MINERU_CONNECT_TIMEOUT",
                defaults.connect_timeout,
            ),
            backend=env_str("MINERU_BACKEND", defaults.backend),
            effort=env_str("MINERU_EFFORT", defaults.effort),
            lang_list=lang_list or defaults.lang_list,
        )


@dataclass(frozen=True)
class OverallSiteExtractionConfig:
    """Agent2 YOLO/MinerU table-location and vision-extraction configuration.

    ``strategy`` selects both the table locator and the vision provider:
    ``*_claude`` uses Claude on Bedrock, ``*_glm`` uses GLM through
    OpenRouter. ``yolo_glm`` is the default.
    """

    strategy: str = "yolo_glm"
    mineru_max_attempts: int = 3
    mineru_retry_max_dimension: int = 6000
    bedrock_max_transport_bytes: int = 5 * 1024 * 1024
    bedrock_max_tokens: int | None = 12000
    # Used by the *_glm strategies; falls back to OPENROUTER_MODEL when unset.
    openrouter_model: str = "z-ai/glm-5.3-flash"
    yolo_detection_confidence: float = 0.5
    yolo_fallback_confidence: float = 0.9
    yolo_weak_inv_confidence: float = 0.5
    yolo_bbox_expansion_ratio: float = 0.01
    batch_claude_enabled: bool = True
    batch_claude_max_pages: int = 20

    @property
    def uses_glm(self) -> bool:
        """Whether the configured strategy reads tables with GLM."""

        return self.strategy.casefold().endswith("_glm")

    @property
    def vision_provider(self) -> str:
        """Provider name recorded in extraction diagnostics."""

        return "openrouter" if self.uses_glm else "bedrock"

    @property
    def vision_provider_label(self) -> str:
        """Human-readable provider name used in errors and logs."""

        return "GLM" if self.uses_glm else "Claude"

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "OverallSiteExtractionConfig":
        load_env_file(env_path)
        defaults = cls()
        return replace(
            defaults,
            strategy=env_str(
                "AGENT2_OVERALL_TABLE_STRATEGY",
                defaults.strategy,
            ),
            mineru_max_attempts=env_int(
                "AGENT2_MINERU_MAX_ATTEMPTS",
                defaults.mineru_max_attempts,
            ),
            mineru_retry_max_dimension=env_int(
                "AGENT2_MINERU_RETRY_MAX_DIMENSION",
                defaults.mineru_retry_max_dimension,
            ),
            bedrock_max_transport_bytes=env_int(
                "AGENT2_BEDROCK_MAX_TRANSPORT_BYTES",
                defaults.bedrock_max_transport_bytes,
            ),
            bedrock_max_tokens=env_int_or_none(
                "AGENT2_BEDROCK_MAX_TOKENS",
                defaults.bedrock_max_tokens,
            ),
            openrouter_model=env_str(
                "AGENT2_OVERALL_TABLE_OPENROUTER_MODEL",
                env_str("OPENROUTER_MODEL", defaults.openrouter_model),
            ),
            yolo_detection_confidence=_confidence_in_unit_interval(
                "AGENT2_YOLO_OVERALL_SCHEDULE_CONFIDENCE",
                env_float(
                    "AGENT2_YOLO_OVERALL_SCHEDULE_CONFIDENCE",
                    defaults.yolo_detection_confidence,
                ),
            ),
            yolo_fallback_confidence=_confidence_in_unit_interval(
                "AGENT2_YOLO_OVERALL_SCHEDULE_FALLBACK_CONFIDENCE",
                env_float(
                    "AGENT2_YOLO_OVERALL_SCHEDULE_FALLBACK_CONFIDENCE",
                    defaults.yolo_fallback_confidence,
                ),
            ),
            yolo_weak_inv_confidence=_confidence_in_unit_interval(
                "AGENT2_YOLO_OVERALL_SCHEDULE_WEAK_INV_CONFIDENCE",
                env_float(
                    "AGENT2_YOLO_OVERALL_SCHEDULE_WEAK_INV_CONFIDENCE",
                    defaults.yolo_weak_inv_confidence,
                ),
            ),
            yolo_bbox_expansion_ratio=_confidence_in_unit_interval(
                "AGENT2_YOLO_OVERALL_SCHEDULE_BBOX_EXPANSION_RATIO",
                env_float(
                    "AGENT2_YOLO_OVERALL_SCHEDULE_BBOX_EXPANSION_RATIO",
                    defaults.yolo_bbox_expansion_ratio,
                ),
            ),
            batch_claude_enabled=env_bool(
                "AGENT2_BATCH_CLAUDE_ENABLED",
                defaults.batch_claude_enabled,
            ),
            batch_claude_max_pages=max(
                2,
                env_int(
                    "AGENT2_BATCH_CLAUDE_MAX_PAGES",
                    defaults.batch_claude_max_pages,
                ),
            ),
        )


@dataclass(frozen=True)
class Agent3ImageConfig:
    """Agent3 page image preprocessing loaded from .env/environment.

    ``grayscale_enabled`` optionally grayscales the shared recognition page
    used by table OCR and Node crops. ``line_enhance_enabled`` applies only to
    Tracker/Box YOLO detection so the table region stays sharp. UI display
    always keeps a separate color original.
    """

    grayscale_enabled: bool = False
    line_enhance_enabled: bool = False

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "Agent3ImageConfig":
        load_env_file(env_path)
        defaults = cls()
        return replace(
            defaults,
            grayscale_enabled=env_bool(
                "AGENT3_GRAYSCALE_IMAGES",
                defaults.grayscale_enabled,
            ),
            line_enhance_enabled=env_bool(
                "AGENT3_LINE_ENHANCE",
                defaults.line_enhance_enabled,
            ),
        )


@dataclass(frozen=True)
class YoloDetectionConfig:
    """Agent3 local YOLO detection thresholds loaded from .env/environment."""

    tracker_confidence: float = 0.35
    box_confidence: float = 0.25
    block_schedule_confidence: float = 0.25
    block_schedule_fallback_confidence: float = 0.8
    block_schedule_bbox_expansion_ratio: float = 0.01

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
    ) -> "YoloDetectionConfig":
        load_env_file(env_path)
        defaults = cls()
        return replace(
            defaults,
            tracker_confidence=_confidence_in_unit_interval(
                "AGENT3_YOLO_TRACKER_CONFIDENCE",
                env_float(
                    "AGENT3_YOLO_TRACKER_CONFIDENCE",
                    defaults.tracker_confidence,
                ),
            ),
            box_confidence=_confidence_in_unit_interval(
                "AGENT3_YOLO_BOX_CONFIDENCE",
                env_float(
                    "AGENT3_YOLO_BOX_CONFIDENCE",
                    defaults.box_confidence,
                ),
            ),
            block_schedule_confidence=_confidence_in_unit_interval(
                "AGENT3_YOLO_BLOCK_SCHEDULE_CONFIDENCE",
                env_float(
                    "AGENT3_YOLO_BLOCK_SCHEDULE_CONFIDENCE",
                    defaults.block_schedule_confidence,
                ),
            ),
            block_schedule_fallback_confidence=_confidence_in_unit_interval(
                "AGENT3_YOLO_BLOCK_SCHEDULE_FALLBACK_CONFIDENCE",
                env_float(
                    "AGENT3_YOLO_BLOCK_SCHEDULE_FALLBACK_CONFIDENCE",
                    defaults.block_schedule_fallback_confidence,
                ),
            ),
            block_schedule_bbox_expansion_ratio=_confidence_in_unit_interval(
                "AGENT3_YOLO_BLOCK_SCHEDULE_BBOX_EXPANSION_RATIO",
                env_float(
                    "AGENT3_YOLO_BLOCK_SCHEDULE_BBOX_EXPANSION_RATIO",
                    defaults.block_schedule_bbox_expansion_ratio,
                ),
            ),
        )


def _confidence_in_unit_interval(name: str, value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ConfigError(
            f"{name} must be between 0 and 1 inclusive, got {value}."
        )
    return value

def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
