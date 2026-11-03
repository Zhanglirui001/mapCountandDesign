from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock, patch

import pytest

from count_and_design.application.ai import (
    ConfigError,
    DeepSeekOcrConfig,
    DeepSeekOcrService,
    MinerUOcrService,
    MinerUOcrConfig,
    OcrClient,
    OcrResult,
    OcrServiceError,
)


def test_screenshot_ocr_defers_missing_url_error_until_recognition(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("COUNT_AND_DESIGN_OCR_BASE_URL", raising=False)

    service = DeepSeekOcrService()

    assert service.base_url == ""
    with pytest.raises(
        OcrServiceError,
        match="COUNT_AND_DESIGN_OCR_BASE_URL",
    ):
        service.recognize(b"png")


def test_ocr_provider_configs_require_base_urls(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("COUNT_AND_DESIGN_OCR_BASE_URL", raising=False)
    monkeypatch.delenv("MINERU_BASE_URL", raising=False)
    missing_env = tmp_path / ".env"

    with pytest.raises(ConfigError, match="COUNT_AND_DESIGN_OCR_BASE_URL"):
        DeepSeekOcrConfig.from_env(missing_env)
    with pytest.raises(ConfigError, match="MINERU_BASE_URL"):
        MinerUOcrConfig.from_env(missing_env)


def test_ocr_provider_configs_read_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "COUNT_AND_DESIGN_OCR_BASE_URL",
        "http://deepseek.test/v1",
    )
    monkeypatch.setenv("COUNT_AND_DESIGN_OCR_MODEL", "deepseek-ocr")
    monkeypatch.setenv("COUNT_AND_DESIGN_OCR_PROMPT", "Read drawing.")
    monkeypatch.setenv("COUNT_AND_DESIGN_OCR_API_KEY", "test-key")
    monkeypatch.setenv("COUNT_AND_DESIGN_OCR_MAX_TOKENS", "2048")
    monkeypatch.setenv("MINERU_BASE_URL", "http://mineru.test")
    monkeypatch.setenv("MINERU_TIMEOUT", "60")
    monkeypatch.setenv("MINERU_BACKEND", "pipeline")
    monkeypatch.setenv("MINERU_EFFORT", "high")
    monkeypatch.setenv("MINERU_LANG_LIST", "en,ch")

    deepseek = DeepSeekOcrConfig.from_env()
    mineru = MinerUOcrConfig.from_env()

    assert deepseek.base_url == "http://deepseek.test/v1"
    assert deepseek.model == "deepseek-ocr"
    assert deepseek.prompt == "Read drawing."
    assert deepseek.api_key == "test-key"
    assert deepseek.max_tokens == 2048
    assert deepseek.timeout == 120.0
    assert mineru.base_url == "http://mineru.test"
    assert mineru.timeout == 60
    assert mineru.backend == "pipeline"
    assert mineru.effort == "high"
    assert mineru.lang_list == ("en", "ch")


def test_mineru_recognize_uploads_image_and_normalizes_table() -> None:
    response = Mock(status_code=200, text="")
    response.json.return_value = {
        "status": "completed",
        "results": {
            "screenshot": {
                "md_content": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                "pages": 1,
                "duration": 0.5,
            }
        },
    }
    service = MinerUOcrService(
        "http://mineru.test",
        backend="hybrid-engine",
    )

    with patch.object(
        service._session,
        "post",
        return_value=response,
    ) as mock_post:
        result = service.recognize(b"png")

    assert result.text == "A\tB\n1\t2"
    assert result.is_table is True
    assert result.provider == "mineru"
    assert mock_post.call_args.args[0] == "http://mineru.test/file_parse"
    assert mock_post.call_args.kwargs["files"] == [
        ("files", ("screenshot.png", b"png", "image/png"))
    ]
    form = dict(mock_post.call_args.kwargs["data"])
    assert form["backend"] == "hybrid-engine"
    assert form["parse_method"] == "ocr"
    assert form["return_md"] == "true"
    assert form["lang_list"] == "en"


def test_mineru_parse_file_preserves_document_metadata() -> None:
    response = Mock(status_code=200, text="")
    response.json.return_value = {
        "md_content": "# Title",
        "pages": 3,
        "duration": 1.25,
        "content_list": [{"type": "text", "text": "Title"}],
    }
    service = MinerUOcrService("http://mineru.test")

    with patch.object(service._session, "post", return_value=response):
        result = service.parse_file(
            b"pdf",
            filename="drawing.pdf",
            return_content_list=True,
        )

    assert result.markdown == "# Title"
    assert result.pages == 3
    assert result.duration == 1.25
    assert result.content_list == [{"type": "text", "text": "Title"}]


def test_mineru_parse_file_accepts_json_string_content_list() -> None:
    response = Mock(status_code=200, text="")
    response.json.return_value = {
        "md_content": "# Title",
        "content_list": '[{"type": "table", "bbox": [1, 2, 3, 4]}]',
    }
    service = MinerUOcrService("http://mineru.test")

    with patch.object(service._session, "post", return_value=response):
        result = service.parse_file(
            b"pdf",
            filename="drawing.pdf",
            return_content_list=True,
        )

    assert result.content_list == [
        {"type": "table", "bbox": [1, 2, 3, 4]}
    ]


def test_ocr_client_uses_optional_mineru_fallback() -> None:
    primary = Mock()
    primary.recognize.side_effect = OcrServiceError(
        "deepseek unavailable"
    )
    mineru = Mock()
    mineru.recognize.return_value = OcrResult(
        text="recognized",
        provider="mineru",
    )
    client = OcrClient(primary, fallback=mineru)

    result = client.recognize(b"png")

    assert result.text == "recognized"
    assert result.provider == "mineru"
    mineru.recognize.assert_called_once_with(b"png")


def test_mineru_uses_one_requests_session_per_worker_thread(monkeypatch) -> None:
    import count_and_design.application.ai.ocr.providers.mineru as mineru_module

    service = MinerUOcrService("http://mineru.test")
    created: list[object] = []
    barrier = threading.Barrier(2, timeout=10)

    def make_session():
        session = object()
        created.append(session)
        return session

    monkeypatch.setattr(mineru_module.requests, "Session", make_session)

    def get_worker_session():
        session = service._session_for_current_thread()
        barrier.wait()
        return session

    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(executor.map(lambda _index: get_worker_session(), range(2)))

    assert len(created) == 2
    assert sessions[0] is not sessions[1]
    assert all(session is not service._session for session in sessions)

def test_agent3_node_names_no_longer_use_ocr_provider_overrides(monkeypatch) -> None:
    """Production Node names use Qwen and the original crop only."""

    from count_and_design.application.ai_statistics.element_recognition import (
        Agent3ElementRecognizer,
    )

    monkeypatch.setenv("AGENT3_NODE_OCR_PROVIDER", "mineru")
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
    )

    assert recognizer._node_ocr is None
    assert recognizer._node_qwen is None
    assert recognizer._node_ocr_angles == (0,)
