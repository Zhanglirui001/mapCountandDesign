"""Agent3 Node-name / review provider selection through the existing switches.

The crop reader follows ``LLM_PROVIDER``; the page-level review follows
``AGENT3_NODE_REVIEW_LLM_PROVIDER`` (Bedrock Claude when unset). Both build
their provider lazily on the first call, so these tests spy on the builder
instead of performing a request.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from count_and_design.application.ai import (
    BoundingBox,
    BedrockLlmProvider,
    OpenRouterLlmProvider,
    QwenLlmProvider,
    create_llm_provider_from_env,
)
from count_and_design.application.ai_statistics.element_recognition import (
    NodeNameReviewCandidate,
    NodeNameReviewError,
    NodeNameReviewer,
    NodeStringTableRow,
)
from count_and_design.application.ai_statistics.element_recognition.node_label import (
    NodeNameRecognizer,
)
from count_and_design.application.ai_statistics.element_recognition.node_label import (
    recognizer as recognizer_module,
)
from count_and_design.application.ai_statistics.element_recognition.node_label import (
    reviewer as reviewer_module,
)

# Variables read while building either provider. Clearing them keeps the host
# machine and the working copy's .env out of these assertions.
_PROVIDER_ENV_NAMES = [
    "ENV_FILE",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "BEDROCK_API_KEY",
    "BEDROCK_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "AGENT3_NODE_REVIEW_LLM_PROVIDER",
]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for name in _PROVIDER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # Point ENV_FILE at a missing file last, so the builders cannot fall back
    # to the working copy's .env.
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "missing.env"))
    yield


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (120, 60), "white").save(output, format="PNG")
    return output.getvalue()


def _set_openrouter(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "z-ai/glm-5.3-flash:nitro")


class _FakeVision:
    """Minimal complete_with_image provider recording its calls."""

    def __init__(self, content: str = "INV1001-CBX-01") -> None:
        self.content = content
        self.calls: list[tuple] = []

    def complete_with_image(self, prompt, image, **kwargs):
        self.calls.append((prompt, image, kwargs))
        return SimpleNamespace(content=self.content, model="fake", usage={})


class _SpyBuilder:
    """Records the arguments the role passed and returns a fake provider."""

    def __init__(self, provider) -> None:
        self.provider = provider
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self.provider


def _read_node_name(recognizer: NodeNameRecognizer) -> tuple[str, float | None]:
    return recognizer.read_image(
        _png(),
        7,
        1,
        (NodeStringTableRow("INV1001-CBX-01", 20),),
    )


def _review(reviewer: NodeNameReviewer):
    return reviewer.review(
        _png(),
        (NodeStringTableRow("INV1001-CBX-01", 20),),
        (
            NodeNameReviewCandidate(
                number=1,
                recognized_name="INV1001-CBX-01",
                bbox=BoundingBox(0, 0, 10, 10),
            ),
        ),
        ("INV1001",),
    )


def test_llm_provider_env_moves_the_node_name_reader(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    _set_openrouter(monkeypatch)

    provider = create_llm_provider_from_env()

    assert isinstance(provider, OpenRouterLlmProvider)
    assert provider.config.model == "z-ai/glm-5.3-flash:nitro"


def test_llm_provider_env_keeps_qwen_as_the_default(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://qwen.test")
    monkeypatch.setenv("LLM_MODEL", "Qwen3.5-9B")

    provider = create_llm_provider_from_env()

    assert isinstance(provider, QwenLlmProvider)
    assert provider.config.model == "Qwen3.5-9B"


def test_node_name_reader_builds_the_provider_lazily(monkeypatch) -> None:
    fake = _FakeVision("INV1001-CBX-01")
    spy = _SpyBuilder(fake)
    monkeypatch.setattr(recognizer_module, "create_llm_provider_from_env", spy)
    recognizer = NodeNameRecognizer()

    assert recognizer.qwen is None

    text, confidence = _read_node_name(recognizer)

    assert text == "INV1001-CBX-01"
    assert confidence is None
    assert recognizer.qwen is fake
    # The reader owns the model choice (LLM_PROVIDER) and passes no arguments.
    assert spy.calls == [{}]
    # A single crop must keep omitting an output cap.
    assert "max_completion_tokens" not in fake.calls[0][2]
    assert fake.calls[0][2]["mime_type"] == "image/png"


def test_review_defaults_to_bedrock(monkeypatch) -> None:
    fake = _FakeVision('{"inv_name":null,"corrections":"All correct"}')
    spy = _SpyBuilder(fake)
    monkeypatch.setattr(reviewer_module, "create_llm_provider_from_env", spy)

    assert _review(NodeNameReviewer()).all_correct is True

    assert spy.calls == [{"provider": "bedrock"}]


def test_review_follows_its_own_provider_switch(monkeypatch) -> None:
    fake = _FakeVision('{"inv_name":"INV1001","corrections":"All correct"}')
    spy = _SpyBuilder(fake)
    monkeypatch.setattr(reviewer_module, "create_llm_provider_from_env", spy)
    monkeypatch.setenv("AGENT3_NODE_REVIEW_LLM_PROVIDER", "openrouter")
    reviewer = NodeNameReviewer()

    result = _review(reviewer)

    assert result.inv_name == "INV1001"
    assert reviewer._bedrock is fake
    assert spy.calls == [{"provider": "openrouter"}]
    # A reasoning review model needs room for its thinking plus the JSON.
    assert fake.calls[0][2]["max_completion_tokens"] >= 2048


def test_review_provider_switch_does_not_follow_llm_provider(monkeypatch) -> None:
    fake = _FakeVision('{"inv_name":null,"corrections":"All correct"}')
    spy = _SpyBuilder(fake)
    monkeypatch.setattr(reviewer_module, "create_llm_provider_from_env", spy)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    _review(NodeNameReviewer())

    assert spy.calls == [{"provider": "bedrock"}]


def test_review_rejects_an_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("AGENT3_NODE_REVIEW_LLM_PROVIDER", "gemini")

    with pytest.raises(NodeNameReviewError, match="gemini"):
        _review(NodeNameReviewer())


def test_injected_providers_win_over_the_env_switches(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("env provider must not be built when one is injected")

    monkeypatch.setattr(reviewer_module, "create_llm_provider_from_env", _boom)
    monkeypatch.setattr(recognizer_module, "create_llm_provider_from_env", _boom)

    node_name_fake = _FakeVision("INV1001-CBX-01")
    assert _read_node_name(NodeNameRecognizer(qwen=node_name_fake))[0] == (
        "INV1001-CBX-01"
    )

    review_fake = _FakeVision('{"inv_name":null,"corrections":"All correct"}')
    reviewer = NodeNameReviewer(review_fake)
    assert _review(reviewer).all_correct is True
    assert reviewer._bedrock is review_fake
