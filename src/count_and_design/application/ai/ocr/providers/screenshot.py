"""Screenshot OCR provider for legacy DeepSeek-OCR recognition.

Talks to a vLLM-hosted DeepSeek-OCR model through its OpenAI-compatible
``/v1/chat/completions`` endpoint. The screenshot is sent as a base64
``image_url`` data URI. DeepSeek-OCR returns tables as HTML ``<table>``
markup, which we convert to TSV so it can be pasted directly into Excel;
any other content is kept as plain text.
"""

from __future__ import annotations

import base64
from html.parser import HTMLParser
import re
from typing import Any

import requests

from count_and_design.application.env import (
    env_int,
    env_str,
    load_env_file,
)
from ..types import OcrResult, OcrServiceError


DEFAULT_OCR_PROMPT = "Free OCR."
# The model's context window (8192) is shared between the image/prompt input
# and the generated output. Reserving the whole budget for output leaves no
# room for the input and the server rejects the request (HTTP 400). Keep a
# conservative output cap and allow overriding it via an env var.
DEFAULT_OCR_MAX_TOKENS = 4096

# Token ids for <td>/</td> in DeepSeek-OCR — whitelisted from the n-gram
# repetition logits processor so large tables are not truncated.
_TABLE_TOKEN_WHITELIST = [128821, 128822]


class DeepSeekOcrService:
    """Client for a vLLM DeepSeek-OCR OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        load_env_file()
        configured_url = base_url or env_str(
            "COUNT_AND_DESIGN_OCR_BASE_URL", ""
        )
        self._base_url = configured_url.rstrip("/")
        self._model = model or env_str("COUNT_AND_DESIGN_OCR_MODEL", "") or None
        self._prompt = prompt or env_str(
            "COUNT_AND_DESIGN_OCR_PROMPT", DEFAULT_OCR_PROMPT
        )
        self._api_key = env_str("COUNT_AND_DESIGN_OCR_API_KEY", "EMPTY")
        self._max_tokens = env_int(
            "COUNT_AND_DESIGN_OCR_MAX_TOKENS",
            DEFAULT_OCR_MAX_TOKENS,
        )
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _resolve_model(self) -> str:
        """Return the configured model, or discover one from ``/v1/models``."""
        if self._model:
            return self._model
        try:
            response = requests.get(
                f"{self._base_url}/models",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OcrServiceError(
                f"Could not query available OCR models: {exc}"
            ) from exc

        if not 200 <= response.status_code < 300:
            raise OcrServiceError(
                f"OCR model list request returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OcrServiceError("OCR model list response was not valid JSON.") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and entry.get("id"):
                    self._model = str(entry["id"])
                    return self._model
        raise OcrServiceError(
            "OCR server did not report any available models. "
            "Set COUNT_AND_DESIGN_OCR_MODEL to the model name."
        )

    def recognize(self, image_bytes: bytes) -> OcrResult:
        """Recognize text/table content from a PNG screenshot."""
        if not image_bytes:
            raise OcrServiceError("No screenshot image was provided.")
        if not self._base_url:
            raise OcrServiceError(
                "Missing required environment variable: "
                "COUNT_AND_DESIGN_OCR_BASE_URL"
            )

        model = self._resolve_model()
        data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": self._prompt},
                ],
            }
        ]

        # First attempt includes the raw-vLLM tuning fields that keep large
        # tables from being truncated. A gateway that fronts multiple backends
        # may reject unknown fields (400/422); if so, retry with a plain
        # OpenAI-compatible body.
        payload = self._post_chat(model, messages, with_vllm_extras=True)
        if payload is None:
            payload = self._post_chat(model, messages, with_vllm_extras=False)
        if payload is None:
            raise OcrServiceError("OCR server rejected the request.")

        content = self._extract_content(payload)
        if not content:
            raise OcrServiceError("OCR server response did not contain recognizable text.")

        return self._to_result(content, payload)

    def _post_chat(
        self, model: str, messages: list[dict[str, Any]], *, with_vllm_extras: bool
    ) -> dict[str, Any] | None:
        """POST to ``/chat/completions``.

        Returns the parsed payload on success, or ``None`` when the request was
        rejected as a bad request (so the caller can retry without the
        vLLM-specific extras). Any other failure raises ``OcrServiceError``.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": 0.0,
        }
        if with_vllm_extras:
            body["skip_special_tokens"] = False
            body["vllm_xargs"] = {
                "ngram_size": 30,
                "window_size": 90,
                "whitelist_token_ids": _TABLE_TOKEN_WHITELIST,
            }

        try:
            response = requests.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OcrServiceError(f"OCR request failed: {exc}") from exc

        if with_vllm_extras and response.status_code in (400, 422):
            # The gateway does not understand the vLLM extras — signal a retry.
            return None

        if not 200 <= response.status_code < 300:
            detail = response.text.strip()[:300]
            message = f"OCR server returned HTTP {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            raise OcrServiceError(message)

        try:
            payload = response.json()
        except ValueError as exc:
            raise OcrServiceError("OCR server returned an empty or invalid response.") from exc
        if not isinstance(payload, dict):
            raise OcrServiceError("OCR server returned an unsupported response format.")
        return payload

    def _extract_content(self, payload: Any) -> str:
        """Pull ``choices[0].message.content`` from a chat-completion payload."""
        if not isinstance(payload, dict):
            raise OcrServiceError("OCR server returned an unsupported response format.")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OcrServiceError("OCR server response contained no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            # Some servers return content as a list of parts.
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str):
            return ""
        return self._strip_special_tokens(content).strip()

    def _strip_special_tokens(self, content: str) -> str:
        """Remove model control tokens (e.g. ``<|end▁of▁sentence|>``)."""
        return re.sub(r"<\|[^|]*\|>", "", content)

    def _to_result(self, content: str, payload: dict[str, Any]) -> OcrResult:
        rows = self._parse_html_table(content)
        if not rows:
            rows = self._parse_markdown_table(content)
        if rows:
            return OcrResult(text=self._rows_to_tsv(rows), is_table=True, raw=payload)
        return OcrResult(text=content, is_table=False, raw=payload)

    def _parse_html_table(self, content: str) -> list[list[str]]:
        if "<table" not in content.lower():
            return []
        parser = _HtmlTableParser()
        try:
            parser.feed(content)
        except Exception:
            return []
        return parser.best_table()

    def _parse_markdown_table(self, content: str) -> list[list[str]]:
        rows: list[list[str]] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|") or "|" not in stripped[1:]:
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            # Skip markdown separator rows like | --- | --- |.
            if cells and all(set(cell) <= {"-", ":", " "} and cell for cell in cells):
                continue
            rows.append([self._clean_cell(cell) for cell in cells])
        return [row for row in rows if any(cell for cell in row)] if len(rows) >= 2 else []

    def _rows_to_tsv(self, rows: list[list[str]]) -> str:
        return "\n".join("\t".join(self._clean_cell(cell) for cell in row) for row in rows).strip()

    def _clean_cell(self, value: Any) -> str:
        return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


class _HtmlTableParser(HTMLParser):
    """Minimal HTML table extractor returning rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: Any) -> None:
        tag = tag.lower()
        if tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._cell_parts is not None and self._current_row is not None:
            self._current_row.append("".join(self._cell_parts).strip())
            self._cell_parts = None
        elif tag == "tr" and self._current_row is not None and self._current_table is not None:
            self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            self._tables.append(self._current_table)
            self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def best_table(self) -> list[list[str]]:
        """Return the table with the most cells (ignoring empty rows)."""
        best: list[list[str]] = []
        best_score = 0
        for table in self._tables:
            cleaned = [row for row in table if any(cell.strip() for cell in row)]
            score = sum(len(row) for row in cleaned)
            if score > best_score:
                best = cleaned
                best_score = score
        return best
